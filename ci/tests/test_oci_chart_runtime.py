"""Admission and rendered-hook regressions for the application Helm package."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from jsonschema import Draft7Validator

from ci import oci_release as release


class ChartRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = release.load_config(release.ROOT / "ci/applications/proposal-studio.example.json")
        self.images = {name: image["repository"] + "@sha256:" + "c" * 64
                       for name, image in self.config["images"].items()}
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.chart, _ = release.generate_chart(self.config, self.images, "a" * 40, "b" * 40, self.folder.name)
        self.values = json.loads((self.chart / "values.yaml").read_text())
        overlay = json.loads((release.ROOT / self.config["validation_values_file"]).read_text())
        for key, value in overlay.items():
            self.values[key].update(value)
        self.validator = Draft7Validator(json.loads((self.chart / "values.schema.json").read_text()))
        self.validator.validate(self.values)

    def test_destination_cannot_replace_digests_or_add_components(self):
        for section, key, value in (("images", "api", self.config["images"]["api"]["repository"] + ":latest"),
                                    ("images", "unreviewed", self.images["api"]),
                                    ("workloads", "worker", self.values["workloads"]["api"])):
            with self.subTest(section=section, key=key):
                values = deepcopy(self.values)
                values[section][key] = value
                self.assertFalse(self.validator.is_valid(values))

    def test_destination_cannot_change_exact_release_commits_or_image_digests(self):
        for section, key, value in (("images", "api", self.config["images"]["api"]["repository"] + "@sha256:" + "d" * 64),
                                    ("releaseIdentity", "sourceCommit", "d" * 40),
                                    ("releaseIdentity", "runnerCommit", "d" * 40)):
            with self.subTest(section=section, key=key):
                values = deepcopy(self.values)
                values[section][key] = value
                self.assertFalse(self.validator.is_valid(values))

    def test_single_process_and_fixed_runtime_ports_survive_overlays(self):
        for component in ("api", "browser"):
            for key, value in (("replicas", 2), ("port", 9000),
                               ("command", ["uvicorn", "studio.main:app", "--workers", "4"])):
                with self.subTest(component=component, key=key):
                    values = deepcopy(self.values)
                    values["workloads"][component][key] = value
                    self.assertFalse(self.validator.is_valid(values))

    def test_destination_environment_cannot_shadow_fixed_bindings_or_inline_secrets(self):
        for key in ("DATABASE_URL", "SESSION_SECRET", "SAM_BROWSER_URL", "WEB_CONCURRENCY"):
            with self.subTest(key=key):
                values = deepcopy(self.values)
                values["external"]["apiEnvFromKeys"].append(key)
                self.assertFalse(self.validator.is_valid(values))
                values = deepcopy(self.values)
                values["external"]["apiEnvironment"][key] = "unreviewed-value"
                self.assertFalse(self.validator.is_valid(values))
        values = deepcopy(self.values)
        values["external"]["apiEnvironment"]["PROVIDER_ACCESS_KEY"] = "unreviewed-value"
        self.assertFalse(self.validator.is_valid(values))

    def _render(self, *, name="proposal-studio", values=None):
        if not shutil.which("helm"):
            self.skipTest("Helm is required for rendered chart regressions")
        target = Path(self.folder.name) / "overlay.json"
        target.write_text(json.dumps(values or self.values))
        result = subprocess.run(["helm", "template", name, str(self.chart), "-f", str(target)],
                                check=True, capture_output=True, text=True)
        return [document for document in result.stdout.split("\n---") if document.strip()]

    @staticmethod
    def _resource(documents, kind, name):
        return next(document for document in documents
                    if re.search(r"(?m)^kind: " + re.escape(kind) + r"$", document)
                    and re.search(r"(?m)^  name: " + re.escape(name) + r"$", document))

    def test_first_install_migration_has_earlier_identity_and_egress_hooks(self):
        documents = self._render()
        for kind in ("ServiceAccount", "NetworkPolicy"):
            resource = self._resource(documents, kind, "proposal-studio-migrate")
            self.assertIn("argocd.argoproj.io/hook: PreSync", resource)
            self.assertIn('argocd.argoproj.io/sync-wave: "-20"', resource)
            self.assertIn('helm.sh/hook-weight: "-20"', resource)
        migration = self._resource(documents, "Job", "proposal-studio-migrate-" + "a" * 12)
        self.assertIn('argocd.argoproj.io/sync-wave: "-10"', migration)
        self.assertIn("serviceAccountName: proposal-studio-migrate", migration)
        self.assertIn("fsGroup: 10001", migration)
        self.assertIn("backoffLimit: 0", migration)
        self.assertIn('command: ["python", "-m", "studio.migrate"]', migration)

    def test_external_database_egress_never_reaches_browser_policy(self):
        values = deepcopy(self.values)
        values["external"]["externalEgressCIDRs"] = ["10.20.30.40/32"]
        documents = self._render(values=values)
        for component in ("api", "migrate"):
            policy = self._resource(documents, "NetworkPolicy", "proposal-studio-" + component)
            self.assertIn("10.20.30.40/32", policy)
        browser = self._resource(documents, "NetworkPolicy", "proposal-studio-browser")
        self.assertNotIn("10.20.30.40/32", browser)
        self.assertNotIn("0.0.0.0/0", browser)
        values["networkPolicy"]["browserPublicEgress"] = True
        browser = self._resource(self._render(values=values), "NetworkPolicy", "proposal-studio-browser")
        self.assertIn("cidr: 0.0.0.0/0", browser)
        self.assertIn("169.254.0.0/16", browser)

    def test_long_release_name_keeps_job_within_kubernetes_name_limit(self):
        for document in self._render(name="p" * 53):
            name = re.search(r"(?m)^  name: ([a-z0-9-]+)$", document)
            self.assertIsNotNone(name)
            self.assertLessEqual(len(name.group(1)), 63)

    def test_runtime_has_fixed_worker_count_and_no_service_link_injection(self):
        documents = self._render()
        api = self._resource(documents, "Deployment", "proposal-studio-api")
        self.assertIn("enableServiceLinks: false", api)
        self.assertIn('name: WEB_CONCURRENCY\n              value: "1"', api)
        self.assertIn("startupProbe:", api)
        self.assertNotIn("kind: Secret\n", "\n".join(documents))


if __name__ == "__main__":
    unittest.main()
