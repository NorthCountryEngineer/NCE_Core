"""Publication prerequisites for disposable container runtime verification."""
import json
import hashlib
import io
from pathlib import Path
import py_compile
import subprocess
import sys
import tarfile
import tempfile
import unittest

from ci.oci_release import ReleaseError
from ci.oci_runtime import CHROMIUM_PROBE, SOURCE_PROBE, WEB_SOURCE_PROBE, image_layer_source_exclusion, runtime_smoke


def image_archive(path, reference, layers=None, *, manifest_change=None):
    layers = layers or [[("app/studio/module.pyc", b"compiled-runtime")]]
    layer_paths = ["layers/" + str(index) + ".tar" for index in range(len(layers))]
    manifest = {"Config": "config.json", "RepoTags": [reference], "Layers": layer_paths}
    if manifest_change:
        manifest.update(manifest_change)
    def add(archive, name, data):
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    with tarfile.open(path, "w") as archive:
        add(archive, "manifest.json", json.dumps([manifest]).encode())
        add(archive, "config.json", b"{}")
        for name, entries in zip(layer_paths, layers):
            content = io.BytesIO()
            with tarfile.open(fileobj=content, mode="w") as layer:
                for filename, data in entries:
                    add(layer, filename, data)
            add(archive, name, content.getvalue())


class RuntimeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.images = {name: "registry.invalid/" + name + ":release" for name in ("api", "web", "browser")}
        self.fixture = "postgres@sha256:" + "a" * 64

    def execute(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "image", "save"]:
            image_archive(args[4], args[-1])
            return ""
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([self.fixture])
        if SOURCE_PROBE in args:
            return json.dumps({"source_packaging": "cpython-bytecode", "original_source_exclusion": "passed",
                               "compiled_modules": 7 if args[-1] == "api" else 4})
        if WEB_SOURCE_PROBE in args:
            return json.dumps({"source_packaging": "javascript-bundle", "original_source_exclusion": "passed"})
        return ""

    def test_synthetic_checks_precede_success_and_no_credentials_enter_containers(self):
        result = runtime_smoke(self.images, execute=self.execute,
                               env={"CORE_CI_GITHUB_TOKEN_FILE": "/private/file", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/private/role"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["database_fixture"], self.fixture)
        self.assertFalse(result["customer_services_accessed"])
        self.assertEqual(result["source_packaging"], "cpython-bytecode")
        self.assertEqual(result["original_source_exclusion"], "passed")
        self.assertEqual(result["frontend_packaging"], "javascript-bundle")
        self.assertEqual(result["layer_source_exclusion"], "passed")
        self.assertEqual(result["layer_counts"], {"api": 1, "browser": 1, "web": 1})
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
        package_checks = [index for index, args in enumerate(self.calls) if SOURCE_PROBE in args or WEB_SOURCE_PROBE in args]
        self.assertEqual(len(package_checks), 3)
        browser_package = next(index for index, args in enumerate(self.calls) if SOURCE_PROBE in args and args[-1] == "browser")
        chromium = next(index for index, args in enumerate(self.calls) if CHROMIUM_PROBE in args)
        self.assertLess(browser_package, chromium)
        self.assertTrue(any(args[-4:] == ["python", "-m", "scripts.migrate_reference_storage", "--help"] for args in self.calls))
        self.assertEqual(len([args for args in self.calls if args[:3] == ["docker", "rm", "--force"]]), 5)
        self.assertEqual(self.calls[-1][:3], ["docker", "network", "rm"])
        self.assertTrue(all(not Path(args[4]).exists() for args in self.calls if args[:3] == ["docker", "image", "save"]))

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

    def test_source_exclusion_failure_cleans_resources_and_prevents_success(self):
        def fail_source(args, **kwargs):
            output = self.execute(args, **kwargs)
            if SOURCE_PROBE in args and args[-1] == "api":
                raise ReleaseError("Original Python source remains")
            return output
        with self.assertRaises(ReleaseError):
            runtime_smoke(self.images, execute=fail_source, env={})
        runs = [args for args in self.calls if args[:2] == ["docker", "run"]]
        self.assertEqual(len(runs), 4)
        self.assertFalse(any(self.images["web"] in args for args in runs))
        self.assertEqual(len([args for args in self.calls if args[:3] == ["docker", "rm", "--force"]]), 4)
        self.assertEqual(self.calls[-1][:3], ["docker", "network", "rm"])

    def test_missing_source_verification_receipt_fails_closed(self):
        def no_evidence(args, **kwargs):
            output = self.execute(args, **kwargs)
            return "{}" if SOURCE_PROBE in args else output
        with self.assertRaisesRegex(ReleaseError, "has not passed"):
            runtime_smoke(self.images, execute=no_evidence, env={})
        self.assertEqual(self.calls[-1][:3], ["docker", "network", "rm"])

    def test_source_in_image_layer_stops_before_disposable_resources_or_publication(self):
        def source_layer(args, **kwargs):
            output = self.execute(args, **kwargs)
            if args[:3] == ["docker", "image", "save"]:
                image_archive(args[4], args[-1], [[("app/studio/original.py", b"proprietary source")]])
            return output
        with self.assertRaisesRegex(ReleaseError, "layer source exclusion"):
            runtime_smoke(self.images, execute=source_layer, env={})
        self.assertFalse(any(args[:2] in (["docker", "run"], ["docker", "push"]) for args in self.calls))
        self.assertFalse(any(args[:3] == ["docker", "network", "create"] for args in self.calls))
        self.assertTrue(all(not Path(args[4]).exists() for args in self.calls if args[:3] == ["docker", "image", "save"]))


class CompiledContentProbeTests(unittest.TestCase):
    def fixture(self, destination, component):
        required = {
            "api": ("studio/__init__.pyc", "studio/main.pyc", "studio/migrate.pyc", "studio/storage_setup.pyc",
                    "scripts/__init__.pyc", "scripts/migrate_reference_storage.pyc", "integrations/opportunity-readback/bridge.pyc"),
            "browser": ("studio/__init__.pyc", "studio/sam_browser_transport.pyc", "studio/sam_browser_worker.pyc", "studio/browser_service.pyc"),
        }
        root = Path(destination) / "app"
        root.mkdir()
        with tempfile.TemporaryDirectory() as source_folder:
            source = Path(source_folder) / "fixture.py"
            source.write_text("assert __debug__\nvalue = 'compiled runtime fixture'\n")
            for relative in required[component]:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                py_compile.compile(str(source), cfile=str(target), doraise=True, optimize=0)
        assets = {"LICENSE": b"Synthetic license notice\n"}
        if component == "api":
            assets.update({"CORE_CONTRACT.json": b"{}", "integrations/opportunity-readback/schema.json": b"{}"})
        for name, content in assets.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = {"schema_version": 1, "component": component, "python": sys.implementation.cache_tag,
                    "code": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required[component]},
                    "assets": {name: hashlib.sha256(content).hexdigest() for name, content in assets.items()}}
        (root / "runtime-package.json").write_text(json.dumps(manifest))
        return root, manifest

    def probe(self, root, component):
        return subprocess.run([sys.executable, "-c", SOURCE_PROBE, component, str(root)],
                              capture_output=True, text=True, timeout=10)

    def test_valid_runtime_bytecode_and_assets_pass(self):
        for component in ("api", "browser"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as folder:
                root, manifest = self.fixture(folder, component)
                result = self.probe(root, component)
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(result.stdout)
                self.assertEqual(evidence["compiled_modules"], len(manifest["code"]))
                self.assertEqual(evidence["original_source_exclusion"], "passed")

    def test_original_source_repository_and_test_entries_are_rejected(self):
        for name in ("studio/original.py", ".git/config", "tests/fixture.json", "studio/__pycache__/module.pyc", "studio/test_module.pyc", "source.zip"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root, _ = self.fixture(folder, "api")
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("must not ship")
                self.assertNotEqual(self.probe(root, "api").returncode, 0)

    def test_missing_compiled_module_cannot_be_hidden_by_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root, manifest = self.fixture(folder, "api")
            name = "studio/migrate.pyc"
            (root / name).unlink()
            del manifest["code"][name]
            (root / "runtime-package.json").write_text(json.dumps(manifest))
            self.assertNotEqual(self.probe(root, "api").returncode, 0)

    def test_wrong_bytecode_magic_is_rejected_even_with_matching_inventory_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root, manifest = self.fixture(folder, "browser")
            name = "studio/browser_service.pyc"
            target = root / name
            target.write_bytes(b"bad!" + target.read_bytes()[4:])
            manifest["code"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
            (root / "runtime-package.json").write_text(json.dumps(manifest))
            self.assertNotEqual(self.probe(root, "browser").returncode, 0)

    def test_missing_or_changed_inventory_and_assets_are_rejected(self):
        for change in ("manifest", "asset", "python"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                root, manifest = self.fixture(folder, "api")
                if change == "manifest":
                    (root / "runtime-package.json").unlink()
                elif change == "asset":
                    (root / "CORE_CONTRACT.json").write_text("changed")
                else:
                    manifest["python"] = "incompatible-interpreter"
                    (root / "runtime-package.json").write_text(json.dumps(manifest))
                self.assertNotEqual(self.probe(root, "api").returncode, 0)

    def test_web_probe_allows_compiled_bundles_and_public_documentation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<!doctype html>")
            (root / "app.js").write_text('(()=>{})();const literal="//# sourceMappingURL=data:example";')
            (root / "documentation.json").write_text("{}")
            result = subprocess.run(["sh", "-c", WEB_SOURCE_PROBE, "probe", str(root)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["original_source_exclusion"], "passed")

    def test_web_probe_rejects_original_sources_maps_and_repository_test_directories(self):
        for name in ("app.ts", "app.tsx", "app.jsx", "app.js.map", ".git/config", "tests/fixture.json", "__tests__/fixture.json", "source.zip"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "index.html").write_text("<!doctype html>")
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("must not ship")
                result = subprocess.run(["sh", "-c", WEB_SOURCE_PROBE, "probe", str(root)],
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)

    def test_web_probe_rejects_inline_source_map_comment_directives(self):
        examples = {
            "app.js": "(()=>{})();\n//# sourceMappingURL=data:application/json;base64,e30=\n",
            "joined.js": "(()=>{})();//# sourceMappingURL = data:application/json;base64,e30=\n",
            "style.css": ".application{}/*# sourceMappingURL=data:application/json;base64,e30= */",
        }
        for name, content in examples.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "index.html").write_text("<!doctype html>")
                (root / name).write_text(content)
                result = subprocess.run(["sh", "-c", WEB_SOURCE_PROBE, "probe", str(root)],
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)


class ImageLayerSourceTests(unittest.TestCase):
    def audit(self, layers, *, manifest_change=None):
        paths = []
        def execute(args, **kwargs):
            self.assertEqual(args[:4], ["docker", "image", "save", "--output"])
            paths.append(Path(args[4]))
            image_archive(args[4], args[-1], layers, manifest_change=manifest_change)
            return ""
        try:
            return image_layer_source_exclusion({"api": "registry.invalid/api:release"}, execute=execute, env={})
        finally:
            self.assertTrue(all(not path.exists() for path in paths))

    def test_compiled_payload_and_licensed_vendor_sources_are_allowed(self):
        layers = [[("usr/local/lib/python3.12/site-packages/vendor/module.py", b"licensed dependency source"),
                   ("usr/share/nginx/html/assets/application.js", b"compiled frontend")],
                  [("app/studio/main.pyc", b"compiled payload"), ("app/LICENSE", b"proprietary license")]]
        self.assertEqual(self.audit(layers), {"api": 2})

    def test_deleted_original_source_remains_rejected_in_lower_layer(self):
        with self.assertRaises(ReleaseError):
            self.audit([[("app/studio/main.py", b"original proprietary source")],
                        [("app/studio/.wh.main.py", b"")]])

    def test_frontend_sources_maps_and_repository_tests_are_rejected(self):
        for name in ("usr/share/nginx/html/app.ts", "usr/share/nginx/html/app.tsx", "usr/share/nginx/html/app.js.map",
                     "usr/share/nginx/html/.git/config", "usr/share/nginx/html/tests/fixture.json",
                     "app/tests/fixture.json", "app/studio/__pycache__/module.pyc", "app/source.zip"):
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                self.audit([[(name, b"must not ship")]])

    def test_mismatched_image_and_unsafe_archive_metadata_are_rejected(self):
        for change in ({"RepoTags": ["registry.invalid/other:release"]}, {"Layers": ["../layer.tar"]},
                       {"Config": "/absolute/config.json"}, {"Layers": []}):
            with self.subTest(change=change), self.assertRaises(ReleaseError):
                self.audit([[('app/studio/module.pyc', b'compiled payload')]], manifest_change=change)


if __name__ == "__main__":
    unittest.main()
