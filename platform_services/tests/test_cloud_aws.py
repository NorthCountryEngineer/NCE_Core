"""Offline AWS contract tests. No AWS credentials, network, or paid builds."""
import base64
import importlib.util
import io
import json
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.conf import settings
from django.test import SimpleTestCase

from platform_services import cloud_aws


ROOT = Path(settings.BASE_DIR) / "deployment" / "aws-apps"
spec = importlib.util.spec_from_file_location("nce_test_trusted_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def platform():
    return SimpleNamespace(account_id="123456789012", region="us-east-1", prefix="nce-tests",
        role_arn="arn:aws:iam::123456789012:role/nce-control", external_id="external-id-for-offline-tests",
        connection_arn="arn:aws:codeconnections:us-east-1:123456789012:connection/abc-123",
        runner_project="nce-test-runner", allowed_repositories=["NorthCountryEngineer/example-app"],
        config={"runner_repository": "NorthCountryEngineer/NCE_Core", "runner_commit": "b" * 40,
            "build_project": "nce-test-builder", "artifact_bucket": "nce-test-artifacts",
            "cloudformation_role_arn": "arn:aws:iam::123456789012:role/nce-cfn",
            "permissions_boundary_arn": "arn:aws:iam::123456789012:policy/nce-boundary",
            "archetypes": ["amplify-static", "amplify-serverless"]})


def snapshot(value, action="provision"):
    return {"app_identifier": "12345678-1234-1234-1234-123456789012", "repository": value.allowed_repositories[0],
        "branch": "main", "commit": "a" * 40, "archetype": "amplify-serverless", "environment": "production",
        "action": action, "public_config": {}, "platform_config": dict(value.config)}


class AdapterTests(SimpleTestCase):
    def setUp(self):
        self.platform = platform()
        self.snapshot = snapshot(self.platform)
        self.clients = {name: Mock() for name in ("sts", "codebuild", "codeconnections", "s3", "ssm")}
        self.factory = self.clients.__getitem__

    def test_verification_is_read_only_and_checks_account_connection_and_role_separation(self):
        self.clients["sts"].get_caller_identity.return_value = {"Account": self.platform.account_id}
        self.clients["codebuild"].batch_get_projects.return_value = {"projects": [
            {"name": self.platform.runner_project, "serviceRole": "runner-role", "environment": {"type": "LINUX_CONTAINER", "privilegedMode": False, "computeType": "BUILD_GENERAL1_SMALL"},
             "sourceVersion": "b" * 40, "source": {"location": "https://github.com/NorthCountryEngineer/NCE_Core.git"}},
            {"name": self.platform.config["build_project"], "serviceRole": "builder-role", "environment": {"type": "LINUX_CONTAINER", "privilegedMode": False, "computeType": "BUILD_GENERAL1_MEDIUM"},
             "sourceVersion": "b" * 40, "source": {"location": "https://github.com/NorthCountryEngineer/NCE_Core.git"}}]}
        self.clients["codeconnections"].get_connection.return_value = {"Connection": {"ConnectionStatus": "AVAILABLE", "ProviderType": "GitHub"}}
        result = cloud_aws.verify(self.platform, client_factory=self.factory)
        self.assertEqual(result["repositories"], self.platform.allowed_repositories)
        self.assertNotIn(self.platform.external_id, json.dumps(result))
        self.assertFalse(self.clients["codebuild"].start_build.called)
        projects = self.clients["codebuild"].batch_get_projects.return_value["projects"]
        projects[1]["serviceRole"] = "runner-role"
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, "separate role"):
            cloud_aws.verify(self.platform, client_factory=self.factory)
        projects[1]["serviceRole"] = "builder-role"
        self.clients["sts"].get_caller_identity.return_value = {"Account": "999999999999"}
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, "account"):
            cloud_aws.verify(self.platform, client_factory=self.factory)

    def test_pinned_job_contains_only_reviewed_public_payload_and_no_buildspec_override(self):
        self.clients["codebuild"].start_build.return_value = {"build": {"id": "nce-test-runner:job-1"}}
        self.snapshot["untrusted_extra"] = {"client_secret": "should-never-leave-core"}
        result = cloud_aws.start_run(self.platform, self.snapshot, "job-1", client_factory=self.factory)
        arguments = self.clients["codebuild"].start_build.call_args.kwargs
        self.assertEqual(arguments["sourceVersion"], "b" * 40)
        self.assertEqual(arguments["secondarySourcesVersionOverride"][0]["sourceVersion"], "a" * 40)
        self.assertNotIn("buildspecOverride", arguments)
        self.assertNotIn("sourceLocationOverride", arguments)
        self.assertNotIn("privilegedModeOverride", arguments)
        variables = {item["name"]: item["value"] for item in arguments["environmentVariablesOverride"]}
        payload = json.loads(base64.b64decode(variables["NCE_DEPLOYMENT"]))
        self.assertNotIn("should-never-leave-core", json.dumps(payload))
        self.assertNotIn(self.platform.external_id, json.dumps(payload))
        self.assertEqual(result["result_key"], f"runs/{arguments['idempotencyToken']}/result.json")
        self.assertEqual(arguments["secondarySourcesOverride"][0]["auth"]["type"], "CODECONNECTIONS")

    def test_invalid_repository_commit_changed_settings_and_secret_variables_fail_before_aws(self):
        invalid = [("repository", "Attacker/not-allowed"), ("commit", "main"),
                   ("archetype", "amplify-container"), ("branch", "main; echo secret"),
                   ("public_config", {"VITE_CLIENT_SECRET": "hidden"}),
                   ("secret_parameter", "/another-app/core-resource-client"),
                   ("platform_config", {})]
        for key, value in invalid:
            with self.subTest(key=key):
                altered = {**self.snapshot, key: value}
                with self.assertRaises(cloud_aws.AWSAdapterError):
                    cloud_aws.start_run(self.platform, altered, "job-1", client_factory=self.factory)
        self.assertFalse(self.clients["codebuild"].start_build.called)

    def test_credentials_are_securestring_scoped_and_never_returned(self):
        self.clients["ssm"].put_parameter.return_value = {"Version": 1}
        name = f"/{self.platform.prefix}/apps/{self.snapshot['app_identifier']}/core-resource-client"
        result = cloud_aws.store_credentials(self.platform, name, {"client_id": "resource-id", "client_secret": "a-private-value"}, client_factory=self.factory)
        self.assertEqual(self.clients["ssm"].put_parameter.call_args.kwargs["Type"], "SecureString")
        self.assertEqual(self.clients["ssm"].put_parameter.call_args.kwargs["Tier"], "Standard")
        self.assertNotIn("a-private-value", json.dumps(result))
        with self.assertRaises(cloud_aws.AWSAdapterError):
            cloud_aws.store_credentials(self.platform, "/nce-other/apps/app/core-resource-client", {}, client_factory=self.factory)
        self.clients["ssm"].put_parameter.reset_mock()
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, "Standard Parameter Store"):
            cloud_aws.store_credentials(self.platform, name, {"client_id": "a" * 2048, "client_secret": "b" * 2048}, client_factory=self.factory)
        self.assertFalse(self.clients["ssm"].put_parameter.called)

    def test_sdk_exception_message_is_sanitized(self):
        error = RuntimeError("client_secret=a-private-value, https://signed-upload?secret=hidden")
        error.response = {"Error": {"Code": "AccessDeniedException", "Message": str(error)}}
        self.clients["codebuild"].start_build.side_effect = error
        with self.assertRaises(cloud_aws.AWSAdapterError) as caught:
            cloud_aws.start_run(self.platform, self.snapshot, "job-1", client_factory=self.factory)
        self.assertNotIn("a-private-value", str(caught.exception))
        self.assertNotIn("signed-upload", str(caught.exception))
        self.assertIn("AccessDeniedException", str(caught.exception))

    def test_poll_receipt_is_bounded_allowlisted_and_stream_closed(self):
        state = {"build_id": "nce-test-runner:job-1", "result_key": "runs/" + "c" * 64 + "/result.json"}
        self.clients["codebuild"].batch_get_builds.return_value = {"builds": [{"buildStatus": "SUCCEEDED"}]}
        body = io.BytesIO(json.dumps({"status": "succeeded", "commit": self.snapshot["commit"], "action": "provision",
            "outputs": {"origin": "https://production.dtest.amplifyapp.com", "amplify_app_id": "dtest",
                "stack_name": "nce-tests-123456789abc-12345678", "client_secret": "never-show-this"},
            "completed_steps": ["backend-provisioned", "https://signed-upload?hidden"]}).encode())
        self.clients["s3"].get_object.return_value = {"Body": body}
        result = cloud_aws.poll_run(self.platform, self.snapshot, state, client_factory=self.factory)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(body.closed)
        self.assertNotIn("never-show-this", json.dumps(result))
        self.assertNotIn("signed-upload", json.dumps(result))
        self.assertEqual(len(result["steps"]), 2)
        self.clients["s3"].get_object.return_value = {"Body": io.BytesIO(json.dumps({"status": "succeeded", "commit": "b" * 40, "action": "provision"}).encode())}
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, "approved plan"):
            cloud_aws.poll_run(self.platform, self.snapshot, state, client_factory=self.factory)

    def test_failure_does_not_fetch_or_expose_runner_logs(self):
        state = {"build_id": "nce-test-runner:job-1", "result_key": "runs/" + "c" * 64 + "/result.json"}
        self.clients["codebuild"].batch_get_builds.return_value = {"builds": [{"buildStatus": "FAILED", "logs": {"deepLink": "private-log"}}]}
        result = cloud_aws.poll_run(self.platform, self.snapshot, state, client_factory=self.factory)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.clients["s3"].get_object.called)
        self.assertNotIn("private-log", json.dumps(result))

    def test_crashed_start_recovers_matching_job_without_another_paid_start(self):
        response = {"build": {"id": "nce-test-runner:job-1"}}
        self.clients["codebuild"].start_build.return_value = response
        known = cloud_aws.start_run(self.platform, self.snapshot, "one-intent", client_factory=self.factory)
        arguments = self.clients["codebuild"].start_build.call_args.kwargs
        self.clients["codebuild"].reset_mock()
        self.clients["codebuild"].get_paginator.return_value.paginate.return_value = [{"ids": [known["build_id"]]}]
        self.clients["codebuild"].batch_get_builds.return_value = {"builds": [{"id": known["build_id"],
            "sourceVersion": arguments["sourceVersion"], "environment": {"environmentVariables": arguments["environmentVariablesOverride"]}}]}
        recovered = cloud_aws.recover_run(self.platform, self.snapshot, "one-intent", client_factory=self.factory)
        self.assertEqual(recovered["build_id"], known["build_id"])
        self.assertFalse(self.clients["codebuild"].start_build.called)
        self.clients["codebuild"].get_paginator.return_value.paginate.return_value = []
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, "ambiguous"):
            cloud_aws.recover_run(self.platform, self.snapshot, "one-intent", client_factory=self.factory)
        self.assertFalse(self.clients["codebuild"].start_build.called)


