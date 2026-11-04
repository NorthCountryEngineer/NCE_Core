"""Offline release contract and fail-closed security tests."""
import copy
import base64
import hashlib
import io
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
        config["values"]["external"]["apiEnvironment"]["SESSION_SECRET"] = "do-not-expose"  # pragma: allowlist secret
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
            self.assertEqual(values["external"]["apiEnvironment"], {})
            self.assertEqual(values["external"]["databaseCredentialRef"], "")
            self.assertFalse(values["ingress"]["enabled"])
            self.assertNotIn("proposal.example.invalid", (chart / "values.yaml").read_text())
        bad = dict(self.images, api=self.config["images"]["api"]["repository"] + ":latest")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(o.ReleaseError):
            o.generate_chart(self.config, bad, APP, CORE, folder)

    def test_non_secret_template_security_and_argocd_migration(self):
        source = (o.CHART_SOURCE / "templates" / "workloads.yaml").read_text()
        for token in ("runAsNonRoot: true", "readOnlyRootFilesystem: true",
                      'capabilities: {drop: ["ALL"]}', "automountServiceAccountToken: false",
                      "seccompProfile: {type: RuntimeDefault}", "fsGroupChangePolicy: OnRootMismatch", "readinessProbe:", "livenessProbe:"):
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
            for policy in ("require_signatures", "require_sbom"):
                config = self._production()
                config[policy] = False
                with self.subTest(policy=policy), self.assertRaises(o.ReleaseError):
                    o.require_publish(config)

    def test_publish_chart_requires_registry_digest(self):
        with patch.object(o, "execute", return_value="Pushed without receipt"), self.assertRaises(o.ReleaseError):
            o.publish_chart(self.config, Path("chart.tgz"), {})
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "proposal-studio-0.1.0.tgz"
            artifact.write_bytes(b"validated-chart-package")
            with patch.object(o, "execute", return_value=""), \
                 patch.object(o, "registry_manifest", return_value=(DIGEST, {})), \
                 patch.object(o, "retrieve_chart", return_value=DIGEST) as retrieve:
                name, digest = o.publish_chart(self.config, artifact, {})
                self.assertEqual(digest, DIGEST)
                self.assertEqual(name, self.config["registry"]["chart_repository"])
                self.assertEqual(retrieve.call_args.args[3], "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest())

    def _production(self):
        config = copy.deepcopy(self.config)
        config["environment"] = "production"
        config["builder_project"] = "nce-proposal-oci-release"
        config["signing"] = {"key_uri": "awskms:///arn:aws:kms:us-east-1:000000000000:key/12345678-1234-1234-1234-123456789abc"}
        return config

    def _build_environment(self):
        return {"CODEBUILD_BUILD_ARN": "arn:aws:codebuild:us-east-1:000000000000:build/nce-proposal-oci-release:12345678-1234-1234-1234-123456789abc"}

    def test_signing_key_rejects_other_account_and_alias(self):
        config = self._production()
        self.assertEqual(self._custom(config)["signing"], config["signing"])
        for uri in (config["signing"]["key_uri"].replace("000000000000", "111111111111"),
                    "awskms:///alias/nce-release", "file://signing.key"):
            config["signing"]["key_uri"] = uri
            with self.subTest(uri=uri), self.assertRaises(o.ReleaseError):
                self._custom(config)

    def test_publish_preflight_rejects_wrong_identity_before_registry_access(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(o, "json_command", return_value={"Account": "111111111111"}) as commands:
            with self.assertRaises(o.ReleaseError):
                o.publish_preflight(self._production(), APP, CORE, folder, env=self._build_environment())
            self.assertEqual(commands.call_count, 1)

    def test_publish_preflight_requires_reviewed_codebuild_project(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(o, "json_command", return_value={"Account": "000000000000"}) as commands:
            with self.assertRaises(o.ReleaseError):
                o.publish_preflight(self._production(), APP, CORE, folder, env={"CODEBUILD_BUILD_ARN": ""})
            self.assertEqual(commands.call_count, 1)

    def _preflight_metadata(self, *, mutability="IMMUTABLE", existing=False):
        config = self._production()
        repositories = sorted({image["repository"] for image in config["images"].values()} |
                              {config["registry"]["chart_repository"]})
        responses = [{"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/nce-release/build"},
                     {"repositories": [{"repositoryUri": repository, "imageTagMutability": mutability}
                                       for repository in repositories]}]
        responses.extend([{"images": [{"imageId": {"imageDigest": DIGEST}}]} if existing else
                          {"images": [], "failures": [{"failureCode": "ImageNotFound"}]} for _ in repositories])
        responses.extend([{"LocationConstraint": None}, {"KeyMetadata": {
            "Arn": o.signing_key(config), "KeyState": "Enabled", "KeyUsage": "SIGN_VERIFY", "KeySpec": "ECC_NIST_P256"}}])
        return responses

    def test_publish_preflight_rejects_mutable_and_existing_artifacts(self):
        for change in ({"mutability": "MUTABLE"}, {"existing": True}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder, \
                 patch.object(o, "json_command", side_effect=self._preflight_metadata(**change)), \
                 patch.object(o, "execute") as command:
                with self.assertRaises(o.ReleaseError):
                    o.publish_preflight(self._production(), APP, CORE, folder, env=self._build_environment())
                command.assert_not_called()

    def test_publish_preflight_proves_private_kms_signing_before_publication(self):
        pem = "-----BEGIN PUBLIC KEY-----\nsynthetic-test-key\n-----END PUBLIC KEY-----"
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(o, "json_command", side_effect=self._preflight_metadata()), \
             patch.object(o, "execute", return_value=pem), \
             patch.object(o, "sign_blob") as sign, \
             patch.object(o, "tool_versions", return_value={"cosign": "2.5.3"}):
            evidence, public_key = o.publish_preflight(self._production(), APP, CORE, folder, env=self._build_environment())
            self.assertEqual(evidence["account_id"], "000000000000")
            self.assertEqual(evidence["codebuild_arn"], self._build_environment()["CODEBUILD_BUILD_ARN"])
            self.assertEqual(public_key.read_text(), pem + "\n")
            sign.assert_called_once()

    def test_registry_manifest_rejects_missing_and_mismatched_content(self):
        repository = self.config["registry"]["chart_repository"]
        raw = json.dumps({"schemaVersion": 2, "config": {"mediaType": o.HELM_CONFIG}, "layers": []})
        digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        metadata = {"images": [{"imageId": {"imageDigest": digest}, "imageManifest": raw}], "failures": []}
        with patch.object(o, "json_command", return_value=metadata):
            self.assertEqual(o.registry_manifest(self.config, repository, digest=digest)[0], digest)
            with self.assertRaises(o.ReleaseError):
                o.registry_manifest(self.config, repository, digest=DIGEST)
        for response in ({"images": [], "failures": [{"failureCode": "ImageNotFound"}]},
                         {"images": [{"imageId": {"imageDigest": DIGEST}, "imageManifest": raw}]}):
            with self.subTest(response=response), patch.object(o, "json_command", return_value=response), self.assertRaises(o.ReleaseError):
                o.registry_manifest(self.config, repository, tag="0.1.0")

    def test_chart_retrieval_checks_media_type_layer_and_package_content(self):
        package = b"synthetic-chart-tgz"
        digest = "sha256:" + hashlib.sha256(package).hexdigest()
        manifest = {"config": {"mediaType": o.HELM_CONFIG},
                    "layers": [{"mediaType": o.HELM_CONTENT, "digest": digest}]}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(o, "registry_manifest", return_value=(DIGEST, manifest)), \
             patch.object(o, "json_command", return_value={"downloadUrl": "https://example.invalid/package"}), \
             patch.object(o, "urlopen", return_value=io.BytesIO(package)):
            target = Path(folder) / "chart.tgz"
            self.assertEqual(o.retrieve_chart(self.config, self.config["registry"]["chart_repository"], DIGEST,
                                              digest, target), DIGEST)
            self.assertEqual(target.read_bytes(), package)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(o, "registry_manifest", return_value=(DIGEST, manifest)), \
             patch.object(o, "json_command") as download, self.assertRaises(o.ReleaseError):
            o.retrieve_chart(self.config, self.config["registry"]["chart_repository"], DIGEST,
                             DIGEST, Path(folder) / "chart.tgz")
        download.assert_not_called()

    def test_private_signature_and_sbom_attestation_match_exact_digest(self):
        reference = self.images["api"]
        payload = [{"critical": {"image": {"docker-manifest-digest": DIGEST}}}]
        with patch.object(o, "execute", return_value=json.dumps(payload)) as verify:
            o.verify_signature(reference, Path("public.pem"))
            self.assertIn("--insecure-ignore-tlog=true", verify.call_args.args[0])
        payload[0]["critical"]["image"]["docker-manifest-digest"] = "sha256:" + "d" * 64
        with patch.object(o, "execute", return_value=json.dumps(payload)), self.assertRaises(o.ReleaseError):
            o.verify_signature(reference, Path("public.pem"))
        predicate = {"spdxVersion": "SPDX-2.3", "packages": []}
        statement = {"predicate": predicate, "subject": [{"digest": {"sha256": DIGEST.removeprefix("sha256:")}}]}
        envelope = {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}
        with patch.object(o, "execute", return_value=json.dumps(envelope)):
            self.assertEqual(o.verify_attestation(reference, Path("public.pem"), predicate), statement)
            with self.assertRaises(o.ReleaseError):
                o.verify_attestation(reference, Path("public.pem"), {"packages": ["changed"]})

    def test_evidence_upload_is_conditional_and_retrieval_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "inventory.json"
            source.write_bytes(b"synthetic-spdx-evidence")
            def execute(args, **kwargs):
                if args[1:3] == ["s3api", "get-object"]:
                    Path(args[-1]).write_bytes(source.read_bytes())
                return "{}"
            with patch.object(o, "execute", side_effect=execute) as command:
                evidence = o.upload_evidence(self.config, source, "oci-releases/test/evidence.json")
                self.assertEqual(evidence["digest"], "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertIn("--if-none-match", command.call_args_list[0].args[0])
            with patch.object(o, "execute", return_value="{}"), self.assertRaises(o.ReleaseError):
                o.upload_evidence(self.config, source, "oci-releases/test/evidence.json")

    def test_runtime_verification_failure_prevents_every_image_push(self):
        with patch.object(o, "execute", return_value="") as commands, \
             patch("ci.oci_runtime.runtime_smoke", side_effect=o.ReleaseError("runtime validation failed")):
            with self.assertRaises(o.ReleaseError):
                o.image_references(self.config, APP, core_commit=CORE,
                    env={"source": Path("/tmp/synthetic-source"), "process": {}})
        invocations = [call.args[0] for call in commands.call_args_list]
        self.assertEqual(sum(args[:2] == ["docker", "build"] for args in invocations), 3)
        self.assertFalse(any(args[:2] == ["docker", "push"] for args in invocations))

    def test_external_feedback_correlation_and_diagnostics(self):
        release = {"release_id": "rel-1", "images": self.images, "chart": {"oci_digest": DIGEST}}
        message = {"release_id": "rel-1", "deployment_id": "raft-123", "environment": "prod",
                   "platform": "rap", "images": self.images, "chart_digest": DIGEST,
                   "stage": "rollout", "status": "blocked", "failure_class": "secrets",
                   "diagnostic_ref": "CASE-123", "human_action": "update-secrets"}
        self.assertEqual(normalize_feedback(release, message), message)
        for change in ({"chart_digest": "sha256:" + "d" * 64},
                       {"diagnostic_ref": "https://" + "user:" + "password@" + "example.com/logs?token=a"},
                       {"freeform_log": "sensitive payload"}):
            with self.subTest(change=change), self.assertRaises(FeedbackError):
                normalize_feedback(release, dict(message, **change))


if __name__ == "__main__":
    unittest.main()
