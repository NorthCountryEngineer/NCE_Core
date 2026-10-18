"""Account ownership, profile validation, and scoped OIDC disclosure."""
import secrets
from unittest.mock import patch
from urllib.parse import urlunsplit

import jwt
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client, TestCase
from django.urls import reverse

from identity.forms import ProfileForm
from identity.models import ApplicationConsent, Membership, Organization, Role, User
from identity.tests import test_identity as identity_fixtures


def avatar_url_with_credentials():
    credentials = ":".join((secrets.token_urlsafe(12), secrets.token_urlsafe(24)))
    return urlunsplit(("https", "@".join((credentials, "images.example.test")), "/avatar.png", "", ""))


class ProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("profile@example.test", secrets.token_urlsafe(32),
            first_name="Original", last_name="Name")
        cls.other = User.objects.create_user("other-profile@example.test", secrets.token_urlsafe(32),
            first_name="Other", last_name="Person")
        cls.organization = Organization.objects.create(slug="profiles", name="Profile organization")
        cls.membership = Membership.objects.create(user=cls.user, organization=cls.organization)
        cls.role = Role.objects.create(organization=cls.organization, slug="profile-member", name="Profile member")
        cls.membership.roles.add(cls.role)

    def setUp(self):
        self.client.force_login(self.user)

    def test_profile_requires_sign_in_and_has_private_cache_policy(self):
        self.client.logout()
        response = self.client.get(reverse("profile"))
        self.assertRedirects(response, reverse("login") + "?next=" + reverse("profile"), fetch_redirect_response=False)
        self.client.force_login(self.user)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "Your profile")
        self.assertContains(response, 'id="account-email" type="email" value="profile@example.test" readonly')
        self.assertContains(response, str(self.user.pk))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(list(response.context["form"].fields), ["first_name", "last_name", "avatar_url"])
        self.assertEqual(self.client.put(reverse("profile")).status_code, 405)

    def test_owner_can_save_profile_without_modifying_identity_or_roles(self):
        original_password = self.user.password
        response = self.client.post(reverse("profile"), {
            "first_name": "  Updated  ", "last_name": "Person", "avatar_url": "https://images.example.test/updated.png",
            "email": self.other.email, "password": secrets.token_urlsafe(32), "is_superuser": "true", "is_staff": "true",
            "is_developer": "true", "is_active": "false", "user_id": self.other.pk,
            "kind": "administrator", "roles": [],
        })
        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        self.other.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.user.first_name, "Updated")
        self.assertEqual(self.user.last_name, "Person")
        self.assertEqual(self.user.avatar_url, "https://images.example.test/updated.png")
        self.assertEqual(self.user.email, "profile@example.test")
        self.assertEqual(self.user.password, original_password)
        self.assertFalse(self.user.is_superuser or self.user.is_staff or self.user.is_developer)
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.membership.kind, Membership.Kind.MEMBER)
        self.assertEqual(list(self.membership.roles.all()), [self.role])
        self.assertEqual(self.other.first_name, "Other")

    def test_optional_avatar_can_be_added_and_removed_without_server_fetches(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Profile must not fetch images")):
            self.client.post(reverse("profile"), {"first_name": "Profile", "last_name": "Owner",
                "avatar_url": "https://images.example.test/profile.png"})
            response = self.client.get(reverse("profile"))
            self.assertContains(response, 'src="https://images.example.test/profile.png"')
            self.assertContains(response, 'referrerpolicy="no-referrer"')
            self.client.post(reverse("profile"), {"first_name": "Profile", "last_name": "Owner", "avatar_url": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_url, "")
        self.assertNotContains(self.client.get(reverse("profile")), 'class="profile-avatar"')

    def test_invalid_avatar_urls_do_not_partially_save_or_render_in_preview(self):
        invalid_urls = ["http://images.example.test/a.png", "javascript:alert(1)", "data:image/png;base64,aGVsbG8=",
            avatar_url_with_credentials(), "https://images.example.test/a.png#fragment",
            "https://images.example.test/\\evil.png", "https://images.example.test/a\n.png"]
        for url in invalid_urls:
            with self.subTest(url=url):
                response = self.client.post(reverse("profile"), {"first_name": "Invalid update", "last_name": "Name", "avatar_url": url})
                self.assertEqual(response.status_code, 200)
                self.assertIn("avatar_url", response.context["form"].errors)
                self.assertNotContains(response, 'class="profile-avatar"')
                self.user.refresh_from_db()
                self.assertEqual(self.user.first_name, "Original")
                self.assertEqual(self.user.avatar_url, "")

    def test_model_and_form_enforce_url_and_name_limits(self):
        self.user.avatar_url = avatar_url_with_credentials()
        with self.assertRaises(ValidationError):
            self.user.full_clean()
        form = ProfileForm({"first_name": "a" * 151, "last_name": "Name", "avatar_url": ""}, instance=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("first_name", form.errors)

    def test_profile_post_requires_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        self.assertEqual(browser.post(reverse("profile"), {"first_name": "Blocked"}).status_code, 403)
        browser.get(reverse("profile"))
        response = browser.post(reverse("profile"), {"first_name": "Allowed", "last_name": "Name", "avatar_url": ""},
            HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value)
        self.assertRedirects(response, reverse("profile"))


class ProfileClaimsTests(TestCase):
    setUpTestData = classmethod(identity_fixtures.IdentityTests.setUpTestData.__func__)
    create_app = staticmethod(identity_fixtures.IdentityTests.create_app)
    basic = identity_fixtures.IdentityTests.basic
    start_flow = identity_fixtures.IdentityTests.start_flow
    complete_flow = identity_fixtures.IdentityTests.complete_flow

    def setUp(self):
        self.member.first_name = "Profile"
        self.member.last_name = "Reader"
        self.member.avatar_url = "https://images.example.test/reader.png"
        self.member.save(update_fields=["first_name", "last_name", "avatar_url"])

    def id_token_claims(self, tokens):
        keys = self.client.get(reverse("oauth2_provider:jwks-info")).json()["keys"]
        header = jwt.get_unverified_header(tokens["id_token"])
        key = jwt.PyJWK.from_dict(next(key for key in keys if key["kid"] == header["kid"]))
        return jwt.decode(tokens["id_token"], key.key, algorithms=["RS256"], audience=self.app.client_id, issuer=settings.ISSUER)

    def userinfo(self, tokens):
        response = self.client.get(reverse("oauth2_provider:user-info"), HTTP_AUTHORIZATION="Bearer " + tokens["access_token"])
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_profile_and_email_claims_respect_independent_scopes(self):
        for scope in ["openid", "openid email", "openid profile", "openid profile email"]:
            with self.subTest(scope=scope), patch("requests.sessions.Session.request", side_effect=AssertionError("OIDC must not fetch avatar")):
                tokens = self.complete_flow(scope=scope, prompt="consent")
                for claims in [self.id_token_claims(tokens), self.userinfo(tokens)]:
                    self.assertEqual(claims["sub"], str(self.member.pk))
                    for name, value in {"name": "Profile Reader", "given_name": "Profile", "family_name": "Reader",
                            "picture": self.member.avatar_url}.items():
                        if "profile" in scope.split():
                            self.assertEqual(claims[name], value)
                        else:
                            self.assertNotIn(name, claims)
                    if "email" in scope.split():
                        self.assertEqual(claims["email"], self.member.email)
                    else:
                        self.assertNotIn("email", claims)
                    self.assertNotIn("email_verified", claims)

    def test_userinfo_reflects_current_profile_and_omits_removed_avatar(self):
        tokens = self.complete_flow()
        signed_snapshot = self.id_token_claims(tokens)
        self.client.post(reverse("profile"), {"first_name": "Changed", "last_name": "Name", "avatar_url": ""})
        claims = self.userinfo(tokens)
        self.assertEqual(claims["name"], "Changed Name")
        self.assertNotIn("picture", claims)
        self.assertEqual(signed_snapshot["name"], "Profile Reader")
        self.assertEqual(claims["sub"], signed_snapshot["sub"])

    def test_discovery_and_approved_application_list(self):
        tokens = self.complete_flow()
        claims = self.client.get(reverse("discovery")).json()["claims_supported"]
        for name in ["name", "given_name", "family_name", "picture", "email"]:
            self.assertIn(name, claims)
        self.assertNotIn("email_verified", claims)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "Reports web")
        consent = ApplicationConsent.objects.get(user=self.member, application=self.app)
        self.assertContains(response, reverse("revoke_application_consent", args=[consent.pk]))
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("profile")), "Reports web")


class ProfileMigrationCompatibilityTests(TestCase):
    def test_pre_profile_user_insert_uses_database_avatar_default(self):
        historical_apps = MigrationLoader(connection).project_state([
            ("identity", "0009_service_auth_name_service_auth_tagline_and_more"),
        ]).apps
        previous_user_model = historical_apps.get_model("identity", "User")
        self.assertNotIn("avatar_url", [field.name for field in previous_user_model._meta.fields])
        # Model the still-running pre-upgrade process after migrations have
        # added the column: its INSERT does not know about avatar_url.
        previous_user = previous_user_model.objects.create(
            email="pre-profile-upgrade@example.test", password=make_password(secrets.token_urlsafe(32)),
            first_name="Before", last_name="Upgrade")
        current_user = User.objects.get(pk=previous_user.pk)
        self.assertEqual(current_user.avatar_url, "")
        self.assertEqual(current_user.get_full_name(), "Before Upgrade")
