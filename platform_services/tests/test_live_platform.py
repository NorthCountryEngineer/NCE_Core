import os
import secrets
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.apps import apps
from django.test import LiveServerTestCase, override_settings

from core_client import CoreClient, CoreServiceError
from runtime_connector import RuntimeConnector
from identity.models import Organization
from platform_services.catalog import seed_catalog
from platform_services.models import AgentDefinition, Dataset, DeploymentConfiguration, MarketplacePackage, Pipeline, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential
from platform_services.workflows import process_one


class LivePlatformTests(LiveServerTestCase):
    def test_injected_client_consumes_real_pipeline_agent_and_runtime_apis(self):
        seed_catalog(apps, None)
        organization = Organization.objects.create(name="Live core organization", slug="live-core")
        install_core(organization)
        package = MarketplacePackage.objects.create(name="Live app", slug="live-app", kind="application", description="Live test",
            manifest={"image": "registry.example.test/live@sha256:" + "a" * 64, "capabilities": ["data.read", "data.write", "agents.run"],
                      "dependencies": ["core-agents", "core-data"]})
        installation = install_package(organization, package, package.manifest["capabilities"])
        credential, token = issue_workload_credential(installation, "Live consumer")
        dataset = Dataset.objects.create(organization=organization, slug="records", name="Records")
        Pipeline.objects.create(organization=organization, slug="ingest", name="Ingest", dataset=dataset)
        AgentDefinition.objects.create(organization=organization, slug="inspect", name="Inspect", kind="inspect")
        DeploymentConfiguration.objects.create(organization=organization, environment="development", namespace="live-core",
            application_origin="https://app.example.test")
        node_token = "cr_" + secrets.token_urlsafe(48)
        node = RuntimeNode.objects.create(organization=organization, namespace="live-core", name="Live runtime",
            credential_hash=WorkloadCredential.digest(node_token))
        deployment = create_deployment(installation, node, "https://app.example.test")
        with override_settings(ISSUER=self.live_server_url):
            with tempfile.TemporaryDirectory() as directory:
                token_file = Path(directory) / "credential"
                token_file.write_text(token)
                token_file.chmod(0o600)
                with patch.dict(os.environ, {"CORE_SERVICES_URL": self.live_server_url + "/api/platform/v1",
                    "CORE_WORKLOAD_TOKEN_FILE": str(token_file), "CORE_ORGANIZATION_ID": str(organization.pk), "CORE_ALLOW_LOCAL_HTTP": "1"}):
                    client = CoreClient.from_environment()
                    self.assertIn("core-data", client.discover()["services"])
                    self.assertTrue(client.ingest("ingest", "event", {"value": 42})["created"])
                    self.assertFalse(client.ingest("ingest", "event", {"value": 42})["created"])
                    self.assertEqual(client.records("records")["records"][0]["data"], {"value": 42})
                    run = client.run_agent("inspect", "request", {"value": 42})
                    process_one()
                    self.assertEqual(client.run_status(run["run_id"])["output"]["field_count"], 1)
                    credential.active = False
                    credential.save(update_fields=["active"])
                    with self.assertRaises(CoreServiceError):
                        client.records("records")
            adapter = Mock(namespace="live-core")
            adapter.reconcile.side_effect = lambda spec: {"deployment_id": spec["deployment_id"], "revision": spec["revision"], "state": "running", "healthy": True}
            connector = RuntimeConnector(self.live_server_url, node_token, adapter, allow_local_http=True)
            connector.run(once=True)
            deployment.refresh_from_db()
            self.assertEqual(deployment.observed_state, "running")
            self.assertTrue(deployment.healthy)
