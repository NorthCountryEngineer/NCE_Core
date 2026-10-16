"""Pinned, trusted CodeBuild deployment runner; never executes application code.

Run only in the onboarded deployment project. Frontend npm execution happens in
the separate artifact-only build project. Source, template, and IAM permissions
are bounded by the reviewed snapshot and platform onboarding roles.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import importlib.util
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
_contract_spec = importlib.util.spec_from_file_location('nce_cloud_contract', ROOT.parent.parent / 'platform_services' / 'cloud_contract.py')
_contract = importlib.util.module_from_spec(_contract_spec)
_contract_spec.loader.exec_module(_contract)
MAX_SOURCE_BYTES = 30 * 1024 * 1024
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


class RunnerError(Exception):
    """Only constant, sanitized messages are allowed to leave this runner."""


def child_directory(root, value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value):
        raise RunnerError("Invalid repository directory.")
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_dir():
        raise RunnerError("Repository directory must remain inside the checkout.")
    return target


def read_manifest(root, archetype):
    path = root / "nce-deploy.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
        raise RunnerError("A bounded nce-deploy.json manifest is required at the repository root.")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or set(manifest) - {"schema", "archetype", "frontend", "backend", "identity"}:
        raise RunnerError("Manifest contains unsupported deployment settings.")
    if manifest.get("schema") != 1 or manifest.get("archetype") != archetype:
        raise RunnerError("Repository manifest does not match the approved archetype.")
    frontend = manifest.get("frontend")
    if not isinstance(frontend, dict) or set(frontend) != {"directory", "output"}:
        raise RunnerError("Frontend manifest requires directory and output only.")
    source = child_directory(root, frontend["directory"])
    # Output is created by the isolated source builder, so it need not exist yet.
    output = frontend["output"]
    if (not isinstance(output, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", output)
            or not (source / output).resolve().is_relative_to(source)):
        raise RunnerError("Frontend output must remain inside its project directory.")
    if not (source / "package-lock.json").is_file() or not (source / "package.json").is_file():
        raise RunnerError("The frontend requires package.json and package-lock.json.")
    package = json.loads((source / "package.json").read_text())
    if not isinstance(package.get("scripts", {}).get("build"), str):
        raise RunnerError("The frontend must provide the standard npm build script.")
    if archetype == "amplify-serverless":
        backend = manifest.get("backend")
        if not isinstance(backend, dict) or set(backend) != {"directory", "handler"}:
            raise RunnerError("Serverless manifest requires backend directory and Python handler only.")
        child_directory(root, backend["directory"])
        if not isinstance(backend["handler"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,100}\.[A-Za-z_][A-Za-z0-9_]*", backend["handler"]):
            raise RunnerError("Invalid Python Lambda handler.")
    elif archetype == 'amplify-fargate':
        try:
            _contract.fargate_backend(manifest.get('backend'))
            _contract.identity_contract(manifest.get('identity'))
        except _contract.ContractError as error:
            raise RunnerError(str(error)) from None
    elif "backend" in manifest:
        raise RunnerError("Static applications cannot supply a backend recipe.")
    if archetype != 'amplify-fargate' and 'identity' in manifest:
        raise RunnerError('Only the Fargate recipe accepts an explicit identity contract.')
    return manifest


def package_lambda(root, backend, destination):
    source = child_directory(root, backend["directory"])
    total = 0
    count = 0
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise RunnerError("Lambda source must not contain symbolic links.")
            if path.is_dir():
                continue
            relative = path.relative_to(source)
            if any(part.startswith(".") or part in {"__pycache__", "node_modules", ".venv"} for part in relative.parts):
                continue
            if path.suffix not in {".py", ".json", ".txt", ".html", ".css"}:
                raise RunnerError("Lambda package contains an unsupported file type.")
            total += path.stat().st_size
            count += 1
            if total > MAX_SOURCE_BYTES or count > 5000:
                raise RunnerError("Lambda source exceeds the bounded packaging limit.")
            archive.write(path, str(relative))
    module = backend["handler"].rsplit(".", 1)[0].replace(".", "/") + ".py"
    with ZipFile(destination) as archive:
        if module not in archive.namelist():
            raise RunnerError("Lambda handler module is missing from the package.")


def stable_names(platform, snapshot):
    app = hashlib.sha256(snapshot["app_identifier"].encode()).hexdigest()[:12]
    environment = hashlib.sha256(snapshot["environment"].encode()).hexdigest()[:8]
    name = f"{platform['prefix']}-{app}-{environment}"
    storage = f"{platform['prefix']}-{platform['account_id']}-{app}-{environment}"
    return name, storage


def no_destructive_changes(changes):
    for change in changes:
        resource = change.get("ResourceChange", {})
        # ECS task definitions are immutable revisions, replaced during every
        # image update; replacing them is the required rolling-release behavior.
        immutable_task = resource.get('ResourceType') == 'AWS::ECS::TaskDefinition'
        if resource.get("Action") == "Remove" or (resource.get("Replacement") in {"True", "Conditional"} and not immutable_task):
            raise RunnerError("Deployment blocked: the infrastructure plan removes or replaces a resource.")


def stack_parameters(platform, snapshot, manifest, code_key, existing=False, activate=False):
    name, storage = stable_names(platform, snapshot)
    public = snapshot.get("public_config", {})
    values = {
        "ResourceName": name, "StorageName": storage, "Environment": snapshot["environment"],
        "PlatformPrefix": platform["prefix"], "ApplicationIdentifier": snapshot["app_identifier"],
        "PermissionsBoundaryArn": platform["config"]["permissions_boundary_arn"],
        "Archetype": snapshot["archetype"], "CoreCredentialsParameter": snapshot["secret_parameter"],
        "LambdaCodeBucket": platform["config"]["artifact_bucket"], "LambdaCodeKey": code_key,
        "LambdaHandler": manifest.get("backend", {}).get("handler", "handler.handler"),
        "ApplicationOrigin": public.get("VITE_APPLICATION_ORIGIN", "https://pending.invalid"),
        "CoreIssuer": public.get("VITE_CORE_ISSUER", public.get("VITE_NCE_CORE_URL", "")),
        "CoreOrganizationId": public.get("VITE_CORE_ORGANIZATION_ID", ""),
        "CoreServiceId": public.get("VITE_CORE_SERVICE_ID", ""),
    }
    identity_keys = {"ApplicationOrigin", "CoreIssuer", "CoreOrganizationId", "CoreServiceId"}
    if snapshot['archetype'] == 'amplify-fargate':
        approved = platform['config']['fargate_apps'][snapshot['repository']]
        try:
            _contract.fargate_applications(platform['config'], platform['account_id'], platform['region'], platform['allowed_repositories'], platform['prefix'])
            _contract.fargate_backend(manifest['backend'], approved)
        except _contract.ContractError as error:
            raise RunnerError(str(error)) from None
        for key in ['Archetype', 'LambdaCodeBucket', 'LambdaCodeKey', 'LambdaHandler']:
            values.pop(key)
        values.update(ApiImageUri=manifest['backend']['image'], BrowserImageUri=manifest['backend']['browser_image'],
            ApiRepositoryName=approved['image_repository'].split('/', 1)[1], BrowserRepositoryName=approved['browser_image_repository'].split('/', 1)[1],
            RuntimeSecretArn=approved['runtime_secret_arn'], CertificateArn=approved['certificate_arn'], ApiHost=approved['api_host'],
            CoreBrowserClientId=public.get('VITE_CORE_BROWSER_CLIENT_ID', ''),
            CoreRedirectUri=public.get('VITE_CORE_REDIRECT_URI', ''), CoreDeploymentId=public.get('VITE_CORE_DEPLOYMENT_ID', ''),
            DesiredCount='1' if activate else '0')
        values['ApplicationOrigin'] = public.get('VITE_APPLICATION_ORIGIN', snapshot.get('public_origin') or 'https://pending.invalid')
        # The first stack exists with count=0 before Core registration. Valid
        # placeholders satisfy template URL constraints without starting tasks
        # or impersonating an actual registered application.
        values['CoreIssuer'] = public.get('VITE_CORE_ISSUER') or 'https://pending.invalid'
        values['CoreRedirectUri'] = public.get('VITE_CORE_REDIRECT_URI') or values['ApplicationOrigin'] + manifest['identity']['callback_path']
        for key in ['CoreOrganizationId', 'CoreServiceId', 'CoreBrowserClientId', 'CoreDeploymentId']:
            values[key] = values[key] or 'pending'
        identity_keys.update({'CoreBrowserClientId', 'CoreRedirectUri', 'CoreDeploymentId', 'DesiredCount', 'ApiImageUri', 'BrowserImageUri'})
    parameters = []
    for key, value in values.items():
        preserve = existing and snapshot["action"] == "provision" and key in identity_keys
        parameters.append({"ParameterKey": key, **({"UsePreviousValue": True} if preserve else {"ParameterValue": value})})
    return parameters


class Runner:
    def __init__(self, snapshot, token, result_key, client_factory=None, sleeper=time.sleep):
        self.snapshot = snapshot
        self.platform = snapshot["platform"]
        self.config = self.platform["config"]
        self.token = token
        self.result_key = result_key
        self.steps = []
        self.outputs = {}
        self.sleeper = sleeper
        self.client_factory = client_factory
        self.clients = {}
        self.runtime_outputs = {}

    def client(self, service):
        if service not in self.clients:
            if self.client_factory:
                self.clients[service] = self.client_factory(service)
            else:
                import boto3
                from botocore.config import Config
                config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3})
                self.clients[service] = boto3.Session(region_name=self.platform["region"]).client(service, config=config)
        return self.clients[service]

    def wait_change_set(self, name, change_set):
        client = self.client("cloudformation")
        client.get_waiter("change_set_create_complete").wait(StackName=name, ChangeSetName=change_set, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        changes = []
        for page in client.get_paginator("describe_change_set").paginate(StackName=name, ChangeSetName=change_set):
            changes.extend(page.get("Changes", []))
        no_destructive_changes(changes)
        return changes

    def provision(self, manifest, root, activate=False):
        name, _ = stable_names(self.platform, self.snapshot)
        code_key = f"code/{name}/{self.snapshot['commit']}/api.zip"
        if self.snapshot["archetype"] == "amplify-serverless":
            archive = Path("/tmp/nce-api.zip")
            package_lambda(root, manifest["backend"], archive)
            self.client("s3").upload_file(str(archive), self.config["artifact_bucket"], code_key,
                ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "application/zip", "ExpectedBucketOwner": self.platform["account_id"]})
        client = self.client("cloudformation")
        # CloudFormation reports missing stacks as ValidationError, not a typed
        # ResourceNotFound exception. Handle only this specific absence.
        try:
            previous = client.describe_stacks(StackName=name)["Stacks"][0]
            existing = True
            if previous["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                raise RunnerError("Existing application stack needs operator recovery before another deployment.")
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") != "ValidationError" or "does not exist" not in response.get("Error", {}).get("Message", ""):
                raise
            existing = False
        operation_token = (hashlib.sha256((self.token + ('-activate' if activate else '-configure')).encode()).hexdigest()
                           if self.snapshot['archetype'] == 'amplify-fargate' else self.token)
        change_set = f"nce-{operation_token[:32]}"
        template = ROOT / ('fargate.template.json' if self.snapshot['archetype'] == 'amplify-fargate' else 'application.template.json')
        if not template.is_file() or template.stat().st_size > 51200:
            raise RunnerError('The pinned Core recipe template is missing or exceeds the inline limit.')
        arguments = {
            "StackName": name, "ChangeSetName": change_set, "ChangeSetType": "UPDATE" if existing else "CREATE",
            "TemplateBody": template.read_text(),
            "Parameters": stack_parameters(self.platform, self.snapshot, manifest, code_key, existing, activate),
            "Capabilities": ["CAPABILITY_NAMED_IAM"], "RoleARN": self.config['fargate_cloudformation_role_arn'] if self.snapshot['archetype'] == 'amplify-fargate' else self.config["cloudformation_role_arn"],
            "ClientToken": operation_token, "Tags": [{"Key": "nce:platform-prefix", "Value": self.platform["prefix"]},
                {"Key": "nce:application", "Value": self.snapshot["app_identifier"]}],
        }
        client.create_change_set(**arguments)
        try:
            changes = self.wait_change_set(name, change_set)
        except Exception as error:
            # Empty changesets are a successful idempotent reconfiguration.
            state = client.describe_change_set(StackName=name, ChangeSetName=change_set)
            reason = state.get("StatusReason", "")
            if existing and state.get("Status") == "FAILED" and ("didn't contain changes" in reason or "No updates are to be performed" in reason):
                changes = []
                # Repeated no-op releases must not consume the stack's finite
                # change-set quota. Only remove our own empty UPDATE plan; a
                # CREATE plan can own an empty stack and is not cleaned here.
                client.delete_change_set(StackName=name, ChangeSetName=change_set)
            else:
                raise error
        if changes:
            client.execute_change_set(StackName=name, ChangeSetName=change_set, ClientRequestToken=operation_token)
            client.get_waiter("stack_update_complete" if existing else "stack_create_complete").wait(
                StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        stack = client.describe_stacks(StackName=name)["Stacks"][0]
        raw = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
        if not re.fullmatch(r"d[a-z0-9]{3,30}", raw.get("AmplifyAppId", "")):
            raise RunnerError("The stack did not return a valid manual Amplify application.")
        self.outputs = {"origin": raw["Origin"], "amplify_app_id": raw["AmplifyAppId"], "stack_name": name,
                        "commit": self.snapshot["commit"], "archetype": self.snapshot["archetype"]}
        if raw.get("ApiUrl"):
            self.outputs["api_url"] = raw["ApiUrl"]
        if self.snapshot['archetype'] == 'amplify-fargate':
            self.outputs['identity'] = json.dumps(manifest['identity'], sort_keys=True, separators=(',', ':'))
            self.runtime_outputs = raw
        self.steps.append("backend-configured" if self.snapshot["action"] == "release" else "backend-provisioned")

    def migrate_and_activate(self, manifest, root):
        raw = self.runtime_outputs
        required = {'Cluster', 'MigrationTaskDefinition', 'PrivateSubnet1', 'PrivateSubnet2', 'TaskSecurityGroup', 'Service', 'WorkerService', 'BrowserService'}
        if not required <= raw.keys():
            raise RunnerError('Fargate recipe did not return its required runtime resources.')
        ecs = self.client('ecs')
        response = ecs.run_task(cluster=raw['Cluster'], taskDefinition=raw['MigrationTaskDefinition'],
            launchType='FARGATE', platformVersion='1.4.0', count=1,
            clientToken=hashlib.sha256((self.token + '-migration').encode()).hexdigest(),
            networkConfiguration={'awsvpcConfiguration': {'subnets': [raw['PrivateSubnet1'], raw['PrivateSubnet2']],
                'securityGroups': [raw['TaskSecurityGroup']], 'assignPublicIp': 'DISABLED'}})
        if response.get('failures') or len(response.get('tasks', [])) != 1:
            raise RunnerError('ECS did not start the controlled migration task.')
        task = response['tasks'][0]['taskArn']
        ecs.get_waiter('tasks_stopped').wait(cluster=raw['Cluster'], tasks=[task], WaiterConfig={'Delay': 15, 'MaxAttempts': 120})
        stopped = ecs.describe_tasks(cluster=raw['Cluster'], tasks=[task])
        containers = stopped.get('tasks', [{}])[0].get('containers', [])
        if not containers or any(container.get('exitCode') != 0 for container in containers):
            raise RunnerError('Database migration failed. Keep services stopped and review the migration task log.')
        self.steps.append('database-migrated')
        self.provision(manifest, root, activate=True)
        raw = self.runtime_outputs
        services = [raw['Service'], raw['WorkerService'], raw['BrowserService']]
        ecs.get_waiter('services_stable').wait(cluster=raw['Cluster'], services=services, WaiterConfig={'Delay': 15, 'MaxAttempts': 80})
        current = ecs.describe_services(cluster=raw['Cluster'], services=services)
        if current.get('failures') or len(current.get('services', [])) != 3 or any(
                service.get('runningCount') != 1 or service.get('desiredCount') != 1 for service in current['services']):
            raise RunnerError('Fargate services are not healthy after migration.')
        self.steps.append('services-healthy')

    def build_frontend(self, manifest):
        client = self.client("codebuild")
        public = dict(self.snapshot.get("public_config", {}))
        public["VITE_APPLICATION_ORIGIN"] = public.get("VITE_APPLICATION_ORIGIN", self.outputs["origin"])
        if self.outputs.get("api_url"):
            public["VITE_API_URL"] = self.outputs["api_url"]
        payload = base64.b64encode(json.dumps({"frontend": manifest["frontend"], "public_config": public}, separators=(",", ":")).encode()).decode()
        response = client.start_build(
            projectName=self.config["build_project"], sourceVersion=self.config["runner_commit"],
            idempotencyToken=hashlib.sha256((self.token + "frontend").encode()).hexdigest(),
            secondarySourcesOverride=[{"type": "GITHUB", "location": f"https://github.com/{self.snapshot['repository']}.git",
                "sourceIdentifier": "APPLICATION", "gitCloneDepth": 1, "gitSubmodulesConfig": {"fetchSubmodules": False},
                "auth": {"type": "CODECONNECTIONS", "resource": self.platform["connection_arn"]}}],
            secondarySourcesVersionOverride=[{"sourceIdentifier": "APPLICATION", "sourceVersion": self.snapshot["commit"]}],
            environmentVariablesOverride=[{"name": "NCE_BUILD_CONFIG", "type": "PLAINTEXT", "value": payload},
                {"name": "NCE_RUN_TOKEN", "type": "PLAINTEXT", "value": self.token}],
            timeoutInMinutesOverride=20, queuedTimeoutInMinutesOverride=15)
        job = response["build"]["id"]
        for _ in range(100):
            build = client.batch_get_builds(ids=[job])["builds"][0]
            status = build["buildStatus"]
            if status == "SUCCEEDED":
                self.steps.append("frontend-built")
                return f"builds/{self.token}/frontend.zip"
            if status != "IN_PROGRESS":
                raise RunnerError("The isolated frontend source build failed. Review its AWS build log.")
            self.sleeper(15)
        client.stop_build(id=job)
        raise RunnerError("Frontend build exceeded the bounded deployment timeout.")

    def publish_frontend(self, artifact_key):
        s3 = self.client("s3")
        metadata = s3.head_object(Bucket=self.config["artifact_bucket"], Key=artifact_key,
            ExpectedBucketOwner=self.platform["account_id"])
        if not 0 < metadata["ContentLength"] <= MAX_ARTIFACT_BYTES:
            raise RunnerError("Frontend artifact exceeds the deployment size limit.")
        archive = Path("/tmp/nce-frontend.zip")
        s3.download_file(self.config["artifact_bucket"], artifact_key, str(archive),
            ExtraArgs={"ExpectedBucketOwner": self.platform["account_id"]})
        with ZipFile(archive) as packed:
            if "index.html" not in packed.namelist():
                raise RunnerError("Frontend archive must contain index.html at its root.")
            total = 0
            for item in packed.infolist():
                path = Path(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise RunnerError("Frontend archive contains an unsafe path.")
                total += item.file_size
            if total > MAX_ARTIFACT_BYTES:
                raise RunnerError("Expanded frontend artifact exceeds the size limit.")
        amplify = self.client("amplify")
        app_id = self.outputs["amplify_app_id"]
        branch = self.snapshot["environment"]
        deployment = amplify.create_deployment(appId=app_id, branchName=branch)
        # AWS returns a one-use presigned upload URL. It stays inside this
        # process, never in a Core provider state, browser output, or build log.
        parsed = urlparse(deployment["zipUploadUrl"])
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".amazonaws.com"):
            raise RunnerError("Amplify returned an unexpected artifact upload endpoint.")
        with urlopen(Request(deployment["zipUploadUrl"], data=archive.read_bytes(), method="PUT",
                             headers={"Content-Type": "application/zip"}), timeout=120) as response:
            if response.status not in {200, 201, 204}:
                raise RunnerError("Amplify did not accept the frontend artifact.")
        job = deployment["jobId"]
        amplify.start_deployment(appId=app_id, branchName=branch, jobId=job)
        for _ in range(80):
            status = amplify.get_job(appId=app_id, branchName=branch, jobId=job)["job"]["summary"]["status"]
            if status == "SUCCEED":
                self.steps.append("frontend-published")
                return
            if status in {"FAILED", "CANCELLED"}:
                raise RunnerError("Amplify frontend publication failed. Review the AWS deployment job.")
            self.sleeper(15)
        amplify.stop_job(appId=app_id, branchName=branch, jobId=job)
        raise RunnerError("Amplify publication exceeded the bounded deployment timeout.")

    def write_result(self, status):
        document = {"status": status, "action": self.snapshot["action"], "commit": self.snapshot["commit"],
                    "outputs": self.outputs if status == "succeeded" else {}, "completed_steps": self.steps}
        self.client("s3").put_object(Bucket=self.config["artifact_bucket"], Key=self.result_key,
            Body=json.dumps(document, separators=(",", ":")).encode(), ContentType="application/json",
            ServerSideEncryption="AES256", ExpectedBucketOwner=self.platform["account_id"])

    def run(self, root):
        trusted = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
        if trusted != self.config['runner_commit']:
            raise RunnerError("Core runner checkout does not match the approved trusted runner commit.")
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
        if actual != self.snapshot["commit"]:
            raise RunnerError("Repository checkout does not match the approved application commit.")
        self.steps.append("source-verified")
        manifest = read_manifest(root, self.snapshot["archetype"])
        self.provision(manifest, root)
        if self.snapshot["action"] == "release":
            if self.snapshot['archetype'] == 'amplify-fargate':
                self.migrate_and_activate(manifest, root)
            self.publish_frontend(self.build_frontend(manifest))
        self.write_result("succeeded")


def main():
    runner = None
    try:
        snapshot = json.loads(base64.b64decode(os.environ["NCE_DEPLOYMENT"], validate=True))
        token = os.environ["NCE_RUN_TOKEN"]
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise RunnerError("Invalid deployment run token.")
        if os.environ["NCE_RESULT_KEY"] != f"runs/{token}/result.json":
            raise RunnerError("Invalid deployment result location.")
        platform = snapshot["platform"]
        expected_environment = {"NCE_PREFIX": platform["prefix"], "NCE_ARTIFACT_BUCKET": platform["config"]["artifact_bucket"],
            "NCE_CFN_ROLE_ARN": platform["config"]["cloudformation_role_arn"],
            "NCE_APP_BOUNDARY_ARN": platform["config"]["permissions_boundary_arn"],
            "NCE_BUILD_PROJECT": platform["config"]["build_project"]}
        if any(os.environ.get(key) != value for key, value in expected_environment.items()):
            raise RunnerError("Deployment snapshot does not match the onboarded runner settings.")
        if snapshot['archetype'] == 'amplify-fargate' and os.environ.get('NCE_FARGATE_CFN_ROLE_ARN') != platform['config'].get('fargate_cloudformation_role_arn'):
            raise RunnerError('Fargate recipe role does not match the onboarded runner.')
        runner = Runner(snapshot, token, os.environ["NCE_RESULT_KEY"])
        root = Path(os.environ["CODEBUILD_SRC_DIR_APPLICATION"]).resolve()
        runner.run(root)
        print("Core AWS deployment phase completed successfully.")
        return 0
    except Exception as error:
        if runner:
            try:
                runner.write_result("failed")
            except Exception:
                pass
        # Repository JSON and AWS SDK exceptions may carry sensitive data.
        print(str(error) if isinstance(error, RunnerError) else
              "Core AWS deployment failed. Check scoped permissions and the reviewed repository manifest.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
