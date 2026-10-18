"""Saved OAuth approval never substitutes for authentication or live app access."""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

from django.db import DatabaseError, close_old_connections, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model, set_token_value

from identity.consent import RememberedConsentAuthorizationView
from identity.oauth import OrganizationValidator
from identity.models import (
    Application, ApplicationConsent, AuthenticationEvidence, AuthenticationPolicy, AuditEvent,
    Membership, Organization, PermissionTag, Role, Service, ServicePermission, User,
)


class RememberedConsentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(slug="consent", name="Consent tests")
        cls.service = Service.objects.create(organization=cls.organization, slug="news", name="News")
        cls.user = User.objects.create_user("consent@example.test", secrets.token_urlsafe(32))
        cls.other_user = User.objects.create_user("other-consent@example.test", secrets.token_urlsafe(32))
        cls.membership = Membership.objects.create(organization=cls.organization, user=cls.user)
        Membership.objects.create(organization=cls.organization, user=cls.other_user)
        cls.application = cls.make_application(cls.service, "News browser")
        cls.other_application = cls.make_application(cls.service, "Another browser")
        cls.resource_secret = secrets.token_urlsafe(32)
        cls.resource_application = Application(service=cls.service, name="Resource backend", client_type="confidential",
            client_secret=cls.resource_secret, authorization_grant_type="authorization-code", algorithm="RS256",
            redirect_uris="https://news.example.test/resource-callback")
        cls.resource_application.full_clean()
        cls.resource_application.save()
        permission = ServicePermission.objects.create(service=cls.service, codename="read", name="Read")
        cls.role = Role.objects.create(organization=cls.organization, slug="reader", name="Reader")
        cls.role.permissions.add(permission)
        cls.membership.roles.add(cls.role)

    @staticmethod
    def make_application(service, name):
        application = Application(service=service, name=name, client_type="public",
            authorization_grant_type="authorization-code", algorithm="RS256",
            redirect_uris="https://news.example.test/callback", allowed_origins="https://news.example.test")
        application.full_clean()
        application.save()
        return application

    def setUp(self):
        self.client.force_login(self.user)

    def start(self, application=None, **overrides):
        application = application or self.application
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": application.client_id, "redirect_uri": application.redirect_uris.split()[0],
            "response_type": "code", "scope": "openid authorization", "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(24), "code_challenge": challenge, "code_challenge_method": "S256", **overrides}
        return self.client.get("/oauth/authorize/", params), verifier, params

    def approve(self, **overrides):
        page, verifier, params = self.start(**overrides)
        self.assertEqual(page.status_code, 200, page.content.decode())
        values = {field.name: field.value() or "" for field in page.context["form"]}
        values["allow"] = "true"
        response = self.client.post("/oauth/authorize/", values)
        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertIn("code", parse_qs(urlparse(response.url).query))
        return response, verifier, params

    def redeem(self, response, verifier, params):
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query["state"], [params["state"]])
        return self.client.post("/oauth/token/", {"client_id": params["client_id"], "grant_type": "authorization_code",
            "code": query["code"][0], "redirect_uri": params["redirect_uri"], "code_verifier": verifier})

    def test_explicit_consent_survives_token_expiry_logout_and_fresh_authentication(self):
        response, verifier, params = self.approve()
        tokens = self.redeem(response, verifier, params)
        self.assertEqual(tokens.status_code, 200, tokens.content.decode())
        get_access_token_model().objects.update(expires=timezone.now() - timedelta(seconds=1))
        evidence = self.client.session["authentication_evidence"]
        self.client.post(reverse("logout"))
        self.assertTrue(AuthenticationEvidence.objects.get(pk=evidence).revoked)
        self.client.force_login(self.user)
        response, verifier, params = self.start()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.redeem(response, verifier, params).status_code, 200)
        self.assertEqual(ApplicationConsent.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="oauth.consent_granted").count(), 1)

    def test_scope_order_is_canonical_but_changed_scopes_require_approval(self):
        self.approve()
        self.assertEqual(ApplicationConsent.objects.get().scope, "authorization openid")
        self.assertEqual(self.start(scope="authorization openid")[0].status_code, 302)
        for scope in ["openid", "openid authorization profile", "openid authorization email"]:
            with self.subTest(scope=scope):
                self.assertEqual(self.start(scope=scope, approval_prompt="auto")[0].status_code, 200)

    def test_approval_is_bound_to_user_and_application(self):
        self.approve()
        self.assertEqual(self.start(application=self.other_application)[0].status_code, 200)
        self.client.force_login(self.other_user)
        self.assertEqual(self.start()[0].status_code, 200)

    def test_changed_registration_or_requested_claims_require_new_approval(self):
        self.approve()
        self.assertEqual(self.start(claims='{"userinfo":{"name":null}}')[0].status_code, 200)
        for field, value in [("name", "Changed recipient"), ("redirect_uris", "https://news.example.test/new-callback"),
                             ("allowed_origins", "https://news.example.test https://other.example.test")]:
            with self.subTest(field=field):
                original = getattr(self.application, field)
                setattr(self.application, field, value)
                self.application.save(update_fields=[field])
                self.assertEqual(self.start(approval_prompt="auto")[0].status_code, 200)
                setattr(self.application, field, original)
                self.application.save(update_fields=[field])

    def test_force_and_consent_prompts_are_respected(self):
        self.approve()
        self.assertEqual(self.start(approval_prompt="force")[0].status_code, 200)
        self.assertEqual(self.start(prompt="consent", approval_prompt="auto")[0].status_code, 200)
        self.assertEqual(self.start(prompt="login")[0].status_code, 302)
        self.assertIn("/accounts/login/", self.start(prompt="login")[0].url)

    def test_prompt_none_needs_prior_consent_and_reuses_it_after_approval(self):
        response, _, params = self.start(prompt="none")
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query["error"], ["consent_required"])
        self.assertEqual(query["state"], [params["state"]])
        self.approve()
        self.assertIn("code", parse_qs(urlparse(self.start(prompt="none")[0].url).query))
        response, _, params = self.start(prompt="none", approval_prompt="force")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlparse(response.url).query)["error"], ["consent_required"])
        self.assertEqual(parse_qs(urlparse(response.url).query)["state"], [params["state"]])

    def test_signed_out_prompt_none_returns_login_required_to_registered_callback(self):
        self.client.logout()
        response, _, params = self.start(prompt="none")
        self.assertEqual(response.status_code, 302)
        location = urlparse(response.url)
        callback = urlparse(self.application.redirect_uris)
        self.assertEqual((location.scheme, location.netloc, location.path), (callback.scheme, callback.netloc, callback.path))
        self.assertEqual(parse_qs(location.query)["error"], ["login_required"])
        self.assertEqual(parse_qs(location.query)["state"], [params["state"]])
        self.assertFalse(ApplicationConsent.objects.exists())
        self.assertFalse(get_grant_model().objects.exists())

    def test_signed_out_prompt_none_never_redirects_to_unregistered_callback(self):
        self.client.logout()
        for changes in [{"redirect_uri": "https://evil.example/callback"}, {"client_id": "unknown"},
                        {"client_id": "", "redirect_uri": "https://evil.example/callback"}]:
            with self.subTest(changes=changes):
                response, _, _ = self.start(prompt="none", **changes)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("Location", response)
        self.assertFalse(get_grant_model().objects.exists())

    def test_signed_out_conflicting_silent_prompts_return_error_without_login_ui(self):
        self.client.logout()
        for prompt in ["none login", "none consent"]:
            with self.subTest(prompt=prompt):
                response, _, params = self.start(prompt=prompt)
                self.assertEqual(response.status_code, 302)
                query = parse_qs(urlparse(response.url).query)
                self.assertEqual(query["error"], ["invalid_request"])
                self.assertEqual(query["state"], [params["state"]])
                self.assertTrue(response.url.startswith(self.application.redirect_uris + "?"))

    def test_malformed_claims_get_and_post_never_grant_or_return_server_error(self):
        page, _, params = self.start()
        values = {field.name: field.value() or "" for field in page.context["form"]}
        malformed = ['"foo"', '[]', 'null', 'false', '5', '{', '{"userinfo":[]}',
                     '{"userinfo":{"name":true}}', '{"id_token":{"name":{"essential":"yes"}}}',
                     '{"id_token":{"name":{"values":"one"}}}']
        for claims in malformed:
            with self.subTest(claims=claims):
                response = self.client.get("/oauth/authorize/", {**params, "claims": claims})
                self.assertEqual(response.status_code, 400, response.content.decode())
                self.assertEqual(response.json()["error"], "invalid_request")
                response = self.client.post("/oauth/authorize/", {**values, "claims": claims, "allow": "true"})
                self.assertEqual(response.status_code, 400, response.content.decode())
                self.assertEqual(response.json()["error"], "invalid_request")
                self.assertFalse(ApplicationConsent.objects.exists())
                self.assertFalse(get_grant_model().objects.exists())

    def test_oidc_claim_objects_are_remembered_with_canonical_json(self):
        self.approve(claims='{"userinfo":{"name":{"essential":true}},"id_token":{"given_name":null}}')
        response, _, _ = self.start(claims='{"id_token":{"given_name":null},"userinfo":{"name":{"essential":true}}}')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.start(claims='{"userinfo":{"email":null}}')[0].status_code, 200)

    def test_oversized_and_deep_claims_fail_cleanly_before_or_after_login(self):
        page, _, params = self.start()
        values = {field.name: field.value() or "" for field in page.context["form"]}
        claims_requests = [
            json.dumps({"userinfo": {"name": {"value": "x" * 16384}}}),
            json.dumps({"userinfo": {"name": {"value": "é" * 9000}}}, ensure_ascii=False),
            '{"userinfo":{"name":{"value":' + '[' * 9 + '0' + ']' * 9 + '}}}',
            '{"userinfo":{"name":{"value":' + '[' * 2000 + '0' + ']' * 2000 + '}}}',
        ]
        for signed_out in [False, True]:
            if signed_out:
                self.client.logout()
            for claims in claims_requests:
                with self.subTest(signed_out=signed_out, length=len(claims)):
                    response = self.client.get("/oauth/authorize/", {**params, "claims": claims})
                    self.assertEqual(response.status_code, 400, response.content.decode())
                    self.assertEqual(response.json()["error"], "invalid_request")
                    response = self.client.post("/oauth/authorize/", {**values, "claims": claims, "allow": "true"})
                    self.assertEqual(response.status_code, 400, response.content.decode())
                    self.assertEqual(response.json()["error"], "invalid_request")
                    self.assertFalse(ApplicationConsent.objects.exists())
                    self.assertFalse(get_grant_model().objects.exists())

    def test_unexpired_token_alone_cannot_replace_saved_approval(self):
        response, verifier, params = self.approve()
        self.assertEqual(self.redeem(response, verifier, params).status_code, 200)
        ApplicationConsent.objects.all().delete()
        self.assertEqual(self.start(approval_prompt="auto")[0].status_code, 200)

    def test_cancel_missing_allow_and_invalid_fields_never_save_approval(self):
        page, _, _ = self.start()
        values = {field.name: field.value() or "" for field in page.context["form"]}
        for change in [{}, {"allow": "false"}, {"allow": "true", "scope": "openid unknown"},
                       {"allow": "true", "redirect_uri": "https://evil.example/callback"},
                       {"allow": "true", "client_id": "unknown"}, {"allow": "true", "response_type": "token"},
                       {"allow": "true", "code_challenge": ""}]:
            with self.subTest(change=change):
                response = self.client.post("/oauth/authorize/", {**values, **change})
                self.assertLess(response.status_code, 500)
                self.assertFalse(ApplicationConsent.objects.exists())
                self.assertFalse(get_grant_model().objects.exists())

    def test_duplicate_get_and_post_parameters_never_save_approval(self):
        page, _, params = self.start()
        self.assertEqual(self.client.get("/oauth/authorize/?" + urlencode(params) + "&scope=openid").status_code, 400)
        values = {field.name: field.value() or "" for field in page.context["form"]}
        body = urlencode({**values, "allow": "true"}) + "&scope=openid"
        self.assertEqual(self.client.post("/oauth/authorize/", body, content_type="application/x-www-form-urlencoded").status_code, 400)
        self.assertFalse(ApplicationConsent.objects.exists())

    def test_csrf_is_required_for_consent_and_revocation(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        page, _, _ = self.start()
        values = {field.name: field.value() or "" for field in page.context["form"]}
        self.assertEqual(csrf_client.post("/oauth/authorize/", {**values, "allow": "true"}).status_code, 403)
        self.approve()
        consent = ApplicationConsent.objects.get()
        self.assertEqual(csrf_client.post(reverse("revoke_application_consent", args=[consent.pk])).status_code, 403)
        self.assertIsNone(ApplicationConsent.objects.get().revoked_at)

    def test_saved_consent_never_bypasses_disabled_boundaries_or_membership(self):
        self.approve()
        for model, pk, field in [(Application, self.application.pk, "active"), (Service, self.service.pk, "active"),
                                 (Organization, self.organization.pk, "active"), (Membership, self.membership.pk, "active")]:
            with self.subTest(model=model):
                model.objects.filter(pk=pk).update(**{field: False})
                response, _, _ = self.start()
                self.assertIn(response.status_code, [400, 403])
                model.objects.filter(pk=pk).update(**{field: True})
        self.assertEqual(self.start()[0].status_code, 302)

    def test_saved_consent_rechecks_tags_and_authentication_evidence(self):
        self.approve()
        tag = PermissionTag.objects.create(organization=self.organization, slug="boundary", name="App access")
        self.application.permission_tags.add(tag)
        self.assertEqual(self.start()[0].status_code, 403)
        self.membership.access_tags.add(tag)
        self.assertEqual(self.start()[0].status_code, 302)
        AuthenticationPolicy.objects.create(organization=self.organization, require_mfa=True)
        self.assertEqual(self.start()[0].status_code, 403)

    def test_saved_consent_does_not_grant_roles_tags_or_permissions(self):
        self.approve()
        self.membership.roles.clear()
        response, verifier, params = self.start()
        tokens = self.redeem(response, verifier, params)
        info = self.client.get("/oauth/userinfo/", HTTP_AUTHORIZATION="Bearer " + tokens.json()["access_token"])
        self.assertEqual(info.json()["permissions"], [])
        self.assertEqual(self.membership.access_tags.count(), 0)
        self.assertEqual(self.membership.roles.count(), 0)
        self.assertEqual(self.membership.kind, Membership.Kind.MEMBER)

    def test_saved_consent_still_requires_pkce_and_exact_callback(self):
        self.approve()
        for change in [{"code_challenge": ""}, {"code_challenge_method": "plain"}, {"redirect_uri": "https://evil.example/callback"}]:
            with self.subTest(change=change):
                response, _, _ = self.start(**change)
                self.assertNotIn("code", parse_qs(urlparse(getattr(response, "url", "")).query))
        response, _, params = self.start()
        self.assertEqual(self.redeem(response, secrets.token_urlsafe(48), params).status_code, 400)

    def test_revoke_is_post_only_user_owned_and_removes_codes_and_tokens(self):
        response, verifier, params = self.approve()
        tokens = self.redeem(response, verifier, params).json()
        self.start()  # An unredeemed code must be revoked too.
        consent = ApplicationConsent.objects.get()
        url = reverse("revoke_application_consent", args=[consent.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.user)
        self.assertRedirects(self.client.post(url), reverse("profile"))
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revoked_at)
        self.assertFalse(get_grant_model().objects.filter(user=self.user, application=self.application).exists())
        self.assertFalse(get_access_token_model().objects.filter(user=self.user, application=self.application).exists())
        self.assertFalse(get_refresh_token_model().objects.filter(user=self.user, application=self.application).exists())
        self.assertEqual(self.client.get("/oauth/userinfo/", HTTP_AUTHORIZATION="Bearer " + tokens["access_token"]).status_code, 401)
        self.assertEqual(self.client.post("/oauth/token/", {"client_id": self.application.client_id,
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).status_code, 400)
        self.assertEqual(self.start(approval_prompt="auto")[0].status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action="oauth.consent_revoked").count(), 1)

    def test_skip_authorization_flag_cannot_bypass_first_consent(self):
        Application.objects.filter(pk=self.application.pk).update(skip_authorization=True)
        self.assertEqual(self.start()[0].status_code, 400)
        self.assertFalse(ApplicationConsent.objects.exists())

    def test_racing_token_persisted_after_revocation_is_denied_live(self):
        response, verifier, params = self.approve()
        tokens = self.redeem(response, verifier, params).json()
        self.client.post(reverse("revoke_application_consent", args=[ApplicationConsent.objects.get().pk]))
        # Model an issuance that passed validation before revoke and persisted
        # after the revoke endpoint removed the existing credentials.
        access = get_access_token_model()(user=self.user, application=self.application,
            scope="openid authorization", expires=timezone.now() + timedelta(minutes=5))
        set_token_value(access, tokens["access_token"])
        access.save()
        refresh = get_refresh_token_model()(user=self.user, application=self.application, access_token=access)
        set_token_value(refresh, tokens["refresh_token"])
        refresh.save()
        self.assertEqual(self.client.get("/oauth/userinfo/", HTTP_AUTHORIZATION="Bearer " + tokens["access_token"]).status_code, 401)
        self.assertEqual(self.client.post("/oauth/token/", {"client_id": self.application.client_id,
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).status_code, 400)
        basic = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(
            (self.resource_application.client_id + ":" + self.resource_secret).encode()).decode()}
        self.assertEqual(self.client.post("/oauth/introspect/", {"token": tokens["access_token"]}, **basic).json(), {"active": False})
        decision = self.client.post("/api/v1/authorize/", {"token": tokens["access_token"],
            "organization_id": str(self.organization.pk), "permission": "read"}, **basic)
        self.assertFalse(decision.json()["allowed"])
        self.assertTrue(AuditEvent.objects.filter(action="oauth.client_access", reason_code="consent_revoked").exists())

    def test_revoke_or_new_approval_during_refresh_validation_prevents_token_save(self):
        response, verifier, params = self.approve()
        tokens = self.redeem(response, verifier, params).json()
        original = OrganizationValidator._save_bearer_token

        def revoke_then_save(validator, token, request, *args, **kwargs):
            ApplicationConsent.objects.filter(user=self.user, application=self.application).update(revoked_at=timezone.now())
            return original(validator, token, request, *args, **kwargs)

        refresh_request = {"client_id": self.application.client_id, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        count = get_access_token_model().objects.count()
        with patch.object(OrganizationValidator, "_save_bearer_token", revoke_then_save):
            result = self.client.post("/oauth/token/", refresh_request)
        self.assertEqual(result.status_code, 400, result.content.decode())
        self.assertEqual(result.json()["error"], "invalid_grant")
        self.assertEqual(get_access_token_model().objects.count(), count)

        def new_approval_then_save(validator, token, request, *args, **kwargs):
            ApplicationConsent.objects.filter(user=self.user, application=self.application).update(granted_at=timezone.now())
            return original(validator, token, request, *args, **kwargs)

        ApplicationConsent.objects.filter(user=self.user, application=self.application).update(revoked_at=None)
        with patch.object(OrganizationValidator, "_save_bearer_token", new_approval_then_save):
            result = self.client.post("/oauth/token/", refresh_request)
        self.assertEqual(result.status_code, 400, result.content.decode())
        self.assertEqual(result.json()["error"], "invalid_grant")
        self.assertEqual(get_access_token_model().objects.count(), count)


class ConsentIssuanceLockTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_auto_authorization_holds_consent_row_lock_during_code_issuance(self):
        organization = Organization.objects.create(slug="consent-race", name="Consent race")
        service = Service.objects.create(organization=organization, slug="race", name="Race")
        user = User.objects.create_user("consent-race@example.test", secrets.token_urlsafe(32))
        Membership.objects.create(organization=organization, user=user)
        application = RememberedConsentTests.make_application(service, "Race browser")
        self.client.force_login(user)
        verifier = secrets.token_urlsafe(48)
        params = {"client_id": application.client_id, "redirect_uri": application.redirect_uris,
            "response_type": "code", "scope": "openid authorization", "state": "opaque-race-state",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
            "code_challenge_method": "S256"}
        page = self.client.get("/oauth/authorize/", params)
        values = {field.name: field.value() or "" for field in page.context["form"]}
        self.assertEqual(self.client.post("/oauth/authorize/", {**values, "allow": "true"}).status_code, 302)
        consent = ApplicationConsent.objects.get(user=user, application=application)
        locked = []

        def competing_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    ApplicationConsent.objects.select_for_update(nowait=True).get(pk=consent.pk)
                locked.append(False)
            except DatabaseError as error:
                locked.append(getattr(error.__cause__, "sqlstate", None) == "55P03")
            finally:
                connections["default"].close()

        original = RememberedConsentAuthorizationView.create_authorization_response

        def issue(view, *args, **kwargs):
            competitor = Thread(target=competing_lock)
            competitor.start()
            competitor.join(timeout=5)
            self.assertFalse(competitor.is_alive(), "Competing consent lock did not complete")
            return original(view, *args, **kwargs)

        with patch.object(RememberedConsentAuthorizationView, "create_authorization_response", issue):
            response = self.client.get("/oauth/authorize/", params)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(locked, [True], "Issuance must hold the same row lock used by revoke")
