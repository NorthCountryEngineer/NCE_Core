"""AWS deployment control plane. Never runs application code on the Core host.

The separately onboarded source builder has no deployment permissions. The
trusted runner is pinned to a Core commit and consumes only declarative metadata
and build artifacts. AWS sessions are transient and never serialized into jobs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Callable
from .cloud_contract import ContractError, fargate_applications, academy_fargate_applications, identity_contract
from platform_services.github_ci import CIGateError, POLICIES, verify_deployment


class AWSAdapterError(Exception):
    """A deliberately sanitized error suitable for a deployment event."""


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
IDENTIFIER = re.compile(r"[a-zA-Z0-9-]{1,64}\Z")
ARCHETYPES = ("amplify-static", "amplify-serverless", "amplify-fargate", "amplify-academy-fargate")
APP_PERMISSIONS = [
    {"codename": "app-read", "name": "Read application data"},
    {"codename": "app-write", "name": "Write application data"},
]
PUBLIC_VARIABLE = re.compile(r"VITE_[A-Z0-9_]{1,100}\Z")
PUBLIC_OUTPUTS = {"origin", "amplify_app_id", "api_url", "stack_name", "commit", "archetype", "identity"}


def _value(platform, name, default=None):
    return platform.get(name, default) if isinstance(platform, dict) else getattr(platform, name, default)


def _required(value, pattern, description):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise AWSAdapterError(f"Invalid {description} in deployment platform settings.")
    return value


def configuration(platform, snapshot=None):
    """Validate server-owned settings before credential acquisition or mutation."""
    config = dict(_value(platform, "config", {}) or {})
    approved_keys = {"runner_repository", "runner_commit", "build_project", "artifact_bucket",
                     "cloudformation_role_arn", "permissions_boundary_arn", "archetypes", "environment_ids",
                     "credentials_kms_key_arn", "fargate_apps", "fargate_cloudformation_role_arn", "academy_fargate_apps"}
    if set(config) - approved_keys:
        raise AWSAdapterError("Platform configuration contains unsupported fields.")
    if snapshot and snapshot.get("platform_config") != config:
        raise AWSAdapterError("Platform settings changed. Review a new deployment plan.")
    account = _required(_value(platform, "account_id"), r"[0-9]{12}", "AWS account")
    region = _required(_value(platform, "region"), r"[a-z]{2}(-[a-z]+)+-[0-9]", "AWS region")
    prefix = _required(_value(platform, "prefix", "nce"), r"[a-z][a-z0-9-]{2,15}", "resource prefix")
    role = _required(_value(platform, "role_arn"), rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", "control role")
    _required(_value(platform, "external_id"), r"[A-Za-z0-9+=,.@:/_-]{16,128}", "platform external ID")
    connection = _required(_value(platform, "connection_arn"), rf"arn:aws:(?:codeconnections|codestar-connections):{region}:{account}:connection/[a-zA-Z0-9-]+", "source connection")
    runner = _required(_value(platform, "runner_project"), r"[A-Za-z0-9_-]{2,255}", "deployment runner")
    builder = _required(config.get("build_project"), r"[A-Za-z0-9_-]{2,255}", "isolated source builder")
    if runner == builder:
        raise AWSAdapterError("The source builder and deployment runner must be separate projects.")
    _required(config.get("runner_repository"), REPOSITORY, "trusted runner repository")
    _required(config.get("runner_commit"), COMMIT, "trusted runner commit")
    _required(config.get("artifact_bucket"), r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", "artifact bucket")
    for key in ("cloudformation_role_arn",):
        _required(config.get(key), rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", key.replace("_", " "))
    _required(config.get("permissions_boundary_arn"), rf"arn:aws:iam::{account}:policy/[A-Za-z0-9+=,.@_/-]+", "application permissions boundary")
    if config.get("credentials_kms_key_arn"):
        raise AWSAdapterError("Custom Parameter Store KMS keys require a reviewed onboarding extension.")
    repositories = _value(platform, "allowed_repositories", [])
    if not isinstance(repositories, list) or not repositories or len(repositories) > 100:
        raise AWSAdapterError("Configure at least one approved repository.")
    for repository in repositories:
        _required(repository, REPOSITORY, "approved repository")
    archetypes = config.get("archetypes", list(ARCHETYPES))
    if not isinstance(archetypes, list) or not archetypes or any(item not in ARCHETYPES for item in archetypes):
        raise AWSAdapterError("Only the static, serverless and Fargate AWS archetypes are supported.")
    try:
        fargate_applications(config, account, region, repositories, prefix)
        academy_fargate_applications(config, account, region, repositories, prefix)
    except ContractError as error:
        raise AWSAdapterError(str(error)) from None
    if config.get('fargate_cloudformation_role_arn') or (config.get('fargate_apps') or config.get('academy_fargate_apps')):
        _required(config.get('fargate_cloudformation_role_arn'), rf'arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+', 'Fargate CloudFormation execution role')
    if (config.get('fargate_apps') or config.get('academy_fargate_apps')) and len(prefix) > 10:
        raise AWSAdapterError('Fargate platform prefixes use at most 10 characters to fit load-balancer names.')
    return {"account_id": account, "region": region, "prefix": prefix, "role_arn": role,
            "connection_arn": connection, "runner_project": runner, "allowed_repositories": repositories,
            "config": config}


def _snapshot(platform, snapshot):
    settings = configuration(platform, snapshot)
    if snapshot.get("repository") not in settings["allowed_repositories"]:
        raise AWSAdapterError("Repository is not approved for this deployment platform.")
    _required(snapshot.get("commit"), COMMIT, "application commit")
    _required(snapshot.get("app_identifier"), IDENTIFIER, "application identifier")
    _required(snapshot.get("environment"), SLUG, "environment")
    branch = snapshot.get("branch")
    if (not isinstance(branch, str) or not 1 <= len(branch) <= 200 or branch.startswith(("-", "/"))
            or re.search(r"[\s\x00-\x1f~^:?*\[\\]", branch) or ".." in branch or "@{" in branch):
        raise AWSAdapterError("Invalid application branch.")
    if snapshot.get("archetype") not in settings["config"].get("archetypes", ARCHETYPES):
        raise AWSAdapterError("Application archetype is not approved for this platform.")
    if snapshot.get('archetype') in ('amplify-fargate', 'amplify-academy-fargate') and snapshot['repository'] not in settings['config'].get('academy_fargate_apps' if snapshot['archetype'] == 'amplify-academy-fargate' else 'fargate_apps', {}):
        raise AWSAdapterError('Approve this repository’s Fargate resource references in platform settings first.')
    if snapshot.get("action", "provision") not in {"provision", "release"}:
        raise AWSAdapterError("Invalid deployment phase.")
    public = snapshot.get("public_config", {})
    if not isinstance(public, dict) or len(public) > 50:
        raise AWSAdapterError("Public configuration must be a bounded environment variable map.")
    for name, value in public.items():
        if (not isinstance(name, str) or not PUBLIC_VARIABLE.fullmatch(name)
                or re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY", name)
                or not isinstance(value, str) or len(value) > 4096 or "\x00" in value):
            raise AWSAdapterError("Only public VITE_ variables may be sent to the source builder.")
    expected_parameter = f"/{settings['prefix']}/apps/{snapshot['app_identifier']}/core-resource-client"
    if snapshot.get("secret_parameter", expected_parameter) != expected_parameter:
        raise AWSAdapterError("Core credential parameter is outside this application's namespace.")
    # Only allow reviewed fields into the runner payload. Credential values and
    # browser-supplied extras are never propagated into CodeBuild variables.
    payload = {key: snapshot.get(key) for key in (
        "app_identifier", "repository", "branch", "commit", "archetype", "environment", "plan_hash", "public_origin")}
    payload.update(action=snapshot.get("action", "provision"), public_config=public,
                   secret_parameter=expected_parameter, platform=settings)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 24000:
        raise AWSAdapterError("Deployment configuration is too large.")
    return settings, encoded


def _clients(platform, services, client_factory: Callable | None = None):
    if client_factory is not None:
        return {service: client_factory(service) for service in services}
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise AWSAdapterError("Install deployment/cloud-runner-requirements.txt to enable AWS deployments.") from None
    config = Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "total_max_attempts": 3})
    settings = configuration(platform)
    session = boto3.Session(region_name=settings["region"])
    arguments = {"RoleArn": settings["role_arn"], "RoleSessionName": "nce-deployment-console", "DurationSeconds": 900}
    external = _value(platform, "external_id", "")
    if external:
        arguments["ExternalId"] = external
    credentials = session.client("sts", config=config).assume_role(**arguments)["Credentials"]
    delegated = boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                            aws_secret_access_key=credentials["SecretAccessKey"],
                            aws_session_token=credentials["SessionToken"], region_name=settings["region"])
    identity = delegated.client("sts", config=config).get_caller_identity()
    if identity["Account"] != settings["account_id"]:
        raise AWSAdapterError("The assumed role belongs to a different AWS account.")
    return {service: delegated.client(service, config=config) for service in services}


def _safe_failure(error):
    if isinstance(error, AWSAdapterError):
        return error
    # SDK errors can echo invalid plaintext, signed URLs, and request bodies.
    # Preserve a small known error code only; never persist str(error).
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    allowed = {"AccessDenied", "AccessDeniedException", "ExpiredToken", "ExpiredTokenException",
               "ResourceNotFoundException", "InvalidInputException", "ValidationException", "ThrottlingException"}
    detail = f" ({code})" if code in allowed else ""
    return AWSAdapterError(f"AWS deployment operation failed{detail}. Check the scoped role and runner configuration.")


def verify(platform, *, client_factory=None):
    """Read-only verification. Does not create connections, builds, or resources."""
    settings = configuration(platform)
    try:
        clients = _clients(platform, ("sts", "codebuild", "codeconnections", "s3"), client_factory)
        if clients["sts"].get_caller_identity()["Account"] != settings["account_id"]:
            raise AWSAdapterError("AWS account does not match platform settings.")
        projects = clients["codebuild"].batch_get_projects(names=[settings["runner_project"], settings["config"]["build_project"]])
        by_name = {project["name"]: project for project in projects.get("projects", [])}
        if len(by_name) != 2 or projects.get("projectsNotFound"):
            raise AWSAdapterError("The deployment runner or isolated builder is missing.")
        for project in by_name.values():
            environment = project.get("environment", {})
            if environment.get("privilegedMode") or environment.get("type") != "LINUX_CONTAINER":
                raise AWSAdapterError("Use unprivileged Linux CodeBuild projects for these archetypes.")
            if (project.get("sourceVersion") != settings["config"]["runner_commit"]
                    or project.get("source", {}).get("location") != f"https://github.com/{settings['config']['runner_repository']}.git"):
                raise AWSAdapterError("The runner and source builder must use the approved immutable Core revision.")
        if by_name[settings["runner_project"]].get("serviceRole") == by_name[settings["config"]["build_project"]].get("serviceRole"):
            raise AWSAdapterError("Source builds must use a separate role without deployment permissions.")
        runner_compute = by_name[settings["runner_project"]]["environment"].get("computeType")
        builder_compute = by_name[settings["config"]["build_project"]]["environment"].get("computeType")
        if runner_compute != "BUILD_GENERAL1_SMALL" or builder_compute != "BUILD_GENERAL1_MEDIUM":
            raise AWSAdapterError("Use the onboarded Small runner and Medium source builder to avoid a single-slot build deadlock.")
        connection = clients["codeconnections"].get_connection(ConnectionArn=settings["connection_arn"])["Connection"]
        if connection.get("ConnectionStatus") != "AVAILABLE" or connection.get("ProviderType") != "GitHub":
            raise AWSAdapterError("Complete the GitHub connection authorization in AWS first.")
        clients["s3"].head_bucket(Bucket=settings["config"]["artifact_bucket"], ExpectedBucketOwner=settings["account_id"])
        return {"account_id": settings["account_id"], "region": settings["region"], "provider": "aws",
                "runner_project": settings["runner_project"], "build_project": settings["config"]["build_project"],
                "repositories": list(settings["allowed_repositories"]), "archetypes": list(settings["config"].get("archetypes", ARCHETYPES)),
                "message": "AWS account, separate runners, artifact bucket, and GitHub connection verified."}
    except Exception as error:
        raise _safe_failure(error) from None


def require_ci(platform, snapshot):
    """Use current operator credentials; never forward them to the AWS runner."""
    sources = (snapshot.get('repository', ''), (_value(platform, 'config', {}) or {}).get('runner_repository', ''))
    if not any(isinstance(source, str) and policy.repository.casefold() == source.casefold() for source in sources for policy in POLICIES):
        return []
    settings = configuration(platform, snapshot)
    try:
        token = _value(platform, 'ci_github_token', '')
    except Exception:
        raise CIGateError('The operator GitHub CI credential could not be decrypted.') from None
    return verify_deployment(snapshot['repository'], snapshot['commit'],
        settings['config']['runner_repository'], settings['config']['runner_commit'], token=token)


def start_run(platform, snapshot, idempotency_key, *, client_factory=None):
    settings, encoded = _snapshot(platform, snapshot)
    require_ci(platform, snapshot)
    token = hashlib.sha256(str(idempotency_key).encode()).hexdigest()
    result_key = f"runs/{token}/result.json"
    try:
        client = _clients(platform, ("codebuild",), client_factory)["codebuild"]
        result = client.start_build(
            projectName=settings["runner_project"], idempotencyToken=token,
            sourceVersion=settings["config"]["runner_commit"],
            secondarySourcesOverride=[{"type": "GITHUB", "location": f"https://github.com/{snapshot['repository']}.git",
                "sourceIdentifier": "APPLICATION", "gitCloneDepth": 1, "gitSubmodulesConfig": {"fetchSubmodules": False},
                "auth": {"type": "CODECONNECTIONS", "resource": settings["connection_arn"]}}],
            secondarySourcesVersionOverride=[{"sourceIdentifier": "APPLICATION", "sourceVersion": snapshot["commit"]}],
            timeoutInMinutesOverride=45, queuedTimeoutInMinutesOverride=30,
            environmentVariablesOverride=[{"name": "NCE_DEPLOYMENT", "type": "PLAINTEXT", "value": base64.b64encode(encoded.encode()).decode()},
                {"name": "NCE_RESULT_KEY", "type": "PLAINTEXT", "value": result_key},
                {"name": "NCE_RUN_TOKEN", "type": "PLAINTEXT", "value": token}],
        )
        build_id = result["build"]["id"]
        return {"build_id": build_id, "result_key": result_key, "run_token": token,
                "phase": snapshot.get("action", "provision"), "status": "running"}
    except Exception as error:
        raise _safe_failure(error) from None


def recover_run(platform, snapshot, idempotency_key, *, client_factory=None):
    """Recover an acknowledged intent without starting another billable build.

    CodeBuild's StartBuild token has a finite lifetime. A Core worker crash after
    AWS accepted a build must therefore be reconciled by read-only inspection,
    never by blindly resending StartBuild after its deduplication window.
    """
    settings, encoded = _snapshot(platform, snapshot)
    token = hashlib.sha256(str(idempotency_key).encode()).hexdigest()
    expected_payload = base64.b64encode(encoded.encode()).decode()
    try:
        client = _clients(platform, ("codebuild",), client_factory)["codebuild"]
        paginator = client.get_paginator("list_builds_for_project")
        matches = []
        for page in paginator.paginate(projectName=settings["runner_project"], sortOrder="DESCENDING",
                PaginationConfig={"MaxItems": 2000}):
            ids = page.get("ids", [])
            for offset in range(0, len(ids), 100):
                builds = client.batch_get_builds(ids=ids[offset:offset + 100]).get("builds", [])
                for build in builds:
                    variables = {item["name"]: item.get("value", "") for item in build.get("environment", {}).get("environmentVariables", [])}
                    if variables.get("NCE_RUN_TOKEN") != token:
                        continue
                    if variables.get("NCE_DEPLOYMENT") != expected_payload or build.get("sourceVersion") != settings["config"]["runner_commit"]:
                        raise AWSAdapterError("Recovered AWS job does not match the reviewed deployment plan.")
                    matches.append(build["id"])
        if len(matches) != 1:
            raise AWSAdapterError("AWS start outcome is ambiguous. Review the runner in AWS before retrying this deployment.")
        return {"build_id": matches[0], "result_key": f"runs/{token}/result.json", "run_token": token,
                "phase": snapshot.get("action", "provision"), "status": "running"}
    except Exception as error:
        raise _safe_failure(error) from None


def poll_run(platform, snapshot, provider_state, *, client_factory=None):
    settings, _ = _snapshot(platform, snapshot)
    state = dict(provider_state)
    _required(state.get("build_id"), rf"{re.escape(settings['runner_project'])}:[a-zA-Z0-9-]+", "runner job")
    _required(state.get("result_key"), r"runs/[a-f0-9]{64}/result\.json", "runner result")
    try:
        clients = _clients(platform, ("codebuild", "s3"), client_factory)
        response = clients["codebuild"].batch_get_builds(ids=[state["build_id"]])
        if not response.get("builds"):
            raise AWSAdapterError("AWS deployment job could not be found.")
        build = response["builds"][0]
        status = build.get("buildStatus", "IN_PROGRESS")
        steps = [{"name": "aws-runner", "status": "running" if status == "IN_PROGRESS" else
                 ("succeeded" if status == "SUCCEEDED" else "failed"), "message": "Trusted AWS deployment runner."}]
        if status == "IN_PROGRESS":
            return {"status": "running", "steps": steps, "outputs": {}, "provider_state": state}
        if status != "SUCCEEDED":
            return {"status": "failed", "steps": steps, "outputs": {}, "provider_state": state}
        response = clients["s3"].get_object(Bucket=settings["config"]["artifact_bucket"], Key=state["result_key"], ExpectedBucketOwner=settings["account_id"])
        body = response["Body"]
        try:
            raw = body.read(65537)
        finally:
            body.close()
        if len(raw) > 65536:
            raise AWSAdapterError("AWS deployment result is too large.")
        result = json.loads(raw)
        if result.get("commit") != snapshot["commit"] or result.get("action") != snapshot.get("action", "provision"):
            raise AWSAdapterError("AWS deployment result does not match the approved plan.")
        if result.get("status") != "succeeded":
            return {"status": "failed", "steps": steps, "outputs": {}, "provider_state": state}
        # Never forward raw CloudFormation outputs, presigned URLs, build log
        # links, or arbitrary runner messages to the browser.
        outputs = {key: value for key, value in result.get("outputs", {}).items()
                   if key in PUBLIC_OUTPUTS and isinstance(value, str) and len(value) <= (8192 if key == 'identity' else 2048)}
        if snapshot['archetype'] in ('amplify-fargate', 'amplify-academy-fargate'):
            approved = settings['config']['academy_fargate_apps' if snapshot['archetype'] == 'amplify-academy-fargate' else 'fargate_apps'][snapshot['repository']]
            if outputs.get('api_url') != 'https://' + approved['api_host']:
                raise AWSAdapterError('Fargate API output does not match the approved host.')
            try:
                identity_contract(json.loads(outputs.get('identity', 'null')))
            except (ContractError, ValueError):
                raise AWSAdapterError('Fargate identity receipt is invalid.') from None
        elif 'identity' in outputs:
            raise AWSAdapterError('This application archetype cannot change its identity contract.')
        if (not re.fullmatch(r"https://[a-z0-9-]+\.d[a-z0-9]+\.amplifyapp\.com", outputs.get("origin", ""))
                or not re.fullmatch(r"d[a-z0-9]{3,30}", outputs.get("amplify_app_id", ""))
                or not re.fullmatch(re.escape(settings["prefix"]) + r"-[a-f0-9]{12}-[a-f0-9]{8}", outputs.get("stack_name", ""))
                or (snapshot['archetype'] not in ('amplify-fargate', 'amplify-academy-fargate') and outputs.get("api_url") and not re.fullmatch(r"https://[a-z0-9]+\.execute-api\." + re.escape(settings["region"]) + r"\.amazonaws\.com", outputs["api_url"]))):
            raise AWSAdapterError("AWS returned an invalid public deployment output.")
        for name in result.get("completed_steps", []):
            if name in {"source-verified", "backend-provisioned", "backend-configured", "frontend-built", "frontend-published", 'database-migrated', 'services-healthy'}:
                steps.append({"name": name, "status": "succeeded", "message": name.replace("-", " ").capitalize() + "."})
        return {"status": "succeeded", "steps": steps, "outputs": outputs, "provider_state": state}
    except Exception as error:
        raise _safe_failure(error) from None


def store_credentials(platform, name, value, *, client_factory=None):
    """Controlled server-only credential write after a billable plan approval."""
    settings = configuration(platform)
    _required(name, rf"/{re.escape(settings['prefix'])}/apps/[A-Za-z0-9-]{{1,64}}/core-resource-client", "Core credential parameter")
    if not isinstance(value, dict) or set(value) != {"client_id", "client_secret"}:
        raise AWSAdapterError("Invalid Core resource credential bundle.")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in value.values()):
        raise AWSAdapterError("Invalid Core resource credential bundle.")
    encoded = json.dumps(value, separators=(",", ":"))
    if len(encoded.encode()) > 4096:
        raise AWSAdapterError("Core resource credentials exceed the Standard Parameter Store size limit.")
    try:
        client = _clients(platform, ("ssm",), client_factory)["ssm"]
        arguments = {"Name": name, "Value": encoded, "Type": "SecureString", "Tier": "Standard", "Overwrite": True}
        if settings["config"].get("credentials_kms_key_arn"):
            arguments["KeyId"] = settings["config"]["credentials_kms_key_arn"]
        response = client.put_parameter(**arguments)
        return {"parameter": name, "version": response["Version"]}
    except Exception as error:
        raise _safe_failure(error) from None


def stop_run(platform, snapshot, provider_state, *, client_factory=None):
    settings, _ = _snapshot(platform, snapshot)
    _required(provider_state.get("build_id"), rf"{re.escape(settings['runner_project'])}:[a-zA-Z0-9-]+", "runner job")
    try:
        _clients(platform, ("codebuild",), client_factory)["codebuild"].stop_build(id=provider_state["build_id"])
        return {"message": "Runner cancellation requested. Already provisioned resources remain retained."}
    except Exception as error:
        raise _safe_failure(error) from None
