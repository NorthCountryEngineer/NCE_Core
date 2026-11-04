"""Publication prerequisites for disposable container runtime verification."""
import json
import unittest

from ci.oci_release import ReleaseError
from ci.oci_runtime import CHROMIUM_PROBE, runtime_smoke


class RuntimeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.images = {name: "registry.invalid/" + name + ":release" for name in ("api", "web", "browser")}
        self.fixture = "postgres@sha256:" + "a" * 64

    def execute(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([self.fixture])
        return ""

    def test_synthetic_checks_precede_success_and_no_credentials_enter_containers(self):
        result = runtime_smoke(self.images, execute=self.execute,
                               env={"CORE_CI_GITHUB_TOKEN_FILE": "/private/file", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/private/role"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["database_fixture"], self.fixture)
        self.assertFalse(result["customer_services_accessed"])
        runs = [args for args in self.calls if args[:2] == ["docker", "run"]]
        self.assertEqual(len(runs), 5)
        migration = next(index for index, args in enumerate(self.calls) if "studio.migrate" in args)
        api = next(index for index, args in enumerate(self.calls)
                   if args[:2] == ["docker", "run"] and args[-1] == self.images["api"])
        self.assertLess(migration, api)
        self.assertTrue(any(args[:4] == ["docker", "network", "create", "--internal"] for args in self.calls))
        for args in runs:
            self.assertIn("--read-only", args)
            self.assertIn("no-new-privileges:true", args)
            self.assertIn("--cap-drop", args)
            self.assertNotIn("--publish", args)
            self.assertNotIn("--volume", args)
            self.assertNotIn("--mount", args)
            self.assertNotIn("--env-file", args)
            for argument in args:
                self.assertNotIn("CORE_CI_GITHUB_TOKEN_FILE", argument)
                self.assertNotIn("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", argument)
        self.assertTrue(any("/api/health" in args[-2] and "-web:" in args[-2] for args in self.calls if len(args) > 2))
        self.assertTrue(any(CHROMIUM_PROBE in args for args in self.calls))
        self.assertEqual(len([args for args in self.calls if args[:3] == ["docker", "rm", "--force"]]), 5)
        self.assertEqual(self.calls[-1][:3], ["docker", "network", "rm"])

    def test_chromium_failure_cleans_every_started_container_and_stops_before_web(self):
        def fail_browser(args, **kwargs):
            output = self.execute(args, **kwargs)
            if CHROMIUM_PROBE in args:
                raise ReleaseError("Synthetic browser launch failed")
            return output

        with self.assertRaises(ReleaseError):
            runtime_smoke(self.images, execute=fail_browser, env={})
        runs = [args for args in self.calls if args[:2] == ["docker", "run"]]
        self.assertEqual(len(runs), 3)
        self.assertFalse(any(self.images["web"] in args for args in runs))
        self.assertEqual(len([args for args in self.calls if args[:3] == ["docker", "rm", "--force"]]), 3)
        self.assertEqual(self.calls[-1][:3], ["docker", "network", "rm"])

    def test_missing_fixture_identity_stops_before_network_or_container_creation(self):
        def no_digest(args, **kwargs):
            output = self.execute(args, **kwargs)
            return "[]" if args[:3] == ["docker", "image", "inspect"] else output

        with self.assertRaises(ReleaseError):
            runtime_smoke(self.images, execute=no_digest, env={})
        self.assertFalse(any(args[:2] == ["docker", "run"] for args in self.calls))
        self.assertFalse(any(args[:3] == ["docker", "network", "create"] for args in self.calls))

    def test_cleanup_failure_prevents_success_evidence(self):
        def fail_cleanup(args, **kwargs):
            output = self.execute(args, **kwargs)
            if args[:3] == ["docker", "network", "rm"]:
                raise ReleaseError("Synthetic cleanup failed")
            return output

        with self.assertRaisesRegex(ReleaseError, "could not be cleaned"):
            runtime_smoke(self.images, execute=fail_cleanup, env={})


if __name__ == "__main__":
    unittest.main()