class RunnerContractTests(SimpleTestCase):
    def test_real_starter_manifests_are_loadable_and_lambda_handler_is_zipped_without_execution(self):
        static = runner.read_manifest(ROOT / "templates/static", "amplify-static")
        serverless = runner.read_manifest(ROOT / "templates/serverless", "amplify-serverless")
        self.assertEqual(static["frontend"]["output"], "dist")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "api.zip"
            with patch.object(runner.subprocess, "run", side_effect=AssertionError("Application code must never execute here")):
                runner.package_lambda(ROOT / "templates/serverless", serverless["backend"], archive)
            with ZipFile(archive) as packed:
                self.assertIn("handler.py", packed.namelist())

    def test_manifest_cannot_inject_commands_or_escape_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"scripts":{"build":"node build.mjs"}}')
            (root / "package-lock.json").write_text('{}')
            manifest = {"schema": 1, "archetype": "amplify-static", "frontend": {"directory": ".", "output": "dist"}}
            for change in ({"commands": ["curl attacker"]}, {"frontend": {"directory": "../", "output": "dist"}},
                           {"frontend": {"directory": ".", "output": "../../outside"}}):
                (root / "nce-deploy.json").write_text(json.dumps({**manifest, **change}))
                with self.assertRaises(runner.RunnerError):
                    runner.read_manifest(root, "amplify-static")

    def test_replacements_and_removals_are_blocked_while_additions_and_in_place_changes_pass(self):
        for change in ({"Action": "Remove"}, {"Action": "Modify", "Replacement": "True"},
                       {"Action": "Modify", "Replacement": "Conditional"}):
            with self.assertRaisesMessage(runner.RunnerError, "removes or replaces"):
                runner.no_destructive_changes([{"ResourceChange": change}])
        runner.no_destructive_changes([{"ResourceChange": {"Action": "Add"}},
                                      {"ResourceChange": {"Action": "Modify", "Replacement": "False"}}])

    def test_release_order_backend_before_isolated_frontend_before_publication(self):
        value = platform()
        _, encoded = cloud_aws._snapshot(value, snapshot(value, "release"))
        target = runner.Runner(json.loads(encoded), "c" * 64, "runs/" + "c" * 64 + "/result.json")
        order = []
        target.provision = Mock(side_effect=lambda *args: order.append("backend"))
        target.build_frontend = Mock(side_effect=lambda *args: order.append("isolated-build") or "builds/test/frontend.zip")
        target.publish_frontend = Mock(side_effect=lambda *args: order.append("publication"))
        target.write_result = Mock(side_effect=lambda *args: order.append("receipt"))
        with patch.object(runner.subprocess, "run", return_value=SimpleNamespace(stdout="a" * 40)), patch.object(runner, "read_manifest", return_value={}):
            target.run(ROOT)
        self.assertEqual(order, ["backend", "isolated-build", "publication", "receipt"])
        order.clear()
        target.snapshot["action"] = "provision"
        with patch.object(runner.subprocess, "run", return_value=SimpleNamespace(stdout="a" * 40)), patch.object(runner, "read_manifest", return_value={}):
            target.run(ROOT)
        self.assertEqual(order, ["backend", "receipt"])

    def test_existing_provision_preserves_identity_and_release_sets_exact_scoped_parameters(self):
        value = platform()
        document = snapshot(value)
        _, encoded = cloud_aws._snapshot(value, document)
        parsed = json.loads(encoded)
        manifest = runner.read_manifest(ROOT / "templates/serverless", "amplify-serverless")
        params = {item["ParameterKey"]: item for item in runner.stack_parameters(parsed["platform"], parsed, manifest, "code/api.zip", existing=True)}
        self.assertTrue(params["CoreIssuer"]["UsePreviousValue"])
        parsed["action"] = "release"
        parsed["public_config"] = {"VITE_CORE_ISSUER": "https://core.example.test", "VITE_APPLICATION_ORIGIN": "https://app.example.test"}
        params = {item["ParameterKey"]: item for item in runner.stack_parameters(parsed["platform"], parsed, manifest, "code/api.zip", existing=True)}
        self.assertEqual(params["ApplicationOrigin"]["ParameterValue"], "https://app.example.test")
        self.assertEqual(params["CoreIssuer"]["ParameterValue"], "https://core.example.test")

    def test_application_template_keeps_data_and_app_roles_scoped(self):
        template = json.loads((ROOT / "application.template.json").read_text())
        resources = template["Resources"]
        self.assertNotIn("Repository", resources["Frontend"]["Properties"])
        self.assertFalse(resources["FrontendBranch"]["Properties"]["EnableAutoBuild"])
        for resource in ("PrivateTable", "PublicTable", "Media"):
            self.assertEqual(resources[resource]["DeletionPolicy"], "Retain")
        role = resources["RuntimeRole"]["Properties"]
        self.assertEqual(role["PermissionsBoundary"], {"Ref": "PermissionsBoundaryArn"})
        policy = role["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertFalse(any(item.get("Resource") == "*" for item in policy))
        decrypt = next(item for item in policy if item.get("Action") == "kms:Decrypt")
        self.assertEqual(decrypt["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"], ["alias/aws/ssm"])
        self.assertEqual(decrypt["Condition"]["StringEquals"]["kms:EncryptionContext:PARAMETER_ARN"],
            {"Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${CoreCredentialsParameter}"})
        self.assertEqual(decrypt["Condition"]["StringEquals"]["kms:ViaService"], {"Fn::Sub": "ssm.${AWS::Region}.amazonaws.com"})
        self.assertEqual(resources["ApiInvoke"]["Properties"]["SourceAccount"], {"Ref": "AWS::AccountId"})

    def test_unchanged_existing_stack_is_successful_without_accumulating_change_sets(self):
        value = platform()
        document = snapshot(value)
        document["archetype"] = "amplify-static"
        _, encoded = cloud_aws._snapshot(value, document)
        client = Mock()
        client.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "Outputs": [
            {"OutputKey": "AmplifyAppId", "OutputValue": "dtest"},
            {"OutputKey": "Origin", "OutputValue": "https://production.dtest.amplifyapp.com"}]}]}
        client.describe_change_set.return_value = {"Status": "FAILED", "StatusReason": "The submitted information didn't contain changes."}
        target = runner.Runner(json.loads(encoded), "c" * 64, "runs/" + "c" * 64 + "/result.json", client_factory=lambda service: client)
        target.wait_change_set = Mock(side_effect=RuntimeError("Empty change set waiter failure"))
        manifest = runner.read_manifest(ROOT / "templates/static", "amplify-static")
        target.provision(manifest, ROOT / "templates/static")
        self.assertEqual(target.outputs["amplify_app_id"], "dtest")
        self.assertFalse(client.execute_change_set.called)
        client.delete_change_set.assert_called_once_with(StackName=target.outputs["stack_name"], ChangeSetName="nce-" + "c" * 32)
        self.assertFalse(client.delete_stack.called)


