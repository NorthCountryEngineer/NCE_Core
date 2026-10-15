import contextvars
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from identity.access import Principal, decision
from identity.models import AuthenticationEvidence, Membership, PermissionTag, ServicePermission, User
from identity.policy import authorization_for
from identity.policy_evaluation import evaluation_active, policy_evaluation, policy_fact
from platform_services.tests import test_external_apps


class PolicyEvaluationLifetimeTests(SimpleTestCase):
    def test_nested_scope_is_shared_but_exited_and_copied_contexts_are_fresh(self):
        load = Mock(side_effect=range(10))
        with policy_evaluation():
            self.assertEqual(policy_fact(("tenant", "fact"), load), 0)
            with policy_evaluation():
                self.assertEqual(policy_fact(("tenant", "fact"), load), 0)
            copied = contextvars.copy_context()
            self.assertTrue(evaluation_active())
        self.assertFalse(evaluation_active())
        self.assertFalse(copied.run(evaluation_active))
        self.assertEqual(copied.run(policy_fact, ("tenant", "fact"), load), 1)
        self.assertEqual(copied.run(policy_fact, ("tenant", "fact"), load), 2)
        with policy_evaluation():
            self.assertEqual(policy_fact(("tenant", "fact"), load), 3)
        self.assertEqual(load.call_count, 4)

    def test_exception_discards_all_facts(self):
        load = Mock(side_effect=["before", "after"])
        with self.assertRaises(ValueError):
            with policy_evaluation():
                self.assertEqual(policy_fact("fact", load), "before")
                raise ValueError("synthetic failure")
        with policy_evaluation():
            self.assertEqual(policy_fact("fact", load), "after")


class LivePolicyEvaluationTests(TestCase):
    # Reuse the synthetic external deployment and real PKCE exchange fixture.
    @classmethod
    def setUpTestData(cls):
        test_external_apps.ExternalApplicationTests.setUpTestData.__func__(cls)

    basic = test_external_apps.ExternalApplicationTests.basic
    exchange = test_external_apps.ExternalApplicationTests.exchange
    introspect = test_external_apps.ExternalApplicationTests.introspect

    def test_read_queries_remain_bounded_as_granted_permissions_grow(self):
        tokens = self.exchange()
        counts = []
        for administrator, extra, expected in [(False, 0, 1), (True, 0, 4), (True, 20, 24)]:
            Membership.objects.filter(pk=self.membership.pk).update(kind="administrator" if administrator else "member")
            for index in range(extra):
                ServicePermission.objects.create(service=self.installation.service,
                    codename=f"synthetic-{index}", name=f"Synthetic {index}")
            with CaptureQueriesContext(connection) as queries:
                claims = self.introspect(tokens["access_token"]).json()
            self.assertTrue(claims["active"])
            self.assertEqual(len(claims["permissions"]), expected)
            reads = sum(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
            audits = sum(query["sql"].lstrip().upper().startswith('INSERT INTO "IDENTITY_AUDITEVENT"') for query in queries)
            counts.append(reads)
            self.assertLessEqual(reads, 38)
            # Preserve every existing per-permission audit, rather than hiding
            # linear audit writes in the read-query performance requirement.
            self.assertEqual(audits, expected if administrator else expected + 1)
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_new_http_request_observes_tag_grant_and_tag_deactivation(self):
        tokens = self.exchange()
        tag = PermissionTag.objects.create(organization=self.organization, slug="software", name="Software")
        self.write.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        self.assertIn("posts-write", self.introspect(tokens["access_token"]).json()["permissions"])
        self.membership.access_tags.remove(tag)
        self.assertNotIn("posts-write", self.introspect(tokens["access_token"]).json()["permissions"])
        self.membership.access_tags.add(tag)
        self.assertIn("posts-write", self.introspect(tokens["access_token"]).json()["permissions"])
        PermissionTag.objects.filter(pk=tag.pk).update(active=False)
        self.assertNotIn("posts-write", self.introspect(tokens["access_token"]).json()["permissions"])

    def test_new_http_request_rechecks_purpose_mfa_and_evidence_revocation(self):
        tokens = self.exchange()
        tag = PermissionTag.objects.create(organization=self.organization, slug="purpose", name="Purpose",
                                           allowed_purposes=["capture"])
        self.write.permission_tags.add(tag)
        self.membership.access_tags.add(tag)

        def claims(purpose):
            return self.client.post("/oauth/introspect/", {"token": tokens["access_token"], "purpose_code": purpose},
                                    **self.basic()).json()

        self.assertNotIn("posts-write", claims("")["permissions"])
        self.assertIn("posts-write", claims("capture")["permissions"])
        PermissionTag.objects.filter(pk=tag.pk).update(allowed_purposes=["other"])
        self.assertNotIn("posts-write", claims("capture")["permissions"])
        PermissionTag.objects.filter(pk=tag.pk).update(allowed_purposes=["capture"], requires_mfa=True)
        self.assertNotIn("posts-write", claims("capture")["permissions"])
        AuthenticationEvidence.objects.filter(user=self.member).update(mfa=True)
        self.assertIn("posts-write", claims("capture")["permissions"])
        AuthenticationEvidence.objects.filter(user=self.member).update(revoked=True)
        self.assertEqual(claims("capture"), {"active": False})

    def test_two_users_and_organizations_do_not_share_grants_in_one_scope(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug="private", name="Private")
        self.write.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        other_user = User.objects.create_user("other-policy-user@example.test")
        other_membership = Membership.objects.create(user=other_user, organization=self.organization)
        other_membership.roles.add(self.role)
        other_membership.access_tags.add(self.environment.boundary_tag)
        other_org_membership = Membership.objects.create(user=self.member, organization=self.other, kind="administrator")
        from identity.models import Service
        other_service = Service.objects.create(organization=self.other, slug="other-policy", name="Other policy")
        ServicePermission.objects.create(service=other_service, codename="other-read", name="Other read")
        with policy_evaluation():
            first = authorization_for(self.membership, self.installation.service,
                Principal(user=self.member, application=self.browser))
            second = authorization_for(other_membership, self.installation.service,
                Principal(user=other_user, application=self.browser))
            foreign = authorization_for(other_org_membership, other_service, Principal(user=self.member))
        self.assertIn("posts-write", first["permissions"])
        self.assertNotIn("posts-write", second["permissions"])
        self.assertEqual(foreign["permissions"], ["admin", "other-read"])

    def test_reused_principal_outside_scope_observes_membership_revocation(self):
        principal = Principal(user=self.member, application=self.browser)
        self.assertTrue(decision(principal, self.installation.service)[0])
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        self.assertFalse(decision(principal, self.installation.service)[0])

    def test_time_and_purpose_conditions_are_not_cached_with_facts(self):
        now = timezone.now()
        evidence = AuthenticationEvidence.objects.create(user=self.member, authenticated_at=now, verified_at=now, mfa=True)
        tag = PermissionTag.objects.create(organization=self.organization, slug="fresh", name="Fresh",
                                           max_auth_age=10, allowed_purposes=["capture"])
        self.write.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        principal = Principal(user=self.member, evidence=evidence, application=self.browser, purpose="capture")
        with policy_evaluation():
            with patch("identity.access.timezone.now", return_value=now):
                self.assertTrue(decision(principal, self.write)[0])
            with patch("identity.access.timezone.now", return_value=now + timedelta(seconds=11)):
                self.assertEqual(decision(principal, self.write)[1], "fresh_authentication_required")
            principal.purpose = "other"
            with patch("identity.access.timezone.now", return_value=now):
                self.assertEqual(decision(principal, self.write)[1], "purpose_required")
