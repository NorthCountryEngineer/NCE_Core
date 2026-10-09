import base64
import io
import json
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from identity.access import Principal, decision, require_access
from identity.models import (Application, AuditEvent, AuthenticationEvidence, AuthenticationPolicy, Membership, Organization,
    PermissionTag, ProtectedResource, Role, Service, ServicePermission, User)
from platform_services.models import AgentDefinition, Dataset, DeploymentConfiguration, MarketplacePackage, Pipeline, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential
from platform_services.workflows import enqueue, process_one, snapshot_agents


class PermissionTagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Protected organization", slug="protected")
        cls.other = Organization.objects.create(name="Other", slug="other-protected")
        cls.user = User.objects.create_user("security-admin@example.test", "long-test-password")
        cls.member = Membership.objects.create(user=cls.user, organization=cls.org, kind="administrator")
        cls.tag = PermissionTag.objects.create(organization=cls.org, slug="phi", name="Health records", classification="restricted",
            requires_mfa=True, max_auth_age=300, allowed_purposes=["treatment"])
        cls.dataset = Dataset.objects.create(organization=cls.org, slug="health", name="Private health dataset")
        cls.dataset.permission_tags.add(cls.tag)
        cls.evidence = AuthenticationEvidence.objects.create(user=cls.user, authenticated_at=timezone.now(), mfa=True)
        install_core(cls.org)
        cls.package = MarketplacePackage.objects.create(slug="health-application", name="Health application", kind="application", description="Test",
            manifest={"image": "registry.example.test/health@sha256:" + "a" * 64, "capabilities": ["data.read", "data.write", "agents.run"]})
        cls.installation = install_package(cls.org, cls.package, cls.package.manifest["capabilities"])
        cls.credential, cls.raw = issue_workload_credential(cls.installation, "Test workload")

    def principal(self, **values):
        return Principal(**{"user": self.user, "evidence": self.evidence, "purpose": "treatment", **values})

    def api(self, path, data=None, purpose=""):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + self.raw, "HTTP_X_CORE_PURPOSE": purpose}
        if data is None:
            return self.client.get("/api/platform/v1" + path, **headers)
        return self.client.post("/api/platform/v1" + path, data=json.dumps(data), content_type="application/json", **headers)

    def test_admin_does_not_bypass_tag_grants_mfa_freshness_or_purpose(self):
        self.assertEqual(decision(self.principal(), self.dataset)[1], "tag_grant_required")
        self.member.access_tags.add(self.tag)
        self.assertTrue(decision(self.principal(), self.dataset)[0])
        self.assertEqual(decision(self.principal(evidence=None), self.dataset)[1], "mfa_required")
        self.assertEqual(decision(self.principal(purpose="research"), self.dataset)[1], "purpose_required")
        self.evidence.authenticated_at = timezone.now() - timedelta(minutes=10)
        self.assertEqual(decision(self.principal(), self.dataset)[1], "fresh_authentication_required")

    def test_tags_inherit_organization_service_and_dataset_boundaries(self):
        pipeline = Pipeline.objects.create(organization=self.org, dataset=self.dataset, name="Protected import", slug="protected-import")
        self.member.access_tags.add(self.tag)
        self.assertTrue(decision(self.principal(), pipeline)[0])
        foreign = PermissionTag.objects.create(organization=self.other, slug="other", name="Foreign")
        pipeline.permission_tags.add(foreign)
        self.assertEqual(decision(self.principal(), pipeline)[1], "invalid_resource_tag")
        self.org.permission_tags.add(self.tag)
        self.assertFalse(decision(self.principal(purpose=""), self.org)[0])

    def test_role_grants_are_live_and_foreign_role_cannot_grant_access(self):
        role = Role.objects.create(organization=self.org, slug="care-team", name="Care team")
        role.access_tags.add(self.tag)
        self.member.roles.add(role)
        self.assertTrue(decision(self.principal(), self.dataset)[0])
        role.access_tags.clear()
        self.assertFalse(decision(self.principal(), self.dataset)[0])
        other_role = Role.objects.create(organization=self.other, slug="foreign", name="Foreign")
        other_role.access_tags.add(self.tag)
        self.member.roles.add(other_role)
        self.assertFalse(decision(self.principal(), self.dataset)[0])

    def test_workloads_cannot_assert_human_mfa_or_bypass_machine_opt_in(self):
        self.installation.access_tags.add(self.tag)
        self.assertEqual(self.api("/datasets/health/records/", purpose="treatment").status_code, 403)
        self.tag.requires_mfa = False
        self.tag.max_auth_age = None
        self.tag.allow_workloads = True
        self.tag.save()
        self.assertEqual(self.api("/datasets/health/records/", purpose="wrong").status_code, 403)
        self.assertEqual(self.api("/datasets/health/records/", purpose="treatment").status_code, 200)
        self.installation.access_tags.clear()
        self.assertEqual(self.api("/datasets/health/records/", purpose="treatment").status_code, 403)

    def test_protected_reads_and_denials_audit_identifiers_without_payloads(self):
        with self.assertRaises(PermissionDenied):
            require_access(self.principal(), self.dataset, "data.read")
        event = AuditEvent.objects.get(action="data.read")
        self.assertEqual(event.outcome, "denied")
        self.assertEqual(event.target, str(self.dataset.pk))
        self.assertEqual(event.actor_subject, self.user.pk)
        self.assertEqual(event.tags, ["phi"])
        self.assertIsNotNone(event.request_id)
        self.member.access_tags.add(self.tag)
        require_access(self.principal(), self.dataset, "data.read")
        self.assertEqual(AuditEvent.objects.filter(action="data.read", outcome="allowed").count(), 1)

    def test_audit_records_resist_application_edit_delete_and_tenant_cascade(self):
        event = AuditEvent.objects.create(organization=self.org, actor=self.user, action="test", target="opaque-id")
        event.target = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(target="changed")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ProtectedError):
            self.org.delete()
        output = io.StringIO()
        call_command("export_security_audit", organization=self.org.slug, stdout=output)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["actor_subject"], str(self.user.pk))

    def test_audit_subject_survives_account_deletion(self):
        former = User.objects.create_user("former-member@example.test")
        subject = former.pk
        event = AuditEvent.objects.create(organization=self.org, actor=former, action="recorded", target="opaque-id")
        former.delete()
        event.refresh_from_db()
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.actor_subject, subject)
        self.assertEqual(event.principal_id, subject)

    def test_portal_hides_protected_dataset_and_blocks_direct_edit_and_declassification(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("platform_dashboard", args=[self.org.slug]))
        self.assertNotContains(page, self.dataset.name)
        self.assertEqual(self.client.get(reverse("dataset_edit", args=[self.org.slug, self.dataset.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("resource_tags", args=[self.org.slug, "dataset", self.dataset.pk]), {}).status_code, 403)

    def test_tag_assignment_rejects_cross_organization_grants_and_is_csrf_protected(self):
        foreign = PermissionTag.objects.create(organization=self.other, slug="foreign", name="Foreign")
        self.client.force_login(self.user)
        url = reverse("resource_tags", args=[self.org.slug, "membership", self.member.pk])
        response = self.client.post(url, {"access_tags": [foreign.pk]})
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(self.member.access_tags.exists())
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        self.assertEqual(browser.post(url, {"access_tags": [self.tag.pk]}).status_code, 403)
        self.assertEqual(self.client.get(reverse("resource_tags", args=[self.org.slug, "dataset", "invalid-id"])).status_code, 404)

    def test_tagged_workflow_cannot_keep_access_after_grant_revocation(self):
        self.tag.requires_mfa = False
        self.tag.max_auth_age = None
        self.tag.allow_workloads = True
        self.tag.allowed_purposes = []
        self.tag.save()
        self.installation.access_tags.add(self.tag)
        agent = AgentDefinition.objects.create(organization=self.org, slug="health-publish", name="Publish", kind="publish", configuration={"dataset": "health"})
        response = self.api("/agents/health-publish/runs/", {"request_id": "secure-run", "input": {"private": "never-log-this"}})
        self.assertEqual(response.status_code, 202)
        from platform_services.models import WorkflowRun
        run = WorkflowRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(list(run.permission_tags.values_list("slug", flat=True)), ["phi"])
        self.installation.access_tags.clear()
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.dataset.records.count(), 0)
        event = AuditEvent.objects.get(action="workflow.execution_denied")
        self.assertEqual(event.principal_id, self.credential.pk)
        self.assertNotIn("never-log-this", str(list(AuditEvent.objects.values())))
        self.assertEqual(self.api(f"/runs/{run.pk}/").status_code, 403)

    def test_authentication_policy_cannot_be_enabled_by_unverified_browser(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("authentication_policy", args=[self.org.slug]),
            {"require_federated": "on", "require_mfa": "on", "max_session_age": 3600})
        self.assertContains(response, "Establish verified MFA")
        self.assertFalse(AuthenticationPolicy.objects.filter(organization=self.org).exists())
        AuthenticationPolicy.objects.create(organization=self.org, require_federated=True)
        self.assertEqual(self.client.get(reverse("platform_dashboard", args=[self.org.slug])).status_code, 403)

    def test_browser_purpose_is_tenant_scoped_and_does_not_grant_mfa_or_tags(self):
        self.client.force_login(self.user)
        url = reverse("access_purpose", args=[self.org.slug])
        self.assertEqual(self.client.post(url, {"purpose_code": "treatment", "next": "https://attacker.example"})["Location"], "/")
        self.assertEqual(self.client.session["access_purposes"], {str(self.org.pk): "treatment"})
        self.assertEqual(AuditEvent.objects.get(action="access.purpose_selected").purpose_code, "treatment")
        self.assertEqual(self.client.get(reverse("dataset_edit", args=[self.org.slug, self.dataset.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("access_purpose", args=[self.other.slug]), {"purpose_code": "treatment"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"purpose_code": "patient name"}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, {"purpose_code": "treatment"}).status_code, 403)

    def test_existing_tag_controls_require_its_grants_and_assurance_even_when_retired(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("permission_tag", args=[self.org.slug, self.tag.pk]), {}).status_code, 403)
        self.member.access_tags.add(self.tag)
        self.assertTrue(decision(self.principal(), self.tag)[0])
        self.tag.active = False
        self.tag.save()
        self.assertTrue(decision(self.principal(), self.tag)[0])
        self.assertFalse(decision(self.principal(), self.dataset)[0])

    def test_provider_tags_also_protect_link_listing_and_editing(self):
        from identity.models import FederatedIdentity, IdentityProvider
        from django.conf import settings
        provider = IdentityProvider.objects.create(organization=self.org, slug="protected-provider", name="Protected provider",
            issuer="https://accounts.google.com", client_id="client", client_secret_file=str(settings.CORE_IDP_SECRET_ROOT) + "/" + str(self.org.pk) + "/client.secret")
        provider.permission_tags.add(self.tag)
        identity = FederatedIdentity.objects.create(provider=provider, subject="opaque-subject", user=self.user)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("security", args=[self.org.slug])), provider.name)
        self.assertEqual(self.client.get(reverse("identity_link", args=[self.org.slug, identity.pk])).status_code, 403)
        response = self.client.post(reverse("identity_link_add", args=[self.org.slug]),
            {"provider": provider.pk, "subject": "another-subject", "user": self.user.pk, "active": "on"})
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(FederatedIdentity.objects.filter(subject="another-subject").exists())

    def test_registered_service_resource_is_checked_centrally_and_unknown_resource_denied(self):
        service = Service.objects.create(organization=self.org, slug="records", name="Records")
        ServicePermission.objects.create(service=service, codename="read", name="Read")
        resource = ProtectedResource.objects.create(service=service, resource_type="patient-record", external_id="opaque-record-123", name="Record")
        resource.permission_tags.add(self.tag)
        secret = secrets.token_urlsafe(32)
        application = Application.objects.create(service=service, name="Records client", algorithm="RS256", client_secret=secret,
            client_type="confidential", authorization_grant_type="authorization-code", redirect_uris="https://records.example.test/callback")
        raw = secrets.token_urlsafe(32)
        token = AccessToken(user=self.user, application=application, expires=timezone.now() + timedelta(minutes=5), scope="openid authorization")
        set_token_value(token, raw)
        token.save()
        from identity.models import OAuthEvidence
        import hashlib
        OAuthEvidence.objects.create(token_hash=hashlib.sha256(raw.encode()).hexdigest(), kind="access", evidence=self.evidence)
        headers = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{application.client_id}:{secret}".encode()).decode()}
        values = {"token": raw, "permission": "read", "organization_id": str(self.org.pk),
                  "resource_type": "patient-record", "resource_id": "opaque-record-123", "purpose_code": "treatment"}
        self.assertFalse(self.client.post("/api/v1/authorize/", values, **headers).json()["allowed"])
        self.member.access_tags.add(self.tag)
        self.assertTrue(self.client.post("/api/v1/authorize/", values, **headers).json()["allowed"])
        values["resource_id"] = "unknown"
        self.assertFalse(self.client.post("/api/v1/authorize/", values, **headers).json()["allowed"])

    def test_runtime_cannot_receive_tagged_deployment_secrets_without_grants(self):
        DeploymentConfiguration.objects.create(organization=self.org, namespace="protected", environment="staging", application_origin="https://records.example.test")
        raw = secrets.token_urlsafe(48)
        node = RuntimeNode.objects.create(organization=self.org, namespace="protected", name="Runtime", credential_hash=WorkloadCredential.digest(raw))
        deployment = create_deployment(self.installation, node, "https://records.example.test")
        deployment.permission_tags.add(self.tag)
        response = self.client.post("/api/platform/v1/runtime/sync/", data='{"reports":[]}', content_type="application/json", HTTP_AUTHORIZATION="Bearer " + raw)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deployments"], [])
        response = self.client.post("/api/platform/v1/runtime/sync/", data=json.dumps({"reports": [
            {"deployment_id": str(deployment.pk), "revision": 1, "state": "running", "healthy": True}]}),
            content_type="application/json", HTTP_AUTHORIZATION="Bearer " + raw)
        deployment.refresh_from_db()
        self.assertFalse(deployment.healthy)

    def test_completed_run_inherits_new_source_tags_and_cannot_leak_old_output(self):
        self.dataset.permission_tags.clear()
        AgentDefinition.objects.create(organization=self.org, slug="publish-health", name="Publish", kind="publish", configuration={"dataset": "health"})
        response = self.api("/agents/publish-health/runs/", {"request_id": "before-classification", "input": {"private": "health-data"}})
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["run_id"]
        process_one()
        self.assertEqual(self.api(f"/runs/{run_id}/").status_code, 200)
        self.dataset.permission_tags.add(self.tag)
        self.assertEqual(self.api(f"/runs/{run_id}/").status_code, 403)
