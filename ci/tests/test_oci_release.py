"""Offline release contract and fail-closed security tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci import oci_release as o
from ci.oci_feedback import FeedbackError, normalize_feedback

APP = "a" * 40
CORE = "b" * 40
DIGEST = "sha256:" + "c" * 64


class OCIReleaseTests(unittest.TestCase):
    def setUp(self):
        self.config = o.load_config(o.ROOT / "ci/applications/proposal-studio.example.json")
        self.images = {name: value["repository"] + "@" + DIGEST
                       for name, value in self.config["images"].items()}

    def _custom(self, value):
        with tempfile.TemporaryDirectory(dir=o.ROOT / "ci/applications") as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(value))
            return o.load_config(path)

    def test_chart_version_exact_release_identity(self):
        self.assertEqual(o.release_version("0.1.0", APP, CORE), "0.1.0-" + APP[:12] + ".r" + CORE[:8])
        with self.assertRaises(o.ReleaseError):
            o.release_version("latest", APP, CORE)

    def test_rejects_injected_secrets_and_extra_workloads(self):
        config = copy.deepcopy(self.config)
        config["values"]["external"]["apiEnvironment"]["SESSION_SECRET"] = "do-not-expose"
        with self.assertRaises(o.ReleaseError) as err:
            self._custom(config)
        self.assertNotIn("do-not-expose", str(err.exception))
        config = copy.deepcopy(self.config)
        config["values"]["workloads"]["worker"] = copy.deepcopy(config["values"]["workloads"]["api"])
        with self.assertRaises(o.ReleaseError):
            self._custom(config)

    def test_rejects_wrong_account_ecr(self):
        config = copy.deepcopy(self.config)
        config["images"]["api"]["repository"] = config["images"]["api"]["repository"].replace("000000000000", "111111111111")
        with self.assertRaises(o.ReleaseError):
            self._custom(config)

    def test_chart_references_only_exact_image_digests(self):
        with tempfile.TemporaryDirectory() as folder:
            chart, version = o.generate_chart(self.config, self.images, APP, CORE, folder)
            self.assertEqual(version, o.release_version("0.1.0", APP, CORE))
            values = json.loads((chart / "values.yaml").read_text())
            self.assertEqual(values["images"], self.images)
            self.assertEqual(values["releaseIdentity"]["sourceCommit"], APP)
            self.assertNotIn("SESSION_SECRET", json.dumps(values["external"]["apiEnvironment"]))
        bad = dict(self.images, api=self.config["images"]["api"]["repository"] + ":latest")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(o.ReleaseError):
            o.generate_chart(self.config, bad, APP, CORE, folder)

    def test_non_secret_template_security_and_argocd_migration(self):
        source = (o.CHART_SOURCE / "templates" / "workloads.yaml").read_text()
        for token in ("runAsNonRoot: true", "readOnlyRootFilesystem: true",
                      'capabilities: {drop: ["ALL"]}', "automountServiceAccountToken: false",
                      "seccompProfile: {type: RuntimeDefault}", "readinessProbe:", "livenessProbe:"):
            self.assertIn(token, source)
        migration = (o.CHART_SOURCE / "templates" / "migration.yaml").read_text()
        self.assertIn("argocd.argoproj.io/hook: PreSync", migration)
        self.assertIn("studio.migrate", migration)
        self.assertIn("backoffLimit: 0", migration)
        self.assertNotIn("kind: Secret", "\n".join(p.read_text() for p in (o.CHART_SOURCE / "templates").iterdir()))

    def test_helm_validation_failure_stops_packaging(self):
        with tempfile.TemporaryDirectory() as folder:
            chart, version = o.generate_chart(self.config, self.images, APP, CORE, folder)
            with patch.object(o, "execute", side_effect=o.ReleaseError("helm failed")) as cmd:
                with self.assertRaises(o.ReleaseError):
                    o.validate_chart(self.config, chart, version)
                self.assertEqual(cmd.call_count, 1)
            with patch.object(o, "execute", side_effect=["lint ok", "kind: Deployment", "pass"]) as cmd:
                result = o.validate_chart(self.config, chart, version)
                self.assertEqual(result["kubernetes_schema"], "passed")
                self.assertEqual([call.args[0][0] for call in cmd.call_args_list], ["helm", "helm", "kubeconform"])

    def test_no_publish_when_release_validation_fails(self):
        with patch.object(o, "ensure_source", return_value=(Path("/tmp/placeholder"), APP)), \
             patch.object(o, "checkout_commit", return_value=CORE), \
             patch.object(o, "admission", return_value=[{"run_id": 1}, {"run_id": 2}]), \
             patch.object(o, "image_references", return_value=self.images), \
             patch.object(o, "validate_chart", side_effect=o.ReleaseError("helm validation failed")), \
             patch.object(o, "package_chart") as package, patch.object(o, "publish_chart") as publish:
            with self.assertRaises(o.ReleaseError):
                o.release(o.ROOT / "ci/applications/proposal-studio.example.json", "/tmp/placeholder")
            package.assert_not_called()
            publish.assert_not_called()

    def test_publish_requires_matching_environment(self):
        with patch.dict("os.environ", {"CORE_CI_ALLOW_PUBLISH": "0", "CORE_CI_ENVIRONMENT": "staging"}):
            with self.assertRaises(o.ReleaseError):
                o.require_publish(self.config)
        with patch.dict("os.environ", {"CORE_CI_ALLOW_PUBLISH": "1", "CORE_CI_ENVIRONMENT": "production"}):
            with self.assertRaises(o.ReleaseError):
                o.require_publish(self.config)

    def test_publish_chart_requires_registry_digest(self):
        with patch.object(o, "execute", return_value="Pushed without receipt"), self.assertRaises(o.ReleaseError):
            o.publish_chart(self.config, Path("chart.tgz"), {})
        with patch.object(o, "execute", return_value="Pushed\nDigest: " + DIGEST):
            name, digest = o.publish_chart(self.config, Path("chart.tgz"), {})
            self.assertEqual(digest, DIGEST)
            self.assertEqual(name, self.config["registry"]["chart_repository"])

    def test_external_feedback_correlation_and_diagnostics(self):
        release = {"release_id": "rel-1", "images": self.images, "chart": {"oci_digest": DIGEST}}
        message = {"release_id": "rel-1", "deployment_id": "raft-123", "environment": "prod",
                   "platform": "rap", "images": self.images, "chart_digest": DIGEST,
                   "stage": "rollout", "status": "blocked", "failure_class": "secrets",
                   "diagnostic_ref": "CASE-123", "human_action": "update-secrets"}
        self.assertEqual(normalize_feedback(release, message), message)
        for change in ({"chart_digest": "sha256:" + "d" * 64},
                       {"diagnostic_ref": "https://user:password@example.com/logs?token=a"},
                       {"freeform_log": "sensitive payload"}):
            with self.subTest(change=change), self.assertRaises(FeedbackError):
                normalize_feedback(release, dict(message, **change))


if __name__ == "__main__":
    unittest.main()
