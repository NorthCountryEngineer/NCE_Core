import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, RefreshToken, set_token_value

from identity.models import Application, AuditEvent, Invitation, Membership, Organization, Role, Service, ServicePermission, User


class IdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(slug="north", name="North", identity_name="North Access")
        cls.other_org = Organization.objects.create(slug="south", name="South")
        cls.admin = User.objects.create_user("admin@example.test", "test-password-long")
        cls.member = User.objects.create_user("member@example.test", "test-password-long")
        cls.outsider = User.objects.create_user("other@example.test", "test-password-long")
        cls.admin_membership = Membership.objects.create(user=cls.admin, organization=cls.org, kind="administrator")
        cls.member_membership = Membership.objects.create(user=cls.member, organization=cls.org)
        cls.other_membership = Membership.objects.create(user=cls.outsider, organization=cls.other_org, kind="administrator")
        cls.service = Service.objects.create(organization=cls.org, slug="reports", name="Reports")
        cls.second_service = Service.objects.create(organization=cls.org, slug="billing", name="Billing")
        cls.other_service = Service.objects.create(organization=cls.other_org, slug="reports", name="South Reports")
        cls.permission = ServicePermission.objects.create(service=cls.service, codename="read", name="Read reports")
        cls.role = Role.objects.create(organization=cls.org, slug="reader", name="Reader")
        cls.role.permissions.add(cls.permission)
        cls.member_membership.roles.add(cls.role)
        cls.raw_secret = secrets.token_urlsafe(32)
        cls.app = cls.create_app(cls.service, "Reports web", cls.raw_secret)
        cls.second_app = cls.create_app(cls.second_service, "Billing web", cls.raw_secret)
        cls.other_app = cls.create_app(cls.other_service, "South web", cls.raw_secret)

    @staticmethod
    def create_app(service, name, secret, client_type="confidential"):
        app = Application(service=service, name=name, client_secret=secret,
                          client_type=client_type, authorization_grant_type="authorization-code",
                          algorithm="RS256", redirect_uris="https://app.example.test/callback")
        app.full_clean()
        app.save()
        return app

    def basic(self, app=None, secret=None):
        return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(
            f"{(app or self.app).client_id}:{secret or self.raw_secret}".encode(),
        ).decode()}

    def issue_fixture_token(self, user=None, app=None, scope="openid authorization"):
        raw = secrets.token_urlsafe(32)
        token = AccessToken(application=app or self.app, user=user or self.member,
                            scope=scope, expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, raw)
        token.save()
        return raw, token

    def introspect(self, raw, app=None):
        return self.client.post("/oauth/introspect/", {"token": raw}, **self.basic(app))

    def authorize(self, raw, permission="read", organization=None, app=None):
        return self.client.post("/api/v1/authorize/", {
            "token": raw, "permission": permission,
            "organization_id": str(organization or self.org.pk),
        }, **self.basic(app))

    def start_flow(self, user=None, app=None, **overrides):
        app = app or self.app
        self.client.force_login(user or self.member)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": app.client_id, "redirect_uri": app.redirect_uris,
                  "response_type": "code", "scope": "openid profile email authorization",
                  "state": "test-state", "nonce": "test-nonce", "code_challenge": challenge,
                  "code_challenge_method": "S256", **overrides}
        response = self.client.get("/oauth/authorize/", params)
        return response, verifier, params

    def complete_flow(self, user=None, app=None, **overrides):
        app = app or self.app
        page, verifier, params = self.start_flow(user, app, **overrides)
        self.assertEqual(page.status_code, 200, page.content.decode())
        form = page.context["form"]
        consent = {field.name: field.value() or "" for field in form}
        consent["allow"] = "true"
        redirect = self.client.post("/oauth/authorize/", consent)
        self.assertEqual(redirect.status_code, 302, redirect.content.decode())
        query = parse_qs(urlparse(redirect["Location"]).query)
        self.assertEqual(query["state"], ["test-state"])
        response = self.client.post("/oauth/token/", {
            "grant_type": "authorization_code", "code": query["code"][0],
            "redirect_uri": app.redirect_uris, "code_verifier": verifier,
        }, **self.basic(app))
        self.assertEqual(response.status_code, 200, response.content.decode())
        return response.json()

    def test_full_oidc_federation_and_signed_claims(self):
        tokens = self.complete_flow()
        keys = self.client.get("/oauth/jwks/").json()["keys"]
        header = jwt.get_unverified_header(tokens["id_token"])
        key = jwt.PyJWK.from_dict(next(key for key in keys if key["kid"] == header["kid"]))
        claims = jwt.decode(tokens["id_token"], key.key, algorithms=["RS256"],
                            audience=self.app.client_id, issuer=settings.ISSUER)
        self.assertEqual(claims["nonce"], "test-nonce")
        self.assertEqual(claims["sub"], str(self.member.pk))
        self.assertEqual(claims["organization_id"], str(self.org.pk))
        self.assertEqual(claims["identity_name"], "North Access")
        self.assertEqual(claims["permissions"], ["read"])
        self.assertEqual(claims["email"], self.member.email)
        userinfo = self.client.get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(userinfo.status_code, 200)
        self.assertEqual(userinfo.json()["sub"], claims["sub"])
        self.assertTrue(self.introspect(tokens["access_token"]).json()["active"])
        self.assertTrue(self.authorize(tokens["access_token"]).json()["allowed"])
        # Reusable bearer values are not stored in the database.
        self.assertEqual(AccessToken.objects.get(token_checksum=hashlib.sha256(tokens["access_token"].encode()).hexdigest()).token, "")
        self.assertEqual(RefreshToken.objects.first().token, "")

    def test_discovery_is_stable_and_describes_safe_flow(self):
        data = self.client.get("/.well-known/openid-configuration").json()
        self.assertEqual(data["issuer"], settings.ISSUER)
        self.assertEqual(data["response_types_supported"], ["code"])
        self.assertEqual(data["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(data["id_token_signing_alg_values_supported"], ["RS256"])
        self.assertEqual(data["introspection_endpoint"], settings.ISSUER + "/oauth/introspect/")

    def test_other_organization_cannot_sign_in(self):
        response, _, _ = self.start_flow(user=self.outsider)
        self.assertEqual(response.status_code, 403)

    def test_multiorganization_user_gets_client_specific_context(self):
        Membership.objects.create(user=self.member, organization=self.other_org)
        tokens = self.complete_flow(app=self.other_app)
        claims = self.introspect(tokens["access_token"], self.other_app).json()
        self.assertEqual(claims["organization_id"], str(self.other_org.pk))
        self.assertEqual(claims["permissions"], [])
        self.assertEqual(self.introspect(tokens["access_token"]).json(), {"active": False})

    def test_missing_pkce_rejected(self):
        response, _, _ = self.start_flow(code_challenge="", code_challenge_method="")
        self.assertIn(response.status_code, [200, 302])
        if response.status_code == 302:
            self.assertIn("error=", response["Location"])
        else:
            self.assertContains(response, "code_challenge")

    def test_plain_pkce_rejected(self):
        response, _, _ = self.start_flow(code_challenge_method="plain")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_redirect_uri_and_resource_cannot_be_changed(self):
        response, _, _ = self.start_flow(redirect_uri="https://attacker.test/callback")
        self.assertEqual(response.status_code, 400)
        response, _, _ = self.start_flow(resource="https://attacker.test/")
        self.assertEqual(response.status_code, 400)

    def test_unregistered_privileged_scope_rejected(self):
        response, _, _ = self.start_flow(scope="openid authorization core.admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("invalid_scope", response["Location"])

    def test_administrator_has_admin_on_all_own_services(self):
        for service, app in [(self.service, self.app), (self.second_service, self.second_app)]:
            raw, _ = self.issue_fixture_token(user=self.admin, app=app)
            self.assertTrue(self.authorize(raw, "admin", app=app).json()["allowed"])
        raw, _ = self.issue_fixture_token(user=self.admin)
        self.assertFalse(self.authorize(raw, app=self.other_app).json()["allowed"])
        self.assertFalse(self.authorize(raw, "unknown").json()["allowed"])

    def test_member_and_org_context_cannot_escalate(self):
        raw, _ = self.issue_fixture_token()
        self.assertFalse(self.authorize(raw, "admin").json()["allowed"])
        self.assertFalse(self.authorize(raw, organization=self.other_org.pk).json()["allowed"])
        self.assertFalse(self.authorize(raw, app=self.second_app).json()["allowed"])

    def test_live_permission_removal(self):
        raw, _ = self.issue_fixture_token()
        self.assertTrue(self.authorize(raw).json()["allowed"])
        self.member_membership.roles.clear()
        self.assertFalse(self.authorize(raw).json()["allowed"])

    def test_cross_tenant_roles_and_permissions_are_never_used(self):
        foreign_permission = ServicePermission.objects.create(service=self.other_service, codename="danger", name="Danger")
        foreign_role = Role.objects.create(organization=self.other_org, slug="foreign", name="Foreign")
        foreign_role.permissions.add(self.permission)
        self.member_membership.roles.clear()
        self.member_membership.roles.add(foreign_role)
        self.role.permissions.add(foreign_permission)
        raw, _ = self.issue_fixture_token()
        self.assertEqual(self.introspect(raw).json()["permissions"], [])
        self.member_membership.roles.add(self.role)
        self.assertEqual(self.introspect(raw).json()["permissions"], ["read"])

    def test_authorization_scope_required(self):
        raw, _ = self.issue_fixture_token(user=self.admin, scope="openid")
        self.assertFalse(self.authorize(raw, "admin").json()["allowed"])

    def test_revoked_membership_rejects_token_userinfo_and_refresh(self):
        tokens = self.complete_flow()
        Membership.objects.filter(pk=self.member_membership.pk).update(active=False)
        self.assertEqual(self.introspect(tokens["access_token"]).json(), {"active": False})
        self.assertFalse(self.authorize(tokens["access_token"]).json()["allowed"])
        response = self.client.get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, **self.basic())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_revocation_between_consent_and_token_exchange(self):
        page, verifier, params = self.start_flow()
        consent = {field.name: field.value() or "" for field in page.context["form"]}
        consent["allow"] = "true"
        response = self.client.post("/oauth/authorize/", consent)
        code = parse_qs(urlparse(response["Location"]).query)["code"][0]
        Membership.objects.filter(pk=self.member_membership.pk).update(active=False)
        response = self.client.post("/oauth/token/", {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.app.redirect_uris, "code_verifier": verifier,
        }, **self.basic())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_wrong_pkce_verifier_cannot_redeem_code(self):
        page, verifier, params = self.start_flow()
        consent = {field.name: field.value() or "" for field in page.context["form"]}
        consent["allow"] = "true"
        response = self.client.post("/oauth/authorize/", consent)
        code = parse_qs(urlparse(response["Location"]).query)["code"][0]
        response = self.client.post("/oauth/token/", {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.app.redirect_uris, "code_verifier": secrets.token_urlsafe(48),
        }, **self.basic())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_public_client_uses_pkce_and_confidential_backend_introspection(self):
        public = self.create_app(self.service, "SPA", self.raw_secret, "public")
        page, verifier, params = self.start_flow(app=public)
        consent = {field.name: field.value() or "" for field in page.context["form"]}
        consent["allow"] = "true"
        response = self.client.post("/oauth/authorize/", consent)
        code = parse_qs(urlparse(response["Location"]).query)["code"][0]
        response = self.client.post("/oauth/token/", {
            "grant_type": "authorization_code", "code": code,
            "client_id": public.client_id, "redirect_uri": public.redirect_uris, "code_verifier": verifier,
        })
        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertTrue(self.introspect(response.json()["access_token"]).json()["active"])

    def test_machine_grant_cannot_assume_human_administrator(self):
        response = self.client.post("/oauth/token/", {"grant_type": "client_credentials", "scope": "openid authorization"}, **self.basic())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unauthorized_client")

    def test_disabled_user_organization_service_or_client_rejects_token(self):
        for obj in [self.member, self.org, self.service, self.app]:
            field = "is_active" if isinstance(obj, User) else "active"
            raw, _ = self.issue_fixture_token()
            setattr(obj, field, False)
            obj.save(update_fields=[field])
            response = self.introspect(raw)
            self.assertIn(response.status_code, [200, 401])
            self.assertFalse(response.json().get("active", False))
            setattr(obj, field, True)
            obj.save(update_fields=[field])

    def test_expired_revoked_unknown_and_id_tokens_are_inactive(self):
        raw, token = self.issue_fixture_token()
        token.expires = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires"])
        self.assertEqual(self.introspect(raw).json(), {"active": False})
        token.delete()
        self.assertEqual(self.introspect(raw).json(), {"active": False})
        self.assertEqual(self.introspect("unknown").json(), {"active": False})
        tokens = self.complete_flow()
        self.assertEqual(self.introspect(tokens["id_token"]).json(), {"active": False})

    def test_refresh_rotates_and_replay_revokes_family(self):
        tokens = self.complete_flow()
        response = self.client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, **self.basic())
        self.assertEqual(response.status_code, 200, response.content.decode())
        refreshed = response.json()
        self.assertNotEqual(tokens["refresh_token"], refreshed["refresh_token"])
        self.assertEqual(self.introspect(tokens["access_token"]).json(), {"active": False})
        self.assertTrue(self.introspect(refreshed["access_token"]).json()["active"])
        replay = self.client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, **self.basic())
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(self.introspect(refreshed["access_token"]).json(), {"active": False})

    def test_revocation_endpoint_invalidates_access(self):
        tokens = self.complete_flow()
        response = self.client.post("/oauth/revoke/", {"token": tokens["access_token"]}, **self.basic())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.introspect(tokens["access_token"]).json(), {"active": False})

    def test_introspection_requires_confidential_client_and_post(self):
        raw, _ = self.issue_fixture_token()
        self.assertEqual(self.client.post("/oauth/introspect/", {"token": raw}).status_code, 401)
        self.assertEqual(self.client.post("/oauth/introspect/", {"token": raw}, **self.basic(secret="wrong")).status_code, 401)
        self.assertEqual(self.client.get("/oauth/introspect/", {"token": raw}, **self.basic()).status_code, 405)
        public = self.create_app(self.service, "Public app", self.raw_secret, "public")
        self.assertEqual(self.introspect(raw, public).status_code, 401)
        self.assertEqual(self.client.post("/oauth/introspect/", **self.basic()).status_code, 400)

    def test_member_cannot_access_org_or_platform_administration(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("organization", args=["north"])).status_code, 403)
        response = self.client.get("/platform-admin/")
        self.assertEqual(response.status_code, 302)

    def test_admin_portal_is_tenant_scoped(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("organization", args=["north"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "North Access")
        self.assertEqual(self.client.get(reverse("organization", args=["south"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("membership", args=["north", self.other_membership.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("service", args=["north", self.other_service.pk])).status_code, 404)

    def test_last_administrator_cannot_be_removed(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("membership", args=["north", self.admin_membership.pk]), {"kind": "member"})
        self.assertContains(response, "at least one active organization administrator")
        self.admin_membership.refresh_from_db()
        self.assertEqual(self.admin_membership.kind, "administrator")
        self.assertTrue(self.admin_membership.active)

    def test_membership_form_rejects_foreign_role(self):
        role = Role.objects.create(organization=self.other_org, slug="foreign", name="Foreign")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("membership", args=["north", self.member_membership.pk]), {
            "kind": "member", "active": "on", "roles": [role.pk],
        })
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(self.member_membership.roles.filter(pk=role.pk).exists())

    def test_role_form_rejects_foreign_permission_and_reserved_name(self):
        permission = ServicePermission.objects.create(service=self.other_service, codename="read", name="Read")
        self.client.force_login(self.admin)
        url = reverse("role_add", args=["north"])
        response = self.client.post(url, {"slug": "test", "name": "Test", "permissions": [permission.pk]})
        self.assertContains(response, "Select a valid choice")
        response = self.client.post(url, {"slug": "administrator", "name": "Test"})
        self.assertContains(response, "reserved")

    def test_branding_is_customizable_without_issuer_change(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("branding", args=["north"]), {"name": "North", "identity_name": "My Company Login"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/.well-known/openid-configuration").json()["issuer"], settings.ISSUER)
        self.assertContains(self.client.get(reverse("organization", args=["north"])), "My Company Login")
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="organization.updated").exists())

    def test_registration_hashes_secret_and_does_not_allow_security_overrides(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("application_add", args=["north", self.service.pk]), {
            "name": "New", "client_type": "confidential", "redirect_uris": "https://app.example.test/new", "active": "on",
            "skip_authorization": "on", "hash_client_secret": "", "algorithm": "HS256", "authorization_grant_type": "password",
        })
        self.assertEqual(response.status_code, 200)
        app = Application.objects.get(name="New")
        self.assertEqual(app.algorithm, "RS256")
        self.assertEqual(app.authorization_grant_type, "authorization-code")
        self.assertFalse(app.skip_authorization)
        self.assertTrue(app.hash_client_secret)
        secret = response.context["client_secret"]
        self.assertTrue(check_password(secret, app.client_secret))
        self.assertEqual(response["Cache-Control"].split(",")[-1].strip(), "private")
        self.assertIn("no-store", response["Cache-Control"])

    def test_registration_rejects_external_http_redirect_and_reserved_permission(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("application_add", args=["north", self.service.pk]), {
            "name": "Unsafe", "client_type": "confidential", "redirect_uris": "http://external.test/callback", "active": "on",
        })
        self.assertContains(response, "HTTPS")
        self.assertFalse(Application.objects.filter(name="Unsafe").exists())
        response = self.client.post(reverse("permission_add", args=["north", self.service.pk]), {"codename": "admin", "name": "Admin"})
        self.assertContains(response, "reserved")

    def test_secret_rotation_invalidates_previous_secret(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("rotate_client_secret", args=["north", self.service.pk, self.app.pk]))
        new_secret = response.context["client_secret"]
        raw, _ = self.issue_fixture_token()
        self.assertEqual(self.introspect(raw).status_code, 401)
        response = self.client.post("/oauth/introspect/", {"token": raw}, **self.basic(secret=new_secret))
        self.assertTrue(response.json()["active"])

    def test_invitation_is_hashed_single_use_and_scoped(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("invite", args=["north"]), {"email": "New@Example.test", "kind": "member"})
        link = response.context["invitation_link"]
        invitation = Invitation.objects.get(email="new@example.test")
        token = urlparse(link).path.split("/")[-2]
        self.assertNotEqual(invitation.token_hash, token)
        self.assertEqual(invitation.token_hash, Invitation.digest(token))
        visitor = Client()
        response = visitor.post(urlparse(link).path, {
            "password1": "invited-test-password-long", "password2": "invited-test-password-long",
            "first_name": "New", "last_name": "User",
        })
        self.assertEqual(response.status_code, 302, response.content.decode())
        user = User.objects.get(email="new@example.test")
        self.assertTrue(Membership.objects.filter(user=user, organization=self.org, kind="member").exists())
        self.assertFalse(Membership.objects.filter(user=user, organization=self.other_org).exists())
        self.assertEqual(visitor.get(urlparse(link).path).status_code, 410)

    def test_existing_user_invitation_requires_matching_account(self):
        raw = secrets.token_urlsafe(32)
        invitation = Invitation.objects.create(organization=self.org, email=self.outsider.email, token_hash=Invitation.digest(raw),
                                                expires=timezone.now() + timedelta(hours=1))
        path = reverse("accept_invitation", args=[raw])
        self.assertEqual(self.client.get(path).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(path).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(path).status_code, 302)
        self.assertTrue(Membership.objects.filter(user=self.outsider, organization=self.org).exists())

    def test_invitation_cannot_change_existing_membership(self):
        raw = secrets.token_urlsafe(32)
        Invitation.objects.create(organization=self.org, email=self.member.email, kind="administrator",
                                  token_hash=Invitation.digest(raw), expires=timezone.now() + timedelta(hours=1))
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("accept_invitation", args=[raw])).status_code, 302)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.kind, "member")

    def test_expired_and_revoked_invitations(self):
        raw = secrets.token_urlsafe(32)
        invitation = Invitation.objects.create(organization=self.org, email="new@example.test", token_hash=Invitation.digest(raw),
                                                expires=timezone.now() - timedelta(seconds=1))
        path = reverse("accept_invitation", args=[raw])
        self.assertEqual(self.client.get(path).status_code, 410)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("revoke_invitation", args=["north", invitation.pk])).status_code, 302)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_administration_and_invitation_acceptance_require_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(reverse("branding", args=["north"]), {"name": "Changed"}).status_code, 403)
        raw = secrets.token_urlsafe(32)
        Invitation.objects.create(organization=self.org, email=self.member.email, token_hash=Invitation.digest(raw),
                                  expires=timezone.now() + timedelta(hours=1))
        browser.force_login(self.member)
        self.assertEqual(browser.post(reverse("accept_invitation", args=[raw])).status_code, 403)

    def test_platform_superuser_has_no_implicit_tenant_token_privileges(self):
        User.objects.filter(pk=self.outsider.pk).update(is_superuser=True, is_staff=True)
        self.outsider.refresh_from_db()
        response, _, _ = self.start_flow(user=self.outsider)
        self.assertEqual(response.status_code, 403)

    def test_health_checks_database(self):
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})
