import json
import secrets
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from identity.models import AuditEvent, Membership, Organization, User
from platform_services.billing import BillingNotConfigured, PROVIDERS
from platform_services.data import publish
from platform_services.kubernetes import deployment_spec
from platform_services.management.commands.core_worker import heartbeat_embedded
from platform_services.models import (
    AgentDefinition, BillingProfile, DataRecord, Dataset, Deployment, DeploymentConfiguration,
    Installation, MarketplacePackage, Onboarding, Pipeline, RunStep, RuntimeNode, Workflow, WorkflowRun, WorkloadCredential,
)
from platform_services.secrets import decrypt_bundle
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential, upgrade_installation
from platform_services.validation import validate_manifest, validate_schema
from platform_services.workflows import process_one


class PlatformTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="North", slug="north")
        cls.other = Organization.objects.create(name="South", slug="south")
        cls.admin = User.objects.create_user("platform-admin@example.test")
        cls.member = User.objects.create_user("platform-member@example.test")
        cls.developer = User.objects.create_user("developer@example.test", is_developer=True)
        cls.administrator = Membership.objects.create(organization=cls.organization, user=cls.admin, kind="administrator")
        Membership.objects.create(organization=cls.organization, user=cls.member)
        install_core(cls.organization)
        install_core(cls.other)
        cls.package = MarketplacePackage.objects.create(slug="reports-app", name="Reports App", kind="application", description="Reports",
            manifest={"image": "registry.example.test/reports@sha256:" + "a" * 64,
                "capabilities": ["data.read", "data.write", "agents.run", "workflows.run", "workflows.read"],
                "dependencies": ["core-workflows", "core-data"]})
        cls.installation = install_package(cls.organization, cls.package, cls.package.manifest["capabilities"])
        cls.other_installation = install_package(cls.other, cls.package, cls.package.manifest["capabilities"])
        cls.credential, cls.token = issue_workload_credential(cls.installation, "Test application")
        cls.other_credential, cls.other_token = issue_workload_credential(cls.other_installation, "Other application")
        cls.dataset = Dataset.objects.create(organization=cls.organization, slug="reports", name="Reports",
            schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False})
        cls.pipeline = Pipeline.objects.create(organization=cls.organization, slug="import", name="Import", dataset=cls.dataset,
            transforms=[{"operation": "rename", "fields": {"subject": "title"}}, {"operation": "select", "fields": ["title"]}])
        cls.selector = AgentDefinition.objects.create(organization=cls.organization, slug="select", name="Select", kind="select", configuration={"fields": ["title"]})
        cls.publisher = AgentDefinition.objects.create(organization=cls.organization, slug="publish", name="Publish", kind="publish", configuration={"dataset": "reports"})
        cls.workflow = Workflow.objects.create(organization=cls.organization, slug="publish-report", name="Publish report", steps=["select", "publish"])
        cls.config = DeploymentConfiguration.objects.create(organization=cls.organization, environment="development", namespace="north",
            application_origin="https://reports.example.test", auto_deploy=True)
        cls.runtime_token = "cr_" + secrets.token_urlsafe(48)
        cls.node = RuntimeNode.objects.create(organization=cls.organization, name="Cluster", namespace="north",
            credential_hash=WorkloadCredential.digest(cls.runtime_token))

    def api(self, path, method="GET", data=None, token=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token}"}
        if method == "GET":
            return self.client.get("/api/platform/v1" + path, data or {}, **kwargs)
        return self.client.post("/api/platform/v1" + path, data=json.dumps(data), content_type="application/json", **kwargs)

    def queued_run(self, request_id="run-1"):
        response = self.api("/workflows/publish-report/runs/", "POST", {"request_id": request_id, "input": {"title": "Report", "private": "removed"}})
        self.assertEqual(response.status_code, 202, response.content.decode())
        return WorkflowRun.objects.get(pk=response.json()["run_id"])

    def runtime_sync(self, reports=None, token=None):
        return self.api("/runtime/sync/", "POST", {"reports": reports or []}, token or self.runtime_token)

    def test_core_services_are_real_marketplace_installations_and_idempotent(self):
        self.assertEqual(self.organization.installations.filter(package__kind="core").count(), 5)
        self.assertEqual(Deployment.objects.filter(installation__organization=self.organization, target="embedded").count(), 5)
        install_core(self.organization)
        self.assertEqual(self.organization.installations.filter(package__kind="core").count(), 5)
        root = Deployment.objects.get(installation__organization=self.organization, installation__package__slug="core-platform")
        self.assertEqual(root.children.count(), 4)
        heartbeat_embedded()
        root.refresh_from_db()
        self.assertEqual(root.status, "running")
        root.last_seen = timezone.now() - timedelta(minutes=2)
        self.assertEqual(root.status, "stale")

    def test_installing_first_application_creates_its_core_controller(self):
        organization = Organization.objects.create(name="New customer", slug="new-customer")
        installation = install_package(organization, self.package, [])
        root = Deployment.objects.get(installation__organization=organization, installation__package__slug="core-platform")
        self.assertTrue(installation.enabled)
        self.assertEqual(root.children.count(), 3)

    def test_manifest_releases_are_snapshotted_and_grants_are_explicit(self):
        self.package.manifest["image"] = "registry.example.test/reports@sha256:" + "b" * 64
        self.package.save()
        self.installation.refresh_from_db()
        self.assertTrue(self.installation.manifest["image"].endswith("a" * 64))
        with self.assertRaises(ValidationError):
            install_package(self.organization, self.package, ["billing.charge"])
        for manifest in [{"image": "image:latest"}, {"capabilities": ["admin"]}, {"image": 1}, {"dependencies": ["arbitrary-code"]}]:
            with self.assertRaises(ValidationError):
                validate_manifest(manifest)

    def test_integration_discovery_is_bound_to_organization_and_current_capabilities(self):
        value = self.api("/integration/").json()
        self.assertEqual(value["organization_id"], str(self.organization.pk))
        self.assertEqual(value["service_id"], str(self.installation.service_id))
        self.assertIn("core-workflows", value["services"])
        self.assertEqual(self.api("/integration/", token="invalid").status_code, 401)
        self.assertEqual(self.api("/integration/", token=self.runtime_token).status_code, 401)

    def test_pipeline_transform_validation_idempotency_and_conflict(self):
        request = {"event_id": "event-1", "data": {"subject": "Hello", "discard": 1}}
        response = self.api("/pipelines/import/events/", "POST", request)
        self.assertEqual(response.status_code, 201)
        record = DataRecord.objects.get(pk=response.json()["record_id"])
        self.assertEqual(record.data, {"title": "Hello"})
        duplicate = self.api("/pipelines/import/events/", "POST", request)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["record_id"], record.pk)
        self.assertEqual(self.api("/pipelines/import/events/", "POST", {"event_id": "event-1", "data": {"subject": "Different"}}).status_code, 409)
        self.assertEqual(self.api("/pipelines/import/events/", "POST", {"event_id": "event-2", "data": {"subject": 123}}).status_code, 400)

    def test_dataset_consumption_cursor_and_tenant_boundaries(self):
        first, _ = publish(self.dataset, "1", {"title": "One"})
        second, _ = publish(self.dataset, "2", {"title": "Two"})
        page = self.api("/datasets/reports/records/", data={"limit": 1}).json()
        self.assertEqual(page["next_cursor"], first.pk)
        self.assertEqual(self.api("/datasets/reports/records/", data={"after": first.pk}).json()["records"][0]["id"], second.pk)
        self.assertEqual(self.api("/datasets/reports/records/", token=self.other_token).status_code, 404)
        self.assertEqual(self.api("/pipelines/import/events/", "POST", {"event_id": "x", "data": {}}, self.other_token).status_code, 404)
        self.assertEqual(self.api("/datasets/reports/records/", data={"after": -1}).status_code, 400)

    def test_capability_removal_module_disabling_and_revoked_credentials_take_effect(self):
        self.installation.granted_capabilities = []
        self.installation.save()
        self.assertEqual(self.api("/datasets/reports/records/").status_code, 403)
        self.installation.granted_capabilities = ["data.read"]
        self.installation.save()
        Installation.objects.filter(organization=self.organization, package__slug="core-data").update(enabled=False)
        self.assertEqual(self.api("/datasets/reports/records/").status_code, 403)
        self.credential.active = False
        self.credential.save()
        self.assertEqual(self.api("/integration/").status_code, 401)

    def test_schema_references_cannot_fetch_remote_resources(self):
        for value in [{"$ref": "https://internal.example.test/secret"}, {"properties": {"x": {"$dynamicRef": "file:///etc/passwd"}}}, {"type": "not-a-type"}]:
            with self.assertRaises(ValidationError):
                validate_schema(value)

    def test_workflow_executes_tools_and_publishes_record(self):
        run = self.queued_run()
        self.assertTrue(process_one())
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.output, {"title": "Report"})
        self.assertEqual(list(run.step_results.values_list("tool", flat=True)), ["select", "publish"])
        self.assertEqual(self.dataset.records.count(), 1)
        self.assertFalse(process_one())
        status = self.api(f"/runs/{run.pk}/").json()
        self.assertEqual(status["output"], {"title": "Report"})
        self.assertEqual(self.api(f"/runs/{run.pk}/", token=self.other_token).status_code, 404)

    def test_run_request_id_is_idempotent_and_cannot_be_reused(self):
        run = self.queued_run()
        response = self.api("/workflows/publish-report/runs/", "POST", {"request_id": "run-1", "input": {"title": "Report", "private": "removed"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], str(run.pk))
        response = self.api("/workflows/publish-report/runs/", "POST", {"request_id": "run-1", "input": {"title": "Other"}})
        self.assertEqual(response.status_code, 409)

    def test_worker_rechecks_authorization_before_execution(self):
        run = self.queued_run()
        WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=False)
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.dataset.records.count(), 0)
        self.assertEqual(run.error, "Authorization revoked.")

    def test_tool_capability_cannot_be_inherited_from_workflow_submission(self):
        self.installation.granted_capabilities = ["workflows.run"]
        self.installation.save()
        response = self.api("/workflows/publish-report/runs/", "POST", {"request_id": "x", "input": {"title": "Report"}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WorkflowRun.objects.count(), 0)

    def test_stale_lease_recovers_without_duplicating_completed_side_effects(self):
        run = self.queued_run()
        process_one()
        WorkflowRun.objects.filter(pk=run.pk).update(status="running", lease_until=timezone.now() - timedelta(seconds=1))
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.step_results.count(), 2)
        self.assertEqual(self.dataset.records.count(), 1)

    def test_rule_agent_selects_an_authorized_tool_and_traces_its_choice(self):
        agent = AgentDefinition(organization=self.organization, slug="router", name="Router", kind="rules", configuration={
            "rules": [{"field": "approved", "equals": True, "tool": "publish", "configuration": {"dataset": "reports"}}],
            "default": {"tool": "inspect"}})
        agent.full_clean()
        agent.save()
        response = self.api("/agents/router/runs/", "POST", {"request_id": "inspect", "input": {"title": "Draft"}})
        self.assertEqual(response.status_code, 202)
        process_one()
        run = WorkflowRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(run.step_results.get().tool, "inspect")
        self.assertEqual(run.output["field_count"], 1)
        agent.configuration["rules"][0]["tool"] = "shell"
        with self.assertRaises(ValidationError):
            agent.full_clean()

    def test_agents_and_workflows_reject_other_tenant_references(self):
        workflow = Workflow(organization=self.other, slug="foreign", name="Foreign", steps=["publish"])
        with self.assertRaises(ValidationError):
            workflow.full_clean()
        agent = AgentDefinition(organization=self.other, slug="foreign", name="Foreign", kind="publish", configuration={"dataset": "reports"})
        with self.assertRaises(ValidationError):
            agent.full_clean()

    def test_deployment_provisions_identity_and_encrypted_integration(self):
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        self.assertEqual(deployment.application.service_id, self.installation.service_id)
        self.assertEqual(deployment.application.redirect_uris, "https://reports.example.test/oauth/callback")
        bundle = decrypt_bundle(deployment.encrypted_bundle)
        self.assertNotIn(bundle["workload_token"], deployment.encrypted_bundle)
        self.assertNotIn(bundle["oidc_client_secret"], deployment.encrypted_bundle)
        self.assertTrue(WorkloadCredential.objects.filter(token_hash=WorkloadCredential.digest(bundle["workload_token"])).exists())
        manifests = deployment_spec(deployment)["manifests"]
        self.assertEqual({m["kind"] for m in manifests}, {"Secret", "Deployment", "Service", "Ingress"})
        workload = next(m for m in manifests if m["kind"] == "Deployment")
        container = workload["spec"]["template"]["spec"]["containers"][0]
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertIn({"name": "CORE_ORGANIZATION_ID", "value": str(self.organization.pk)}, container["env"])
        self.assertFalse(workload["spec"]["template"]["spec"]["automountServiceAccountToken"])

    def test_runtime_cannot_observe_or_manage_another_node(self):
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        response = self.runtime_sync()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deployments"][0]["deployment_id"], str(deployment.pk))
        report = {"deployment_id": str(deployment.pk), "revision": deployment.revision, "state": "running", "healthy": True}
        self.assertEqual(self.runtime_sync([report]).status_code, 200)
        deployment.refresh_from_db()
        self.assertTrue(deployment.healthy)
        node = RuntimeNode.objects.create(organization=self.other, name="Other", namespace="south", credential_hash=WorkloadCredential.digest("other-runtime"))
        self.assertEqual(self.runtime_sync([dict(report, state="failed", healthy=False)], "other-runtime").json()["deployments"], [])
        deployment.refresh_from_db()
        self.assertEqual(deployment.observed_state, "running")
        self.node.active = False
        self.node.save()
        self.assertEqual(self.runtime_sync().status_code, 401)

    def test_stale_runtime_revision_cannot_mark_new_revision_healthy(self):
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        deployment.revision = 2
        deployment.save()
        self.runtime_sync([{"deployment_id": str(deployment.pk), "revision": 1, "state": "running", "healthy": True}])
        deployment.refresh_from_db()
        self.assertFalse(deployment.healthy)

    def test_controller_is_in_inventory_and_can_be_adopted_without_replacing_its_config(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("adopt_controller", args=["north"]), {"node": str(self.node.pk), "resource_name": "core-controller"})
        self.assertEqual(response.status_code, 302)
        root = Deployment.objects.get(installation__organization=self.organization, installation__package__slug="core-platform")
        spec = deployment_spec(root)
        self.assertEqual(spec["mode"], "controller")
        self.assertNotIn("manifests", spec)
        self.client.post(reverse("deployment_action", args=["north", root.pk]), {"action": "restart"})
        root.refresh_from_db()
        self.assertEqual(root.action, "restart")

    def test_admin_panel_and_state_changes_are_scoped_and_csrf_protected(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("platform_dashboard", args=["north"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Core Platform")
        self.assertEqual(self.client.get(reverse("platform_dashboard", args=["south"])).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("platform_dashboard", args=["north"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("onboarding")).status_code, 403)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(reverse("issue_workload_key", args=["north", self.installation.pk])).status_code, 403)

    def test_portal_cannot_assign_other_tenant_dataset_or_runtime(self):
        other_dataset = Dataset.objects.create(organization=self.other, slug="other", name="Other")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("pipeline_add", args=["north"]), {"slug": "bad", "name": "Bad", "dataset": str(other_dataset.pk), "transforms": "[]", "active": "on"})
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(Pipeline.objects.filter(slug="bad").exists())

    def test_payment_adapters_are_inactive_and_never_charge(self):
        for provider in PROVIDERS.values():
            with self.assertRaises(BillingNotConfigured):
                provider.create_checkout()

    def test_developer_login_triggers_resumable_onboarding_and_five_steps(self):
        self.client.force_login(self.developer)
        self.assertRedirects(self.client.get("/"), reverse("onboarding"))
        self.assertEqual(self.client.post(reverse("onboarding"), {"name": "New Org", "slug": "new-org", "identity_name": "New Cloud"}).status_code, 302)
        state = Onboarding.objects.get(user=self.developer)
        self.assertEqual(state.step, 2)
        organization = state.organization
        self.assertTrue(organization.memberships.filter(user=self.developer, kind="administrator").exists())
        self.assertEqual(self.client.post(reverse("onboarding"), {
            "legal_name": "New Org LLC", "country": "us", "support_email": "support@example.test", "timezone": "America/New_York",
            "billing_email": "billing@example.test", "currency": "usd", "provider": "stripe"}).status_code, 302)
        self.assertEqual(BillingProfile.objects.get(organization=organization).status, "not_configured")
        self.assertEqual(self.client.post(reverse("onboarding"), {"environment": "staging", "namespace": "new-org",
            "application_origin": "https://new.example.test"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("onboarding"), {"name": ""}).status_code, 302)
        self.assertEqual(self.client.post(reverse("onboarding"), {"confirm": "on"}).status_code, 302)
        state.refresh_from_db()
        self.assertTrue(state.complete)
        self.assertEqual(organization.installations.count(), 5)
        self.assertEqual(self.client.get(reverse("onboarding")).status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(organization=organization, action="onboarding.completed").exists())

    def test_dev_query_parameter_cannot_grant_developer_permissions(self):
        self.client.force_login(self.member)
        response = self.client.get("/?dev=1")
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_developer)
        self.assertFalse(Onboarding.objects.filter(user=self.member).exists())

    def test_auto_deploy_requires_a_connected_existing_runtime(self):
        self.client.force_login(self.developer)
        org = Organization.objects.create(name="Developer Org", slug="developer-org")
        Membership.objects.create(organization=org, user=self.developer, kind="administrator")
        DeploymentConfiguration.objects.create(organization=org, environment="staging", namespace="developer-org",
            application_origin="https://developer.example.test", auto_deploy=True)
        state = Onboarding.objects.create(user=self.developer, organization=org, step=4)
        response = self.client.post(reverse("onboarding"), {"name": "Existing cluster"})
        self.assertEqual(response.status_code, 200)
        raw = response.context["runtime_token"]
        self.assertIn("no-store", response["Cache-Control"])
        state.refresh_from_db()
        self.assertEqual(state.step, 5)
        response = self.client.post(reverse("onboarding"), {"confirm": "on"})
        self.assertContains(response, "Connect the configured runtime")
        self.assertFalse(org.installations.exists())
        self.assertEqual(self.runtime_sync(token=raw).status_code, 200)
        self.assertEqual(self.client.post(reverse("onboarding"), {"confirm": "on"}).status_code, 302)
        state.refresh_from_db()
        self.assertTrue(state.complete)

    def test_release_upgrade_requires_explicit_review_and_reconciles_instances(self):
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        self.installation.manifest["capabilities"] = ["data.read"]
        self.installation.granted_capabilities = ["data.read"]
        self.installation.save()
        self.package.version = "2.0.0"
        self.package.manifest["image"] = "registry.example.test/reports@sha256:" + "b" * 64
        self.package.manifest["callback_path"] = "/sign-in/callback"
        self.package.save()
        self.client.force_login(self.admin)
        response = self.client.post(reverse("installation_settings", args=["north", self.installation.pk]),
                                    {"enabled": "on", "grants": ["data.write"]})
        self.assertContains(response, "Select a valid choice")
        self.installation.refresh_from_db()
        self.assertEqual(self.installation.granted_capabilities, ["data.read"])
        response = self.client.post(reverse("installation_upgrade", args=["north", self.installation.pk]),
                                    {"grants": ["data.read", "data.write"]})
        self.assertEqual(response.status_code, 302)
        self.installation.refresh_from_db()
        deployment.refresh_from_db()
        self.assertEqual(self.installation.version, "2.0.0")
        self.assertTrue(self.installation.manifest["image"].endswith("b" * 64))
        self.assertEqual(deployment.revision, 2)
        self.assertEqual(deployment.application.redirect_uris, "https://reports.example.test/sign-in/callback")

    def test_stop_revokes_access_and_start_rotates_only_this_instance_credential(self):
        from oauth2_provider.models import get_access_token_model
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        old = decrypt_bundle(deployment.encrypted_bundle)["workload_token"]
        get_access_token_model().objects.create(user=self.admin, application=deployment.application, token="stopped-access",
            expires=timezone.now() + timedelta(hours=1), scope="openid")
        self.client.force_login(self.admin)
        url = reverse("deployment_action", args=["north", deployment.pk])
        self.assertEqual(self.client.post(url, {"action": "stop"}).status_code, 302)
        self.assertEqual(self.api("/integration/", token=old).status_code, 401)
        self.assertEqual(self.api("/integration/").status_code, 200)
        self.assertFalse(get_access_token_model().objects.filter(token="stopped-access").exists())
        deployment.refresh_from_db()
        self.assertFalse(deployment.application.active)
        self.assertEqual(deployment_spec(deployment)["manifests"], [])
        self.client.post(url, {"action": "start"})
        deployment.refresh_from_db()
        new = decrypt_bundle(deployment.encrypted_bundle)["workload_token"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.api("/integration/", token=new).status_code, 200)
        self.client.post(url, {"action": "start"})
        self.assertEqual(self.api("/integration/", token=new).status_code, 401)
        self.assertEqual(self.installation.credentials.filter(name=f"deployment:{deployment.pk}", active=True).count(), 1)

    def test_disabling_installation_also_disables_its_federated_service(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("installation_settings", args=["north", self.installation.pk]), {"grants": ["data.read"]})
        self.installation.refresh_from_db()
        self.assertFalse(self.installation.service.active)
        self.assertEqual(self.api("/integration/").status_code, 401)

    def test_failed_run_retry_preserves_caller_and_completed_work(self):
        run = self.queued_run()
        WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=False)
        process_one()
        self.client.force_login(self.admin)
        self.client.post(reverse("retry_run", args=["north", run.pk]))
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.credential_id, self.credential.pk)
        self.assertIsNone(run.actor_id)
        WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=True)
        self.client.post(reverse("retry_run", args=["north", run.pk]))
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(self.dataset.records.count(), 1)

    def test_lost_onboarding_key_can_be_replaced_without_leaving_old_access(self):
        self.client.force_login(self.developer)
        Membership.objects.create(organization=self.organization, user=self.developer, kind="administrator")
        state = Onboarding.objects.create(user=self.developer, organization=self.organization, step=5, runtime=self.node)
        self.client.post(reverse("onboarding"), {"action": "reconnect"})
        state.refresh_from_db()
        self.assertEqual(state.step, 4)
        self.assertIsNone(state.runtime_id)
        self.assertEqual(self.runtime_sync().status_code, 401)
        response = self.client.post(reverse("onboarding"), {"name": "Replacement cluster"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runtime_sync(token=response.context["runtime_token"]).status_code, 200)

    def test_new_application_remains_a_child_of_adopted_controller(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("adopt_controller", args=["north"]), {"node": self.node.pk, "resource_name": "core-controller"})
        deployment = create_deployment(self.installation, self.node, "https://reports.example.test")
        self.assertIsNotNone(deployment.parent_id)
        self.assertEqual(deployment.parent.target, "kubernetes-controller")
