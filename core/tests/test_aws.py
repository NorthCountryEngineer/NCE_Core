import importlib.util
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie

from core.aws import AWSOriginMiddleware
from deployment.aws_ssm import put_secure_parameter, signature_headers

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("aws_entrypoint", ROOT / "deployment/aws_entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


@ensure_csrf_cookie
def csrf_test_page(request):
    return HttpResponse("ok")


def csrf_failure(request, reason=""):
    return HttpResponse(status=403)


urlpatterns = [path("aws-test/", csrf_test_page)]


@override_settings(CORE_ORIGIN_SECRET="s" * 64)
class AWSBoundaryTests(SimpleTestCase):
    @override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=["core.example"], CSRF_TRUSTED_ORIGINS=["https://core.example"],
                       CSRF_FAILURE_VIEW=__name__ + ".csrf_failure",
                       CSRF_COOKIE_SECURE=True, MIDDLEWARE=["core.aws.AWSOriginMiddleware", "django.middleware.csrf.CsrfViewMiddleware"])
    def test_origin_boundary_preserves_django_csrf_enforcement_and_secure_cookie(self):
        client = Client(enforce_csrf_checks=True)
        headers = {"HTTP_HOST": "core.example", "HTTP_X_CORE_ORIGIN_SECRET": "s" * 64}
        page = client.get("/aws-test/", secure=True, **headers)
        self.assertTrue(page.cookies["csrftoken"]["secure"])
        token = page.cookies["csrftoken"].value
        good = client.post("/aws-test/", secure=True, HTTP_ORIGIN="https://core.example", HTTP_X_CSRFTOKEN=token, **headers)
        self.assertEqual(good.status_code, 200)
        bad = client.post("/aws-test/", secure=True, HTTP_ORIGIN="https://unregistered.example", HTTP_X_CSRFTOKEN=token, **headers)
        self.assertEqual(bad.status_code, 403)
        missing = client.post("/aws-test/", secure=True, HTTP_ORIGIN="https://core.example", **headers)
        self.assertEqual(missing.status_code, 403)

    def test_public_origin_requires_header_and_forces_no_shared_cache(self):
        factory = RequestFactory()
        boundary = AWSOriginMiddleware(lambda request: HttpResponse("private"))
        self.assertEqual(boundary(factory.get("/oauth/introspect/")).status_code, 403)
        self.assertEqual(boundary(factory.get("/", HTTP_X_CORE_ORIGIN_SECRET="wrong")).status_code, 403)
        response = boundary(factory.post("/", HTTP_X_CORE_ORIGIN_SECRET="s" * 64))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_static_paths_cannot_escape_collected_static_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "static"
            root.mkdir()
            (Path(directory) / "secret.txt").write_text("private")
            (root / "app.css").write_text("body {}")
            with override_settings(STATIC_ROOT=root, STATIC_URL="/static/"):
                boundary = AWSOriginMiddleware(lambda request: HttpResponse())
                factory = RequestFactory()
                self.assertEqual(boundary(factory.get("/static/../secret.txt", HTTP_X_CORE_ORIGIN_SECRET="s" * 64)).status_code, 404)
                response = boundary(factory.get("/static/app.css", HTTP_X_CORE_ORIGIN_SECRET="s" * 64))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                response.close()

    def test_health_check_is_only_unauthenticated_route_and_reports_db_failure(self):
        boundary = AWSOriginMiddleware(lambda request: HttpResponse("should not reach"))
        with patch("core.aws.connection") as mocked_connection:
            mocked_connection.cursor.side_effect = RuntimeError("DB down")
            self.assertEqual(boundary(RequestFactory().get("/health/")).status_code, 503)
        self.assertEqual(boundary(RequestFactory().post("/health/")).status_code, 403)

    def test_ecs_secrets_become_private_files_and_postgres_verifies_tls(self):
        environment = {"CORE_DJANGO_KEY": "django", "CORE_OIDC_PRIVATE_KEY": "pem", "CORE_ORIGIN_SECRET": "s" * 64,
                       "CORE_DB_PASSWORD": "x:y@z", "CORE_DB_HOST": "db.rds.amazonaws.com"}
        with tempfile.TemporaryDirectory() as directory:
            entrypoint.configure(environment, directory)
            self.assertNotIn("CORE_DJANGO_KEY", environment)
            self.assertNotIn("CORE_DB_PASSWORD", environment)
            self.assertIn("x%3Ay%40z", environment["DATABASE_URL"])
            self.assertIn("sslmode=verify-full", environment["DATABASE_URL"])
            self.assertEqual(stat.S_IMODE(Path(environment["CORE_SECRET_KEY_FILE"]).stat().st_mode), 0o600)
            self.assertEqual(entrypoint.command(["web"])[1], "core.wsgi:application")
            self.assertEqual(entrypoint.command(["manage", "migrate", "--noinput"])[2:], ["migrate", "--noinput"])

    def test_template_keeps_database_private_and_amplify_away_from_signing_keys(self):
        template = json.loads((ROOT / "infra/aws/core-fargate.template.json").read_text())
        resources = template["Resources"]
        self.assertFalse(resources["Database"]["Properties"]["PubliclyAccessible"])
        self.assertTrue(resources["Database"]["Properties"]["StorageEncrypted"])
        self.assertTrue(resources["Database"]["Properties"]["DeletionProtection"])
        self.assertEqual(resources["Database"]["DeletionPolicy"], "Snapshot")
        self.assertFalse(any(item["Type"] == "AWS::EC2::NatGateway" for item in resources.values()))
        policy = resources["AmplifyComputeRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
        self.assertEqual(policy["Resource"], {"Ref": "OriginSecret"})
        self.assertEqual(template["Parameters"]["DesiredCount"]["Default"], 0)
        web, worker = resources["TaskDefinition"]["Properties"]["ContainerDefinitions"]
        self.assertEqual([web["Command"], worker["Command"]], [["web"], ["worker"]])
        self.assertNotIn("CORE_BOOTSTRAP_PASSWORD", [item["Name"] for item in web["Secrets"]])
        registration = resources["RegistrationTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertEqual(registration[0]["Action"], "ssm:PutParameter")
        self.assertEqual(registration[1]["Action"], "kms:Encrypt")
        self.assertIn("kms:EncryptionContext:PARAMETER_ARN", registration[1]["Condition"]["StringEquals"])

    def test_registration_signature_uses_temporary_token_and_refuses_other_parameter(self):
        headers = signature_headers("us-east-1", {"AccessKeyId": "TEST", "SecretAccessKey": "test", "Token": "temporary"},
                                    b"{}", datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIn("20260101/us-east-1/ssm/aws4_request", headers["authorization"])
        self.assertEqual(headers["x-amz-security-token"], "temporary")
        # Cross-checked locally against @smithy/signature-v4 with the same inputs.
        self.assertTrue(headers["authorization"].endswith(
            "Signature=c7db1c5045f65cfccf1f4914b2d74248b8a4cfd9a7e2e13b0d3fe81c707a0289"))
        with patch.dict(os.environ, {"CORE_REGISTRATION_SSM_PARAMETER": "/axolotl-news/core-resource-client"}):
            with self.assertRaises(ValueError):
                put_secure_parameter("/another-app/credentials", "private")
