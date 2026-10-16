import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password, make_password
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from identity.client_credentials import ResourceClientSecretVerifier
from identity.models import Application, Membership, Organization, Role, Service, ServicePermission, User


class ResourceClientSecretVerifierTests(SimpleTestCase):
    def test_success_only_is_bound_to_client_secret_and_current_hash(self):
        verifier = ResourceClientSecretVerifier()
        secret = secrets.token_urlsafe(32)
        with patch("identity.client_credentials.check_password", return_value=True) as check:
            self.assertTrue(verifier.verify("one", secret, "hash-one"))
            self.assertTrue(verifier.verify("one", secret, "hash-one"))
            self.assertEqual(check.call_count, 1)
            verifier.verify("two", secret, "hash-one")
            verifier.verify("one", secret, "hash-two")
            self.assertEqual(check.call_count, 3)
            check.return_value = False
            for _ in range(2):
                self.assertFalse(verifier.verify("one", secret + "invalid", "hash-one"))
            self.assertEqual(check.call_count, 5)
        self.assertTrue(all(isinstance(key, bytes) and len(key) == 32 for key in verifier._entries))
        self.assertTrue(all(isinstance(expiry, float) for expiry in verifier._entries.values()))

    def test_expiry_and_lru_bound_force_normal_verification(self):
        now = [100.0]
        verifier = ResourceClientSecretVerifier(max_entries=2, clock=lambda: now[0])
        with patch("identity.client_credentials.check_password", return_value=True) as check:
            verifier.verify("one", "supplied", "encoded")
            verifier.verify("two", "supplied", "encoded")
            verifier.verify("one", "supplied", "encoded")
            verifier.verify("three", "supplied", "encoded")
            self.assertEqual(len(verifier._entries), 2)
            verifier.verify("two", "supplied", "encoded")
            self.assertEqual(check.call_count, 4)
            now[0] += 599
            verifier.verify("two", "supplied", "encoded")
            self.assertEqual(check.call_count, 4)
            now[0] += 1
            verifier.verify("two", "supplied", "encoded")
            self.assertEqual(check.call_count, 5)

    def test_concurrent_success_has_one_expensive_verification(self):
        verifier = ResourceClientSecretVerifier()
        started = threading.Event()
        release = threading.Event()

        def check(*args):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return True

        with patch("identity.client_credentials.check_password", side_effect=check) as check_mock:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(verifier.verify, "client", "supplied", "encoded") for _ in range(8)]
                self.assertTrue(started.wait(timeout=5))
                release.set()
                self.assertTrue(all(future.result(timeout=5) for future in futures))
            self.assertEqual(check_mock.call_count, 1)

    def test_hash_failure_does_not_create_a_proof(self):
        verifier = ResourceClientSecretVerifier()
        with patch("identity.client_credentials.check_password", side_effect=RuntimeError("hash unavailable")):
            with self.assertRaises(RuntimeError):
                verifier.verify("client", "supplied", "encoded")
        self.assertEqual(len(verifier._entries), 0)


class WarmResourceClientAuthorizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(slug="credential-tests", name="Credential tests")
        cls.service = Service.objects.create(organization=cls.org, slug="reports", name="Reports")
        cls.user = User.objects.create_user("credential-user@example.test", secrets.token_urlsafe(32))
        cls.membership = Membership.objects.create(user=cls.user, organization=cls.org)
        cls.permission = ServicePermission.objects.create(service=cls.service, codename="read", name="Read")
        cls.role = Role.objects.create(organization=cls.org, slug="reader", name="Reader")
        cls.role.permissions.add(cls.permission)
        cls.membership.roles.add(cls.role)
        cls.secret = secrets.token_urlsafe(32)
        cls.app = Application.objects.create(service=cls.service, name="Credential test app",
            client_type=Application.CLIENT_CONFIDENTIAL, client_secret=cls.secret,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            algorithm=Application.RS256_ALGORITHM, redirect_uris="https://app.example.test/callback")

    def setUp(self):
        self.verifier = ResourceClientSecretVerifier()
        self.cache_patch = patch("identity.views.resource_client_secrets", self.verifier)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.raw = secrets.token_urlsafe(32)
        self.token = AccessToken(application=self.app, user=self.user, scope="openid authorization",
                                 expires=timezone.now() + timedelta(minutes=5))
        set_token_value(self.token, self.raw)
        self.token.save()

    def introspect(self, secret=None):
        import base64
        authorization = base64.b64encode(f"{self.app.client_id}:{secret or self.secret}".encode()).decode()
        return self.client.post("/oauth/introspect/", {"token": self.raw}, HTTP_AUTHORIZATION=f"Basic {authorization}")

    def test_warm_client_still_observes_live_permissions_membership_and_token(self):
        with patch("identity.client_credentials.check_password", wraps=check_password) as check:
            self.assertEqual(self.introspect().json()["permissions"], ["read"])
            self.role.permissions.clear()
            self.assertEqual(self.introspect().json()["permissions"], [])
            Membership.objects.filter(pk=self.membership.pk).update(active=False)
            self.assertEqual(self.introspect().json(), {"active": False})
            Membership.objects.filter(pk=self.membership.pk).update(active=True)
            self.assertTrue(self.introspect().json()["active"])
            self.token.delete()
            self.assertEqual(self.introspect().json(), {"active": False})
            self.assertEqual(check.call_count, 1)

    def test_warm_client_and_organization_deactivation_are_immediate(self):
        self.assertTrue(self.introspect().json()["active"])
        for model, instance in ((Application, self.app), (Service, self.service), (Organization, self.org)):
            model.objects.filter(pk=instance.pk).update(active=False)
            self.assertEqual(self.introspect().status_code, 401)
            model.objects.filter(pk=instance.pk).update(active=True)
            self.assertTrue(self.introspect().json()["active"])
        Application.objects.filter(pk=self.app.pk).update(client_type=Application.CLIENT_PUBLIC)
        self.assertEqual(self.introspect().status_code, 401)

    def test_warm_credential_rejects_old_secret_immediately_after_rotation(self):
        self.assertTrue(self.introspect().json()["active"])
        replacement = secrets.token_urlsafe(32)
        Application.objects.filter(pk=self.app.pk).update(client_secret=make_password(replacement))
        self.assertEqual(self.introspect().status_code, 401)
        self.assertTrue(self.introspect(replacement).json()["active"])
        self.assertEqual(self.introspect().status_code, 401)

    def test_rehashing_same_secret_requires_new_verification(self):
        self.assertTrue(self.introspect().json()["active"])
        Application.objects.filter(pk=self.app.pk).update(client_secret=make_password(self.secret))
        with patch("identity.client_credentials.check_password", wraps=check_password) as check:
            self.assertTrue(self.introspect().json()["active"])
            self.assertEqual(check.call_count, 1)

    def test_database_unavailability_does_not_fall_back_to_cached_authentication(self):
        self.assertTrue(self.introspect().json()["active"])
        from django.db import OperationalError
        with patch("identity.views.Application.objects.select_related", side_effect=OperationalError("database unavailable")):
            with self.assertRaises(OperationalError):
                self.introspect()