class StarterAPITests(SimpleTestCase):
    def setUp(self):
        sdk = Mock()
        sdk.Session.return_value.client.return_value.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"client_id": "resource-id", "client_secret": "a-private-value"})}}
        config = SimpleNamespace(Config=lambda **kwargs: kwargs)
        specification = importlib.util.spec_from_file_location("nce_test_starter_api", ROOT / "templates/serverless/api/handler.py")
        self.api = importlib.util.module_from_spec(specification)
        with patch.dict(sys.modules, {"boto3": sdk, "botocore.config": config}):
            specification.loader.exec_module(self.api)
        self.environment = patch.dict(self.api.os.environ, {
            "CORE_ISSUER": "https://core.example.test", "CORE_ORGANIZATION_ID": "1" * 36,
            "CORE_SERVICE_ID": "2" * 36, "CORE_CREDENTIALS_PARAMETER": "/nce/apps/app/core-resource-client",
            "APPLICATION_ORIGIN": "https://app.example.test", "PRIVATE_TABLE": "private-table", "NCE_APP_ID": "app"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.claims = {"active": True, "iss": "https://core.example.test", "organization_id": "1" * 36,
            "service_id": "2" * 36, "aud": "urn:core:service:" + "2" * 36, "sub": "3" * 36,
            "exp": self.api.time.time() + 300, "scope": "openid authorization", "token_type": "Bearer",
            "permissions": ["app-read", "app-write"]}

    def event(self, path="/notes", method="GET", authorization="Bearer access-token"):
        return {"rawPath": path, "headers": {"authorization": authorization, "origin": "https://app.example.test"},
            "requestContext": {"http": {"method": method}}, "body": '{"note":"private note"}'}

    def request(self, claims, event=None):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(claims).encode())
        with patch.object(self.api, "build_opener", return_value=opener):
            return self.api.handler(event or self.event(), None)

    def test_live_core_revocation_tenant_service_audience_and_permissions_are_enforced(self):
        self.api._dynamo.Table.return_value.get_item.return_value = {"Item": {"note": "private note"}}
        self.assertEqual(self.request(self.claims)["statusCode"], 200)
        changes = [("active", False), ("organization_id", "another-organization"),
                   ("service_id", "another-service"), ("aud", "another-audience"), ("exp", 0)]
        for name, value in changes:
            with self.subTest(name=name):
                self.assertEqual(self.request({**self.claims, name: value})["statusCode"], 401)
        self.assertEqual(self.request({**self.claims, "permissions": ["app-read"]}, self.event(method="POST"))["statusCode"], 403)
        self.assertEqual(self.request(self.claims, self.event(method="POST"))["statusCode"], 200)
        self.api._dynamo.Table.return_value.put_item.assert_called_once_with(Item={"id": "3" * 36, "note": "private note"})

    def test_options_needs_no_core_or_aws_credentials_and_unconfigured_api_fails_closed(self):
        result = self.api.handler(self.event(method="OPTIONS", authorization=""), None)
        self.assertEqual(result["statusCode"], 204)
        self.assertEqual(result["headers"]["Access-Control-Allow-Origin"], "https://app.example.test")
        self.assertFalse(self.api._ssm.get_parameter.called)
        self.assertEqual(self.api.handler(self.event(authorization=""), None)["statusCode"], 401)
        with patch.dict(self.api.os.environ, {"CORE_ISSUER": ""}):
            self.assertEqual(self.api.handler(self.event(), None)["statusCode"], 503)

    def test_http_api_payload_v1_and_v2_route_and_header_shapes_work(self):
        self.api._dynamo.Table.return_value.get_item.return_value = {"Item": {"note": "private note"}}
        older_event = {"version": "1.0", "path": "/notes", "httpMethod": "GET",
            "headers": {"Authorization": "Bearer access-token", "Origin": "https://app.example.test"},
            "requestContext": {"httpMethod": "GET"}}
        result = self.request(self.claims, older_event)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["headers"]["Access-Control-Allow-Origin"], "https://app.example.test")
        self.assertEqual(json.loads(result["body"])["note"], "private note")
        self.assertEqual(self.request(self.claims, self.event())["statusCode"], 200)
        self.api._ssm.get_parameter.reset_mock()
        older_event["httpMethod"] = "OPTIONS"
        self.assertEqual(self.api.handler(older_event, None)["statusCode"], 204)
        self.assertFalse(self.api._ssm.get_parameter.called)
