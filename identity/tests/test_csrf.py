import secrets
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from identity.models import Invitation, Membership, Organization, User


@override_settings(
    DEBUG=True,
    ALLOWED_HOSTS=["localhost"],
    CSRF_TRUSTED_ORIGINS=["http://localhost:8000"],
)
class BrowserFormSecurityTests(TestCase):
    origin = "http://localhost:8000"
    host = "localhost:8000"

    @classmethod
    def setUpTestData(cls):
        cls.password = secrets.token_urlsafe(32)
        cls.admin = User.objects.create_superuser("operator@example.test", cls.password)
        cls.organization = Organization.objects.create(slug="inviting", name="Inviting")
        Membership.objects.create(
            organization=cls.organization, user=cls.admin, kind="administrator",
        )

    def setUp(self):
        self.browser = Client(enforce_csrf_checks=True)

    def get_form(self, path):
        response = self.browser.get(path, HTTP_HOST=self.host)
        self.assertEqual(response.status_code, 200)
        # Browsers derive the Origin header for native form POSTs from this
        # policy. A no-referrer policy can produce an opaque (null) origin.
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        return self.browser.cookies["csrftoken"].value

    def post_form(self, path, data, token, origin=None):
        return self.browser.post(
            path, {**data, "csrfmiddlewaretoken": token},
            HTTP_HOST=self.host, HTTP_ORIGIN=self.origin if origin is None else origin,
        )

    def test_login_then_onboarding_with_rotated_csrf_token(self):
        token = self.get_form("/accounts/login/")
        response = self.post_form("/accounts/login/", {
            "username": self.admin.email, "password": self.password,
        }, token)
        self.assertEqual(response.status_code, 302)
        new_token = self.get_form("/onboarding/")
        self.assertNotEqual(token, new_token)
        data = {"name": "Browser Sandbox", "slug": "browser-sandbox", "identity_name": ""}
        self.assertEqual(self.post_form("/onboarding/", data, token).status_code, 403)
        self.assertEqual(self.post_form("/onboarding/", data, new_token).status_code, 302)
        self.assertTrue(Organization.objects.filter(slug="browser-sandbox").exists())

    def test_null_and_foreign_origins_remain_rejected(self):
        token = self.get_form("/accounts/login/")
        for origin in ("null", "https://attacker.example.test", "http://127.0.0.1:8000"):
            with self.subTest(origin=origin):
                response = self.post_form("/accounts/login/", {
                    "username": self.admin.email, "password": self.password,
                }, token, origin=origin)
                self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.browser.session)

    def test_same_origin_still_requires_csrf_token(self):
        self.get_form("/accounts/login/")
        response = self.browser.post("/accounts/login/", {
            "username": self.admin.email, "password": self.password,
        }, HTTP_HOST=self.host, HTTP_ORIGIN=self.origin)
        self.assertEqual(response.status_code, 403)

    def test_invitation_creation_page_supports_same_origin_forms(self):
        self.browser.force_login(self.admin)
        path = "/organizations/inviting/invite/"
        token = self.get_form(path)
        response = self.post_form(path, {"email": "invitee@example.test", "kind": "member"}, token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(self.post_form("/accounts/logout/", {}, token).status_code, 302)

    def test_invitation_acceptance_keeps_csrf_protection(self):
        raw = secrets.token_urlsafe(32)
        invitation = Invitation.objects.create(
            organization=self.organization, email="invitee@example.test", kind="member",
            token_hash=Invitation.digest(raw), expires=timezone.now() + timedelta(hours=1),
        )
        path = f"/invitations/{raw}/"
        token = self.get_form(path)
        password = secrets.token_urlsafe(32)
        data = {"password1": password, "password2": password}
        self.assertEqual(self.post_form(path, data, token, origin="null").status_code, 403)
        self.assertEqual(self.post_form(path, data, token).status_code, 302)
        invitation.refresh_from_db()
        self.assertTrue(invitation.accepted)
