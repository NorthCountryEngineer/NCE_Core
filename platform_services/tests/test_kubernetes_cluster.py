"""Opt-in verification against a disposable, local kind cluster; never a remote cluster."""
import json
import os
import socket
import subprocess
import time
import uuid
from unittest import skipUnless
from unittest.mock import patch

import requests
from django.test import TestCase

from identity.models import Organization
from platform_services.kubernetes import deployment_spec
from platform_services.models import DeploymentConfiguration, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, upgrade_installation
from runtime_connector import KubernetesAdapter


@skipUnless(os.environ.get("NCE_TEST_KUBECONFIG") and os.environ.get("NCE_TEST_IMAGE"),
            "Requires the explicitly selected disposable local Kubernetes cluster.")
class LocalKubernetesTests(TestCase):
    def test_real_namespace_rbac_apply_health_restart_upgrade_and_stop(self):
        kubectl = os.environ.get("NCE_TEST_KUBECTL", "kubectl")
        namespace = "nce-smoke-" + uuid.uuid4().hex[:12]
        with patch.dict(os.environ, {"KUBECONFIG": os.environ["NCE_TEST_KUBECONFIG"]}):
            def command(*arguments, data=None):
                result = subprocess.run([kubectl, "--context", "kind-nce-platform-test", *arguments],
                    input=data, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, "Local Kubernetes command failed: " + result.stderr[:1000])
                return result.stdout

            config = json.loads(command("config", "view", "--minify", "-o", "json"))
            self.assertEqual(config["current-context"], "kind-nce-platform-test")
            self.assertTrue(config["clusters"][0]["cluster"]["server"].startswith("https://127.0.0.1:"),
                            "This test must never provision resources in a remote or billable cluster.")
            command("create", "namespace", namespace)
            try:
                rbac = subprocess.check_output([".venv/bin/python", "scripts/connector_rbac.py", "--namespace", namespace], text=True)
                command("apply", "-f", "-", data=rbac)
                principal = f"system:serviceaccount:{namespace}:core-runtime"
                organization = Organization.objects.create(slug=namespace, name="Local Kubernetes smoke")
                install_core(organization)
                DeploymentConfiguration.objects.create(organization=organization, environment="development", namespace=namespace,
                    application_origin="https://smoke.example.test", auto_deploy=True)
                node = RuntimeNode.objects.create(organization=organization, name="Local kind", namespace=namespace,
                    credential_hash=WorkloadCredential.digest(uuid.uuid4().hex))
                package = MarketplacePackage.objects.create(slug="kubernetes-smoke", name="Kubernetes smoke", kind="application", description="Local test application",
                    manifest={"image": os.environ["NCE_TEST_IMAGE"], "capabilities": ["data.read"], "dependencies": ["core-data"]})
                installation = install_package(organization, package, ["data.read"])
                deployment = create_deployment(installation, node, "https://smoke.example.test")
                adapter = KubernetesAdapter(namespace, "kind-nce-platform-test", kubectl)
                adapter.command[1:1] = ["--as", principal]

                def settle(state):
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        report = adapter.reconcile(deployment_spec(deployment))
                        self.assertNotEqual(report["state"], "failed", "Actual Kubernetes rejected reconciliation.")
                        if report["state"] == state:
                            return report
                        time.sleep(1)
                    self.fail("Actual Kubernetes did not reach " + state)

                self.assertTrue(settle("running")["healthy"])
                name = deployment_spec(deployment)["resource_name"]
                workload = json.loads(command("--namespace", namespace, "get", "deployment", name, "-o", "json"))
                self.assertFalse(workload["spec"]["template"]["spec"]["automountServiceAccountToken"])
                self.assertEqual(workload["spec"]["template"]["spec"]["containers"][0]["image"], os.environ["NCE_TEST_IMAGE"])
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                forwarding = subprocess.Popen([kubectl, "--context", "kind-nce-platform-test", "--namespace", namespace,
                    "port-forward", "service/" + name, f"{port}:80"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            response = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
                            break
                        except requests.RequestException:
                            if time.monotonic() >= deadline:
                                self.fail("The deployed application health endpoint was unreachable.")
                            time.sleep(0.2)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["organization_id"], str(organization.pk))
                    self.assertTrue(response.json()["integrated"])
                finally:
                    forwarding.terminate()
                    forwarding.wait(timeout=10)
                deployment.revision += 1
                deployment.save()
                self.assertTrue(settle("running")["healthy"])
                package.version = "2.0.0"
                package.save()
                upgrade_installation(installation, ["data.read"], actor=None)
                deployment.refresh_from_db()
                self.assertTrue(settle("running")["healthy"])
                # Namespace-scoped RBAC must deny deployment access elsewhere.
                denial = subprocess.run([kubectl, "--context", "kind-nce-platform-test", "--as", principal,
                    "--namespace", "kube-system", "get", "deployments"], capture_output=True, text=True, timeout=20)
                self.assertNotEqual(denial.returncode, 0)
                self.assertIn("Forbidden", denial.stderr)
                deployment.desired_state = "stopped"
                deployment.revision += 1
                deployment.save()
                self.assertFalse(settle("stopped")["healthy"])
            finally:
                command("delete", "namespace", namespace, "--wait=false")
