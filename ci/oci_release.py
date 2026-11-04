"""NCE Core application OCI release stages, invoked by ci/pipeline.py.

Only the trusted Core runner checks out proprietary source. No source or
environment secrets are included in the released OCI artifacts.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from jsonschema import Draft202012Validator
from platform_services.github_ci import CIGateError, verify_deployment

ROOT = Path(__file__).resolve().parents[1]
CHART_SOURCE = ROOT / "charts/nce-application"
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9-]{0,52}[a-z0-9]\Z")
VERSION = re.compile(r"0|[1-9]\d*\.[0-9]+\.[0-9]+\Z")
ECR = re.compile(r"(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z]{2}(?:-[a-z]+)+-[0-9])\.amazonaws\.com/(?P<name>[a-z0-9][a-z0-9._/-]*)\Z")
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
    if set(workloads) != set(value["images"]):
        raise ReleaseError("Workload/image names do not match.")
    for name, workload in workloads.items():
        if workload.get("replicas", 1) != 1 and name in ("api", "browser"):
            raise ReleaseError("Single-process API and browser services require exactly one replica.")
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


def require_publish():
    if os.environ.get("CORE_CI_ALLOW_PUBLISH") != "1":
        raise ReleaseError("CORE_CI_ALLOW_PUBLISH=1 and protected environment approval are required.")


def require_runner_branch(config, core_commit):
    # Do not release code from an unreviewed feature branch or a detached,
    # stale checkout. The runner's CI receipt is independently verified.
    branch = config["runner_release_branch"]
    remote = execute(["git", "-C", str(ROOT), "ls-remote", "origin", "refs/heads/" + branch])
    if remote.split() != [core_commit, "refs/heads/" + branch]:
        raise ReleaseError("Runner checkout is not the current approved release branch tip.")


def image_references(config, app_commit, *, dry_run=False, env=None):
    refs = {}
    for name, image in sorted(config["images"].items()):
        tag = image["repository"] + ":" + app_commit
        if dry_run:
            digest = "sha256:" + hashlib.sha256((app_commit + "/" + name).encode()).hexdigest()
        else:
            execute(["docker", "build", "-f", image["dockerfile"], "--label",
                     "org.opencontainers.image.revision=" + app_commit, "-t", tag, "."],
                    cwd=env["source"], env=env["process"])
            execute(["docker", "image", "inspect", tag], env=env["process"])
            execute(["docker", "push", tag], env=env["process"])
            raw = execute(["docker", "buildx", "imagetools", "inspect", tag,
                           "--format", "{{json .Manifest}}"], env=env["process"])
            try:
                digest = json.loads(raw)["digest"]
            except (ValueError, KeyError, TypeError):
                raise ReleaseError("Registry did not return an image manifest digest.") from None
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
    values = deepcopy(config["values"])
    values["images"] = images
    values["releaseIdentity"] = {"sourceCommit": app_commit, "runnerCommit": core_commit}
    (chart / "values.yaml").write_text(json.dumps(values, sort_keys=True, indent=2) + "\n")
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
    execute(["kubeconform", "-strict", "-summary", "-ignore-missing-schemas"], input=rendered)
    return {"helm_lint": "passed", "helm_template": "passed", "kubernetes_schema": "passed",
            "render_sha256": hashlib.sha256(rendered.encode()).hexdigest(), "chart_version": version}


def package_chart(chart, version, destination):
    execute(["helm", "package", str(chart), "--destination", str(destination), "--version", version])
    artifact = Path(destination) / (chart.name + "-" + version + ".tgz")
    if not artifact.is_file():
        raise ReleaseError("Helm packaging did not produce the expected chart.")
    return artifact, "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()


def publish_chart(config, artifact, env):
    repository = config["registry"]["chart_repository"]
    parent = repository.rsplit("/", 1)[0]
    output = execute(["helm", "push", str(artifact), "oci://" + parent], env=env)
    digests = re.findall(r"(?m)^Digest: (sha256:[a-f0-9]{64})$", output)
    if len(digests) != 1:
        raise ReleaseError("Helm did not return an OCI manifest digest.")
    return repository, digests[0]


def record_manifest(config, document, path, *, publish=False, env=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    if publish:
        registry = config["registry"]
        key = f'oci-releases/{config["application"]["name"]}/{document["release_id"]}.json'
        execute(["aws", "s3api", "put-object", "--bucket", registry["release_bucket"],
                 "--key", key, "--body", str(path), "--content-type", "application/json",
                 "--if-none-match", "*", "--region", registry["region"]], env=env)
    return path


def release(config_path, source, *, mode="verify"):
    config = load_config(config_path)
    if mode not in ("verify", "publish"):
        raise ReleaseError("Unsupported Helm release mode.")
    app_root, app_commit = ensure_source(source, config)
    core_commit = checkout_commit(ROOT)
    if mode == "publish":
        require_publish()
        require_runner_branch(config, core_commit)
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
            images = image_references(config, app_commit, dry_run=(mode == "verify"),
                env={"source": app_root, "process": environment})
            completed.extend(STAGES[:2] if mode == "publish" else ("synthetic-image-preview",))
            chart, version = generate_chart(config, images, app_commit, core_commit, temp_path)
            completed.append("generate-helm")
            validation = validate_chart(config, chart, version)
            completed.append("validate-helm")
            package, package_digest = package_chart(chart, version, temp_path)
            completed.append("package-helm")
            oci = config["registry"]["chart_repository"]
            chart_digest = ""
            if mode == "publish":
                oci, chart_digest = publish_chart(config, package, environment)
                completed.append("publish-helm-oci")
            shutil.copy2(package, artifacts_dir / package.name)
            document = {
                "schema_version": 1, "release_id": release_id, "application": config["application"],
                "source_commit": app_commit, "runner_repository": "NorthCountryEngineer/NCE_Core",
                "runner_commit": core_commit, "images": images, "chart": {
                    "version": version, "oci": oci + ":" + version,
                    "oci_digest": chart_digest, "package_digest": package_digest},
                "ci": receipts, "validation": validation, "stages": completed,
                "status": "published" if mode == "publish" else "dry-run",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sbom": "not-produced", "signatures": "not-produced",
            }
            # Signing is a required optional release policy, never an implicit
            # unsigned production fallback when the operator requests it.
            if mode == "publish" and config.get("require_signatures", False):
                if not shutil.which("cosign"):
                    raise ReleaseError("The required OCI signing tool is missing.")
                for reference in [*images.values(), oci + "@" + chart_digest]:
                    execute(["cosign", "sign", "--yes", reference], env=environment)
                document["signatures"] = "cosign"
            if mode == "publish" and config.get("require_sbom", False):
                if not shutil.which("syft"):
                    raise ReleaseError("The required SBOM tool is missing.")
                for name, reference in images.items():
                    file = artifacts_dir / (name + ".spdx.json")
                    execute(["syft", reference, "-o", "spdx-json=" + str(file)], env=environment)
                document["sbom"] = "spdx-json"
            completed.append("record-release")
            record_manifest(config, document, artifacts_dir / "release.json",
                            publish=(mode == "publish"), env=environment)
            return document
