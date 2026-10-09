import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core_client import CoreClient, CoreServiceError
from runtime_connector import KubernetesAdapter, ReconciliationError, RuntimeConnector


class ConnectorTests(SimpleTestCase):
    def setUp(self):
        self.adapter = KubernetesAdapter("tenant", context="test-context")
        self.spec = {"deployment_id": "instance", "organization_id": "org", "namespace": "tenant", "resource_name": "core-instance",
                     "revision": 1, "desired_state": "running", "mode": "application", "manifests": [{"kind": "Deployment",
                     "metadata": {"name": "core-instance", "namespace": "tenant", "labels": {"core.platform/organization": "org"}}}]}
        self.ready = {"metadata": {"labels": {"core.platform/organization": "org"}, "generation": 2},
                      "spec": {"replicas": 1}, "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
                                                          "readyReplicas": 1, "availableReplicas": 1}}

    def test_apply_is_confined_and_requires_observed_readiness(self):
        with patch.object(self.adapter, "run", side_effect=[{}, {}, {}, self.ready]) as run:
            report = self.adapter.reconcile(self.spec)
            self.assertEqual(report["state"], "running")
            self.assertTrue(report["healthy"])
            apply = [c for c in run.call_args_list if c.args[0][0] == "apply"][0]
            self.assertIn("--server-side", apply.args[0])
        pending = {**self.ready, "status": {"observedGeneration": 1, "availableReplicas": 1}}
        with patch.object(self.adapter, "run", side_effect=[{}, {}, {}, pending]):
            self.assertEqual(self.adapter.reconcile(self.spec)["state"], "pending")
        old_ready = {**self.ready, "status": {**self.ready["status"], "updatedReplicas": 0}}
        with patch.object(self.adapter, "run", side_effect=[{}, {}, {}, old_ready]):
            self.assertEqual(self.adapter.reconcile(self.spec)["state"], "pending")

    def test_existing_resource_ownership_and_namespace_are_enforced(self):
        foreign = {"metadata": {"labels": {"core.platform/organization": "other"}}}
        with patch.object(self.adapter, "run", return_value=foreign) as run:
            self.assertEqual(self.adapter.reconcile(self.spec)["state"], "failed")
            self.assertEqual(run.call_count, 1)
        with patch.object(self.adapter, "run") as run:
            self.assertEqual(self.adapter.reconcile({**self.spec, "namespace": "other"})["state"], "failed")
            run.assert_not_called()

    def test_unapproved_objects_and_existing_secrets_cannot_be_overwritten(self):
        bad = {**self.spec, "manifests": [{"kind": "ClusterRole", "metadata": {"name": "core-instance", "namespace": "tenant"}}]}
        with patch.object(self.adapter, "run", return_value={}):
            self.assertEqual(self.adapter.reconcile(bad)["state"], "failed")
        foreign = {"metadata": {"labels": {"core.platform/organization": "other"}}}
        with patch.object(self.adapter, "run", side_effect=[{}, foreign]) as run:
            self.assertEqual(self.adapter.reconcile(self.spec)["state"], "failed")
            self.assertFalse(any(call.args[0][0] == "apply" for call in run.call_args_list))

    def test_stop_waits_for_old_pods_to_terminate(self):
        stopped = {**self.spec, "desired_state": "stopped", "manifests": []}
        with patch.object(self.adapter, "run", side_effect=[self.ready, {}, {"status": {"replicas": 1}}]):
            self.assertEqual(self.adapter.reconcile(stopped)["state"], "pending")
        with patch.object(self.adapter, "run", side_effect=[self.ready, {}, {"status": {"replicas": 0}}]):
            self.assertEqual(self.adapter.reconcile(stopped)["state"], "stopped")

    def test_controller_restart_preserves_configuration(self):
        spec = {**self.spec, "mode": "controller", "action": "restart", "revision": 2}
        with patch.object(self.adapter, "run", side_effect=[self.ready, {}, self.ready]) as run:
            report = self.adapter.reconcile(spec)
            self.assertEqual(report["state"], "running")
            patch_call = [call for call in run.call_args_list if call.args[0][0] == "patch"][0]
            self.assertEqual(patch_call.args[1], {"spec": {"template": {"metadata": {"annotations": {"core.platform/revision": "2"}}}}})
            self.assertFalse(any(call.args[0][0] == "apply" for call in run.call_args_list))

    def test_runtime_transports_reports_and_rejects_wrong_namespace_or_origin(self):
        connector = RuntimeConnector("https://core.example.test", "runtime-token", self.adapter)
        value = {"version": 1, "namespace": "tenant", "deployments": [self.spec]}
        with patch("runtime_connector.requests.post", return_value=Mock(status_code=200, json=lambda: value)) as post:
            with patch.object(self.adapter, "reconcile", return_value={"state": "running"}):
                connector.tick()
                connector.tick()
                self.assertEqual(post.call_args.kwargs["json"]["reports"], [{"state": "running"}])
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
        value["namespace"] = "other"
        with patch("runtime_connector.requests.post", return_value=Mock(status_code=200, json=lambda: value)):
            with self.assertRaises(ReconciliationError):
                connector.tick()
        with self.assertRaises(ValueError):
            RuntimeConnector("http://core.example.test", "runtime-token", self.adapter, allow_local_http=True)

    def test_kubernetes_secrets_are_passed_via_stdin_and_failures_do_not_leak_them(self):
        with patch("runtime_connector.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr="secret-value")) as run:
            with self.assertRaises(ReconciliationError) as error:
                self.adapter.run(["apply", "-f", "-"], {"token": "secret-value"})
            self.assertNotIn("secret-value", str(error.exception))
            self.assertNotIn("secret-value", " ".join(run.call_args.args[0]))
            self.assertIn("secret-value", run.call_args.kwargs["input"])

    def test_core_client_uses_injected_configuration_and_fails_closed(self):
        client = CoreClient("https://core.example.test/api/platform/v1", "workload-token", "org")
        good = {"organization_id": "org", "core_services_url": client.base_url}
        with patch("core_client.requests.request", return_value=Mock(status_code=200, json=lambda: good)):
            self.assertEqual(client.discover()["organization_id"], "org")
        with patch("core_client.requests.request", return_value=Mock(status_code=200, json=lambda: {**good, "organization_id": "other"})):
            with self.assertRaises(CoreServiceError):
                client.discover()
        with patch("core_client.requests.request", return_value=Mock(status_code=403)):
            with self.assertRaises(CoreServiceError):
                client.records("reports")
        with self.assertRaises(ValueError):
            client.records("../../other")
