"""NCE Core application OCI release stages, invoked by ci/pipeline.py.

Only the trusted Core runner checks out proprietary source. No source or
environment secrets are included in the released OCI artifacts.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from jsonschema import Draft202012Validator
from platform_services.github_ci import CIGateError, verify_deployment

ROOT = Path(__file__).resolve().parents[1]
CHART_SOURCE = ROOT / "charts/nce-application"
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9-]{0,52}[a-z0-9]\Z")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
ECR = re.compile(r"(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z]{2}(?:-[a-z]+)+-[0-9])\.amazonaws\.com/(?P<name>[a-z0-9][a-z0-9._/-]*)\Z")
KMS = re.compile(r"awskms:///(?P<arn>arn:aws:kms:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):key/(?:[a-f0-9-]{36}|mrk-[a-f0-9]{32}))\Z")
HELM_CONFIG = "application/vnd.cncf.helm.config.v1+json"
HELM_CONTENT = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
STAGES = ("build-images", "publish-images", "generate-helm", "validate-helm", "package-helm", "publish-helm-oci", "record-release")


class ReleaseError(ValueError):
    """A sanitized rejection; do not leak CLI stderr or configuration values."""


def execute(args, *, cwd=None, env=None, input=None):
    try:
        result = subprocess.run(args, cwd=cwd or ROOT, env=env, input=input, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("Required release command is unavailable or timed out.") from None
    if result.returncode:
        raise ReleaseError("Release command failed: " + Path(args[0]).name) from None
    return result.stdout.strip()


def checkout_commit(path):
    commit = execute(["git", "-C", str(path), "rev-parse", "--verify", "HEAD^{commit}"])
    if not SHA.fullmatch(commit):
        raise ReleaseError("A full checked-out Git commit is required.")
    if execute(["git", "-C", str(path), "status", "--porcelain"]):
        raise ReleaseError("Release requires a clean checkout.")
    return commit


def load_config(path):
    target = Path(path).resolve()
    allowed = (ROOT / "ci/applications").resolve()
    if not target.is_relative_to(allowed) or not target.is_file() or target.is_symlink():
        raise ReleaseError("Application release settings must be a reviewed NCE Core CI file.")
    try:
        value = json.loads(target.read_text())
        schema = json.loads((ROOT / "ci/oci.schema.json").read_text())
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: str(list(item.path)))
    except (ValueError, OSError):
        raise ReleaseError("Invalid application release settings.") from None
    if errors:
        raise ReleaseError("Invalid release configuration fields: " +
                           ", ".join("/".join(map(str, item.path)) or "<root>" for item in errors))
    for image in value["images"].values():
        match = ECR.fullmatch(image["repository"])
        if not match or match["account"] != value["registry"]["account_id"] or match["region"] != value["registry"]["region"]:
            raise ReleaseError("Each image must use an approved ECR repository in the release account and region.")
        dockerfile = image["dockerfile"]
        if Path(dockerfile).is_absolute() or ".." in Path(dockerfile).parts:
            raise ReleaseError("Dockerfile paths must remain inside the application checkout.")
    registry = value["registry"]
    repository = registry["chart_repository"]
    match = ECR.fullmatch(repository)
    if not match or match["account"] != registry["account_id"] or match["region"] != registry["region"]:
        raise ReleaseError("Chart repository must be in the approved ECR account and region.")
    if not repository.endswith("/" + value["chart"]["name"]):
        raise ReleaseError("Chart repository must end in the chart name.")
    # All deployable workloads have exactly one digest-pinned image. Disallow
    # arbitrary image/command injection from an unreviewed environment overlay.
    workloads = value["values"]["workloads"]
    if set(workloads) != {"web", "api", "browser"} or set(value["images"]) != set(workloads):
        raise ReleaseError("This chart requires exactly the web, api and browser workloads.")
    for key in value["values"]["external"]["apiEnvironment"]:
        if re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY", key):
            raise ReleaseError("Secret values may not be embedded in chart environment settings.")
    for name, workload in workloads.items():
        if workload.get("replicas", 1) != 1 and name in ("api", "browser"):
            raise ReleaseError("Single-process API and browser services require exactly one replica.")
    if "signing" in value:
        key = KMS.fullmatch(value["signing"]["key_uri"])
        if not key or key["account"] != registry["account_id"] or key["region"] != registry["region"]:
            raise ReleaseError("Signing requires an immutable KMS key in the release account and region.")
    return value


def release_version(base, app_commit, runner_commit):
    if not VERSION.fullmatch(base):
        raise ReleaseError("Invalid chart base version.")
    return f"{base}-{app_commit[:12]}.r{runner_commit[:8]}"


def ensure_source(source, config):
    folder = Path(source).resolve()
    if not folder.is_dir() or not (folder / ".git").exists():
        raise ReleaseError("A separate application Git checkout is required on the NCE runner.")
    sha = checkout_commit(folder)
    if config["application"]["repository"].casefold() == "northcountryengineer/nce_core":
        raise ReleaseError("The application source must be a separate reviewed repository.")
    for image in config["images"].values():
        target = (folder / image["dockerfile"]).resolve()
        if not target.is_relative_to(folder) or not target.is_file() or target.is_symlink():
            raise ReleaseError("An approved application Dockerfile is missing.")
    return folder, sha


def admission(config, app_commit, core_commit):
    try:
        receipts = verify_deployment(config["application"]["repository"], app_commit,
                                      "NorthCountryEngineer/NCE_Core", core_commit)
    except CIGateError as error:
        raise ReleaseError(str(error)) from None
    if len(receipts) != 2:
        raise ReleaseError("Both exact-commit application and runner CI receipts are required.")
    return receipts


def require_publish(config):
    if os.environ.get("CORE_CI_ALLOW_PUBLISH") != "1" or os.environ.get("CORE_CI_ENVIRONMENT") != config["environment"]:
        raise ReleaseError("Publish requires the approved matching protected CI environment.")
    if config["environment"] == "production" and (
            not config.get("require_signatures") or not config.get("require_sbom")):
        raise ReleaseError("Production publication requires both signed artifacts and verified SBOMs.")


def require_runner_branch(config, core_commit):
    # Do not release code from an unreviewed feature branch or a detached,
    # stale checkout. The runner's CI receipt is independently verified.
    branch = config["runner_release_branch"]
    remote = execute(["git", "-C", str(ROOT), "ls-remote", "origin", "refs/heads/" + branch])
    if remote.split() != [core_commit, "refs/heads/" + branch]:
        raise ReleaseError("Runner checkout is not the current approved release branch tip.")


def signing_key(config):
    key = KMS.fullmatch(config.get("signing", {}).get("key_uri", ""))
    if (not key or key["account"] != config["registry"]["account_id"]
            or key["region"] != config["registry"]["region"]):
        raise ReleaseError("Production signing requires a reviewed KMS key in the release account and region.")
    return key["arn"]


def sign_blob(config, source, signature, public_key, *, env=None):
    execute(["cosign", "sign-blob", "--yes", "--key", config["signing"]["key_uri"],
             "--tlog-upload=false", "--output-signature", str(signature), str(source)], env=env)
    if not signature.is_file() or not signature.stat().st_size:
        raise ReleaseError("Signing did not produce a verifiable signature.")
    execute(["cosign", "verify-blob", "--key", str(public_key),
             "--insecure-ignore-tlog=true", "--signature", str(signature), str(source)], env=env)


def tool_versions(config, *, env=None):
    commands = {"aws": ["aws", "--version"], "docker": ["docker", "version", "--format", "{{.Client.Version}}"],
                "helm": ["helm", "version", "--short"], "kubeconform": ["kubeconform", "-v"]}
    if config.get("require_signatures"):
        commands["cosign"] = ["cosign", "version", "--json"]
    if config.get("require_sbom"):
        commands["syft"] = ["syft", "version", "-o", "json"]
    versions = {"python": sys.version.split()[0]}
    for tool, args in commands.items():
        version = execute(args, env=env)
        if not version or len(version) > 4096:
            raise ReleaseError("A required release tool did not return bounded version evidence.")
        versions[tool] = version
    return versions


def publish_preflight(config, app_commit, core_commit, destination, *, env=None):
    """Verify infrastructure and signing before the first registry write."""
    registry = config["registry"]
    region = registry["region"]
    identity = json_command(["aws", "sts", "get-caller-identity", "--region", region,
                             "--output", "json"], env=env)
    if identity.get("Account") != registry["account_id"]:
        raise ReleaseError("AWS identity does not match the approved release account.")
    project = config.get("builder_project", "")
    build_arn = (env or os.environ).get("CODEBUILD_BUILD_ARN", "")
    expected = f'arn:aws:codebuild:{region}:{registry["account_id"]}:build/{project}:'
    if (config["environment"] == "production" or project) and (
            not project or not build_arn.startswith(expected) or not re.fullmatch(
                re.escape(expected) + r"[a-f0-9-]{36}", build_arn)):
        raise ReleaseError("Production artifacts must be built in the reviewed NCE CodeBuild project.")
    repositories = sorted({item["repository"] for item in config["images"].values()} |
                          {registry["chart_repository"]})
    response = json_command(["aws", "ecr", "describe-repositories", "--repository-names",
        *[repository_name(item) for item in repositories], "--region", region,
        "--output", "json"], env=env)
    actual = {item.get("repositoryUri"): item for item in response.get("repositories", [])}
    if set(actual) != set(repositories) or any(item.get("imageTagMutability") != "IMMUTABLE"
                                              for item in actual.values()):
        raise ReleaseError("All release repositories must exist and enforce immutable tags without exclusions.")
    release_id = release_version(config["chart"]["base_version"], app_commit, core_commit)
    for repository in repositories:
        response = json_command(["aws", "ecr", "batch-get-image", "--repository-name",
            repository_name(repository), "--image-ids", "imageTag=" + release_id,
            "--region", region, "--output", "json"], env=env)
        failures = response.get("failures", [])
        if (response.get("images") or len(failures) != 1
                or failures[0].get("failureCode") != "ImageNotFound"):
            raise ReleaseError("Release identity already exists or cannot be checked; immutable artifacts are never overwritten. Inspect the prior release before retrying.")
    bucket = json_command(["aws", "s3api", "get-bucket-location", "--bucket", registry["release_bucket"],
        "--expected-bucket-owner", registry["account_id"], "--region", region,
        "--output", "json"], env=env)
    if (bucket.get("LocationConstraint") or "us-east-1") != region:
        raise ReleaseError("Release receipt storage must be in the approved AWS region.")
    public_key = None
    key_arn = ""
    if config.get("require_signatures"):
        key_arn = signing_key(config)
        metadata = json_command(["aws", "kms", "describe-key", "--key-id", key_arn,
            "--region", region, "--output", "json"], env=env).get("KeyMetadata", {})
        if (metadata.get("Arn") != key_arn or metadata.get("KeyState") != "Enabled"
                or metadata.get("KeyUsage") != "SIGN_VERIFY"
                or metadata.get("KeySpec") != "ECC_NIST_P256"):
            raise ReleaseError("The reviewed signing key must be an enabled P-256 asymmetric signing key.")
        public_key = Path(destination) / "signing-public.pem"
        pem = execute(["cosign", "public-key", "--key", config["signing"]["key_uri"]], env=env)
        if not pem.startswith("-----BEGIN PUBLIC KEY-----") or not pem.endswith("-----END PUBLIC KEY-----"):
            raise ReleaseError("The signing key did not return a verification public key.")
        public_key.write_text(pem + "\n")
        challenge = Path(destination) / "signing-preflight.txt"
        challenge.write_text("NCE private OCI release signing preflight\n" + release_id + "\n")
        sign_blob(config, challenge, Path(destination) / "signing-preflight.sig", public_key, env=env)
    return {"account_id": identity["Account"], "principal_arn": identity.get("Arn", ""),
            "codebuild_arn": build_arn, "signing_key_arn": key_arn,
            "tools": tool_versions(config, env=env)}, public_key


def json_command(args, *, env=None):
    try:
        value = json.loads(execute(args, env=env))
    except (ValueError, TypeError):
        raise ReleaseError("A release verification command returned invalid JSON.") from None
    if not isinstance(value, dict):
        raise ReleaseError("A release verification command returned invalid metadata.")
    return value


def repository_name(reference):
    match = ECR.fullmatch(reference)
    if not match:
        raise ReleaseError("An approved ECR repository is required.")
    return match["name"]


def registry_manifest(config, repository, *, tag=None, digest=None, env=None):
    selector = "imageDigest=" + digest if digest else "imageTag=" + tag
    response = json_command(["aws", "ecr", "batch-get-image", "--repository-name",
        repository_name(repository), "--image-ids", selector, "--region",
        config["registry"]["region"], "--output", "json"], env=env)
    images = response.get("images", [])
    if response.get("failures") or not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ReleaseError("The expected immutable registry artifact is missing or ambiguous.")
    image = images[0]
    actual = (image.get("imageId") or {}).get("imageDigest", "")
    raw = image.get("imageManifest", "")
    if (not DIGEST.fullmatch(actual) or not isinstance(raw, str)
            or "sha256:" + hashlib.sha256(raw.encode()).hexdigest() != actual
            or (digest and digest != actual)):
        raise ReleaseError("Registry manifest content does not match its immutable digest.")
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise ReleaseError("Registry manifest is invalid.") from None
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2
            or not isinstance(manifest.get("config"), dict)
            or not isinstance(manifest.get("layers"), list)
            or any(not isinstance(layer, dict) for layer in manifest["layers"])):
        raise ReleaseError("Registry manifest has an unsupported schema.")
    return actual, manifest


def image_references(config, app_commit, *, dry_run=False, env=None, core_commit=None):
    refs = {}
    # The builder revision participates in the tag and image config. A repaired
    # builder can publish a new release without overwriting an immutable tag or
    # appending to an immutable legacy Cosign signature tag.
    identity = release_version(config["chart"]["base_version"], app_commit, core_commit) if core_commit else app_commit
    if dry_run:
        for name, image in sorted(config["images"].items()):
            digest = "sha256:" + hashlib.sha256((app_commit + "/" + name).encode()).hexdigest()
            refs[name] = image["repository"] + "@" + digest
        return refs
    tags = {name: image["repository"] + ":" + identity for name, image in config["images"].items()}
    for name, image in sorted(config["images"].items()):
        execute(["docker", "build", "-f", image["dockerfile"], "--label",
                 "org.opencontainers.image.revision=" + app_commit, "--label",
                 "org.opencontainers.image.vendor=North Country Engineer", "--label",
                 "ai.nce.core.runner.revision=" + (core_commit or "unspecified"),
                 "--platform", "linux/amd64", "-t", tags[name], "."],
                cwd=env["source"], env=env["process"])
        execute(["docker", "image", "inspect", tags[name]], env=env["process"])
    # These are NCE-owned disposable containers with synthetic state. Their
    # runtime verification must finish before the first registry publication.
    from ci.oci_runtime import runtime_smoke
    env["runtime_evidence"] = runtime_smoke(tags, execute=execute, env=env["process"])
    for name, image in sorted(config["images"].items()):
        execute(["docker", "push", tags[name]], env=env["process"])
        digest, manifest = registry_manifest(config, image["repository"], tag=identity, env=env["process"])
        if manifest.get("config", {}).get("mediaType") not in (
                "application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"):
            raise ReleaseError("Registry artifact is not a production container image.")
        reference = image["repository"] + "@" + digest
        execute(["docker", "pull", reference], env=env["process"])
        inspected = json_command(["docker", "image", "inspect", reference,
            "--format", "{{json .}}"], env=env["process"])
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        if (reference not in inspected.get("RepoDigests", [])
                or labels.get("org.opencontainers.image.revision") != app_commit
                or labels.get("ai.nce.core.runner.revision") != (core_commit or "unspecified")
                or inspected.get("Architecture") != "amd64"):
            raise ReleaseError("Retrieved image does not match the exact source and builder identity.")
        if not DIGEST.fullmatch(digest):
            raise ReleaseError("Registry did not return a SHA-256 image digest.")
        refs[name] = image["repository"] + "@" + digest
    return refs


@contextmanager
def registry_login(config):
    # AWS CLI and the runner's short-lived identity provide the sole credentials.
    with tempfile.TemporaryDirectory(prefix="nce-oci-credentials-") as temp:
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = str(Path(temp) / "docker")
        env["HELM_REGISTRY_CONFIG"] = str(Path(temp) / "helm" / "registry.json")
        Path(env["DOCKER_CONFIG"]).mkdir(mode=0o700)
        Path(env["HELM_REGISTRY_CONFIG"]).parent.mkdir(mode=0o700)
        password = execute(["aws", "ecr", "get-login-password", "--region", config["registry"]["region"]], env=env)
        host = f'{config["registry"]["account_id"]}.dkr.ecr.{config["registry"]["region"]}.amazonaws.com'
        execute(["docker", "login", "--username", "AWS", "--password-stdin", host],
                input=password + "\n", env=env)
        execute(["helm", "registry", "login", "--username", "AWS", "--password-stdin", host],
                input=password + "\n", env=env)
        yield env


def generate_chart(config, images, app_commit, core_commit, destination):
    if set(images) != set(config["images"]):
        raise ReleaseError("Missing release image digests.")
    for name, reference in images.items():
        if reference.rsplit("@", 1)[0] != config["images"][name]["repository"] or not DIGEST.fullmatch(reference.rsplit("@", 1)[-1]):
            raise ReleaseError("Chart image is not a digest from the approved release repository.")
    chart = Path(destination) / config["chart"]["name"]
    shutil.copytree(CHART_SOURCE, chart)
    version = release_version(config["chart"]["base_version"], app_commit, core_commit)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: " + config["chart"]["name"] +
        "\ndescription: Immutable NCE application release\n"
        "type: application\nversion: " + version + "\nappVersion: \"" + app_commit + "\"\n")
    # The published application artifact is target-neutral. Deployment-specific
    # origins, external endpoints, Secret names and Raft networking belong to
    # the destination's separately approved values overlay only.
    values = {
        "images": images,
        "releaseIdentity": {"sourceCommit": app_commit, "runnerCommit": core_commit},
        "workloads": deepcopy(config["values"]["workloads"]),
        "external": {"databaseCredentialRef": "", "runtimeCredentialRef": "",
                     "migrationStateClaim": "",
                     "apiServiceAccountAnnotations": {}, "migrationServiceAccountAnnotations": {},
                     "pullSecrets": [], "apiEnvironment": {}, "apiEnvFromKeys": [],
                     "externalEgressCIDRs": []},
        "ingress": {"enabled": False, "className": "", "host": "", "tlsRef": ""},
        "networkPolicy": {"enabled": True, "ingressNamespaceSelector": {},
                          "ingressPodSelector": {}, "browserPublicEgress": False},
    }
    (chart / "values.yaml").write_text(json.dumps(values, sort_keys=True, indent=2) + "\n")
    schema_path = chart / "values.schema.json"
    schema = json.loads(schema_path.read_text())
    for name, reference in images.items():
        schema["properties"]["images"]["properties"][name]["const"] = reference
    for name, commit in values["releaseIdentity"].items():
        schema["properties"]["releaseIdentity"]["properties"][name]["const"] = commit
    schema_path.write_text(json.dumps(schema, sort_keys=True, indent=2) + "\n")
    return chart, version


def validate_chart(config, chart, version):
    # Helm lint validates values.schema.json; kubeconform checks Kubernetes APIs.
    # No --skip-schema-validation, no cluster access and no apply operation.
    overrides = (ROOT / config["validation_values_file"]).resolve()
    if not overrides.is_relative_to(ROOT / "ci/applications") or not overrides.is_file() or overrides.is_symlink():
        raise ReleaseError("A reviewed non-secret validation values file is required.")
    name = config["application"]["name"]
    arguments = ["--values", str(overrides)]
    execute(["helm", "lint", "--strict", str(chart), *arguments])
    rendered = execute(["helm", "template", name, str(chart), "--namespace",
                        "nce-validation", *arguments])
    if re.search(r"(?m)^kind:\s*Secret\s*$", rendered):
        raise ReleaseError("Generated chart must not create secrets.")
    if not rendered.strip():
        raise ReleaseError("Helm rendered no Kubernetes manifests.")
    execute(["kubeconform", "-strict", "-summary"], input=rendered)
    return {"helm_lint": "passed", "helm_template": "passed", "kubernetes_schema": "passed",
            "render_sha256": hashlib.sha256(rendered.encode()).hexdigest(), "chart_version": version}


def package_chart(chart, version, destination):
    execute(["helm", "package", str(chart), "--destination", str(destination), "--version", version])
    artifact = Path(destination) / (chart.name + "-" + version + ".tgz")
    if not artifact.is_file():
        raise ReleaseError("Helm packaging did not produce the expected chart.")
    return artifact, "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()


def retrieve_chart(config, repository, digest, package_digest, destination, *, env=None):
    actual, manifest = registry_manifest(config, repository, digest=digest, env=env)
    layers = manifest.get("layers", [])
    if (manifest.get("config", {}).get("mediaType") != HELM_CONFIG
            or len(layers) != 1 or layers[0].get("mediaType") != HELM_CONTENT
            or layers[0].get("digest") != package_digest):
        raise ReleaseError("Published Helm manifest does not match the validated chart package.")
    layer = json_command(["aws", "ecr", "get-download-url-for-layer", "--repository-name",
        repository_name(repository), "--layer-digest", package_digest, "--region",
        config["registry"]["region"], "--output", "json"], env=env)
    url = layer.get("downloadUrl", "")
    # Only an AWS-returned temporary URL is used, never persisted or logged.
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ReleaseError("Registry chart retrieval is unavailable.")
    try:
        with urlopen(url, timeout=60) as response:
            data = response.read(16 * 1024 * 1024 + 1)
    except Exception:
        raise ReleaseError("Registry chart retrieval failed.") from None
    if len(data) > 16 * 1024 * 1024 or "sha256:" + hashlib.sha256(data).hexdigest() != package_digest:
        raise ReleaseError("Retrieved chart content does not match the validated package digest.")
    Path(destination).write_bytes(data)
    return actual


def publish_chart(config, artifact, env, *, version=None):
    repository = config["registry"]["chart_repository"]
    parent = repository.rsplit("/", 1)[0]
    execute(["helm", "push", str(artifact), "oci://" + parent], env=env)
    # Helm writes its human receipt to stderr. Resolve the actual manifest from
    # ECR and verify its content instead of parsing CLI presentation output.
    version = version or artifact.name.removeprefix(config["chart"]["name"] + "-").removesuffix(".tgz")
    digest, _ = registry_manifest(config, repository, tag=version, env=env)
    package_digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="nce-oci-retrieval-") as temp:
        retrieve_chart(config, repository, digest, package_digest, Path(temp) / artifact.name, env=env)
    return repository, digest


def upload_evidence(config, source, key, *, env=None):
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    execute(["aws", "s3api", "put-object", "--bucket", config["registry"]["release_bucket"],
             "--key", key, "--body", str(source), "--if-none-match", "*", "--region",
             config["registry"]["region"]], env=env)
    with tempfile.TemporaryDirectory(prefix="nce-evidence-retrieval-") as temp:
        retrieved = Path(temp) / "evidence"
        execute(["aws", "s3api", "get-object", "--bucket", config["registry"]["release_bucket"],
                 "--key", key, "--region", config["registry"]["region"], str(retrieved)], env=env)
        if not retrieved.is_file() or "sha256:" + hashlib.sha256(retrieved.read_bytes()).hexdigest() != digest:
            raise ReleaseError("Stored release evidence does not match its recorded digest.")
    return {"uri": "s3://" + config["registry"]["release_bucket"] + "/" + key,
            "digest": digest, "retrieval": "passed"}


def verify_signature(reference, public_key, *, env=None):
    try:
        signatures = json.loads(execute(["cosign", "verify", "--key", str(public_key),
            "--insecure-ignore-tlog=true", reference], env=env))
    except ValueError:
        raise ReleaseError("OCI signature verification returned invalid evidence.") from None
    digest = reference.rsplit("@", 1)[-1]
    if (not isinstance(signatures, list) or not signatures
            or not all(row.get("critical", {}).get("image", {}).get("docker-manifest-digest") == digest
                       for row in signatures if isinstance(row, dict))
            or any(not isinstance(row, dict) for row in signatures)):
        raise ReleaseError("OCI signature evidence does not match the immutable artifact digest.")


def verify_attestation(reference, public_key, predicate, *, env=None):
    try:
        raw = execute(["cosign", "verify-attestation", "--key", str(public_key),
            "--insecure-ignore-tlog=true", "--type", "spdxjson", reference], env=env)
        try:
            rows = json.loads(raw)
        except ValueError:
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not rows:
            raise ValueError
        statements = [json.loads(base64.b64decode(row["payload"], validate=True)) for row in rows]
    except (ValueError, KeyError, TypeError):
        raise ReleaseError("SBOM attestation verification returned invalid evidence.") from None
    digest = reference.rsplit("@sha256:", 1)[-1]
    for statement in statements:
        subjects = statement.get("subject", [])
        if (statement.get("predicate") == predicate
                and any(item.get("digest", {}).get("sha256") == digest for item in subjects)):
            return statement
    raise ReleaseError("Signed SBOM does not match the retrieved image and generated package inventory.")


def record_manifest(config, document, path, *, publish=False, env=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    if publish:
        registry = config["registry"]
        key = f'oci-releases/{config["application"]["name"]}/{document["release_id"]}.json'
        upload_evidence(config, path, key, env=env)
    return path


def release(config_path, source, *, mode="verify"):
    config = load_config(config_path)
    if mode not in ("verify", "publish"):
        raise ReleaseError("Unsupported Helm release mode.")
    app_root, app_commit = ensure_source(source, config)
    core_commit = checkout_commit(ROOT)
    if mode == "publish":
        require_publish(config)
        require_runner_branch(config, core_commit)
        required = ["aws", "docker", "helm", "kubeconform"]
        required.extend(["cosign"] if config.get("require_signatures") else [])
        required.extend(["syft"] if config.get("require_sbom") else [])
        if any(not shutil.which(tool) for tool in required):
            raise ReleaseError("A required release, signing or SBOM tool is unavailable.")
        if config.get("require_signatures"):
            signing_key(config)
    receipts = admission(config, app_commit, core_commit)
    release_id = release_version(config["chart"]["base_version"], app_commit, core_commit)
    completed = []
    artifacts_dir = ROOT / "artifacts" / "oci" / config["application"]["name"] / release_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    # The Core pipeline's check/test stage and exact-commit application CI
    # attest the test suite. We rebuild production images in the release.
    with tempfile.TemporaryDirectory(prefix="nce-helm-") as temp:
        temp_path = Path(temp)
        @contextmanager
        def credentials():
            if mode == "publish":
                with registry_login(config) as env:
                    yield env
            else:
                yield os.environ.copy()
        with credentials() as environment:
            # Catch schema/template/tool failures before any image is pushed.
            preview = temp_path / "preview"
            preview.mkdir()
            synthetic = image_references(config, app_commit, dry_run=True, core_commit=core_commit)
            preview_chart, preview_version = generate_chart(config, synthetic, app_commit, core_commit, preview)
            validate_chart(config, preview_chart, preview_version)
            builder = {}
            public_key = None
            if mode == "publish":
                builder, public_key = publish_preflight(config, app_commit, core_commit, temp_path,
                                                        env=environment)
            build_context = {"source": app_root, "process": environment}
            images = image_references(config, app_commit, dry_run=(mode == "verify"),
                env=build_context, core_commit=core_commit)
            completed.extend(STAGES[:2] if mode == "publish" else ("synthetic-image-preview",))
            chart, version = generate_chart(config, images, app_commit, core_commit, temp_path)
            completed.append("generate-helm")
            validation = validate_chart(config, chart, version)
            if mode == "publish":
                validation["container_runtime"] = build_context["runtime_evidence"]
            completed.append("validate-helm")
            package, package_digest = package_chart(chart, version, temp_path)
            completed.append("package-helm")
            oci = config["registry"]["chart_repository"]
            chart_digest = ""
            if mode == "publish":
                oci, chart_digest = publish_chart(config, package, environment, version=version)
                completed.append("publish-helm-oci")
                validation.update(immutable_image_retrieval="passed", immutable_chart_retrieval="passed",
                                  chart_media_type=HELM_CONFIG, chart_package_content="digest-verified")
            shutil.copy2(package, artifacts_dir / package.name)
            document = {
                "schema_version": 1, "release_id": release_id, "application": config["application"],
                "source_commit": app_commit, "runner_repository": "NorthCountryEngineer/NCE_Core",
                "runner_commit": core_commit, "images": images, "chart": {
                    "version": version, "oci": oci + ":" + version,
                    "oci_digest": chart_digest, "package_digest": package_digest},
                "ci": receipts, "validation": validation, "stages": completed,
                "provenance": {"source_repository": config["application"]["repository"],
                               "source_commit": app_commit, "builder_repository": "NorthCountryEngineer/NCE_Core",
                               "builder_commit": core_commit, "helm_template": "charts/nce-application",
                               "container_build": "reviewed Dockerfiles at exact source commit",
                               "builder": builder},
                "status": "published" if mode == "publish" else "dry-run",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sbom": "not-produced", "signatures": "not-produced", "sboms": {},
            }
            evidence_prefix = f'oci-releases/{config["application"]["name"]}/{release_id}/'
            # Signing is a required optional release policy, never an implicit
            # unsigned production fallback when the operator requests it.
            if mode == "publish" and config.get("require_signatures", False):
                key_file = artifacts_dir / "signing-public.pem"
                shutil.copy2(public_key, key_file)
                key_evidence = upload_evidence(config, key_file, evidence_prefix + key_file.name, env=environment)
                signing = {"key_uri": config["signing"]["key_uri"], "public_key": key_evidence,
                           "transparency_log": False, "trust": "NCE KMS public key",
                           "verification": "passed", "artifacts": []}
                for reference in [*images.values(), oci + "@" + chart_digest]:
                    execute(["cosign", "sign", "--yes", "--key", config["signing"]["key_uri"],
                             "--tlog-upload=false", reference], env=environment)
                    verify_signature(reference, public_key, env=environment)
                    signing["artifacts"].append({"reference": reference, "verification": "passed"})
                document["signatures"] = "cosign"
                document["signing"] = signing
                completed.append("sign-and-verify-artifacts")
            if mode == "publish" and config.get("require_sbom", False):
                for name, reference in images.items():
                    file = artifacts_dir / (name + ".spdx.json")
                    execute(["syft", reference, "-o", "spdx-json=" + str(file)], env=environment)
                    try:
                        predicate = json.loads(file.read_text())
                    except (OSError, ValueError):
                        raise ReleaseError("SBOM generation did not produce a valid package inventory.") from None
                    if (not isinstance(predicate, dict) or not predicate.get("spdxVersion", "").startswith("SPDX-2.")
                            or not isinstance(predicate.get("packages"), list)):
                        raise ReleaseError("SBOM generation did not produce a valid SPDX package inventory.")
                    inventory = upload_evidence(config, file, evidence_prefix + file.name, env=environment)
                    inventory["image"] = reference
                    if config.get("require_signatures"):
                        execute(["cosign", "attest", "--yes", "--key", config["signing"]["key_uri"],
                                 "--tlog-upload=false", "--type", "spdxjson", "--predicate", str(file),
                                 reference], env=environment)
                        statement = verify_attestation(reference, public_key, predicate, env=environment)
                        attestation_file = artifacts_dir / (name + ".attestation.json")
                        attestation_file.write_text(json.dumps(statement, sort_keys=True, indent=2) + "\n")
                        inventory["attestation"] = upload_evidence(config, attestation_file,
                            evidence_prefix + attestation_file.name, env=environment)
                        inventory["attestation"]["verification"] = "passed"
                    document["sboms"][name] = inventory
                document["sbom"] = "spdx-json"
                completed.append("generate-and-verify-sbom")
            completed.append("record-release")
            manifest_file = artifacts_dir / "release.json"
            pending_file = artifacts_dir / "release.pending.json"
            if mode == "publish" and config.get("require_signatures"):
                signature_file = artifacts_dir / "release.json.sig"
                document["receipt_verification"] = {
                    "signature_uri": "s3://" + config["registry"]["release_bucket"] + "/" + evidence_prefix + signature_file.name,
                    "public_key": document["signing"]["public_key"], "verification": "passed",
                    "transparency_log": False}
                record_manifest(config, document, pending_file)
                sign_blob(config, pending_file, signature_file, public_key, env=environment)
                upload_evidence(config, signature_file, evidence_prefix + signature_file.name, env=environment)
            record_manifest(config, document, pending_file,
                            publish=(mode == "publish"), env=environment)
            pending_file.replace(manifest_file)
            return document
