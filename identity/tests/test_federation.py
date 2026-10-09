import base64
import hashlib
import json
import secrets
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from identity.access import authentication_valid
from identity.federation import FederationError, trusted_url, verify_id_token
from identity.models import AuthenticationEvidence, AuthenticationPolicy, FederatedIdentity, IdentityProvider, Membership, Organization, User


@override_settings(DEBUG=True)
class FederationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="test-key", alg="RS256", use="sig")
        cls.codes = {}
        cls.claim_overrides = {}
        cls.secret = secrets.token_urlsafe(32)
        outer = cls

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path)
                if path.path.endswith("/.well-known/openid-configuration"):
                    self.send_json({"issuer": outer.issuer, "authorization_endpoint": outer.issuer + "/authorize",
                        "token_endpoint": outer.issuer + "/token", "jwks_uri": outer.issuer + "/keys"})
                elif path.path.endswith("/keys"):
                    self.send_json({"keys": [outer.jwk]})
                elif path.path.endswith("/authorize"):
                    values = {key: value[0] for key, value in parse_qs(path.query).items()}
                    if values.get("code_challenge_method") != "S256":
                        return self.send_json({}, 400)
                    code = secrets.token_urlsafe(24)
                    outer.codes[code] = values
                    self.send_response(302)
                    self.send_header("Location", values["redirect_uri"] + "?" + urlencode({"code": code, "state": values["state"]}))
                    self.end_headers()
                else:
                    self.send_json({}, 404)

            def do_POST(self):
                values = {key: value[0] for key, value in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                attempt = outer.codes.pop(values.get("code"), None)
                expected = "Basic " + base64.b64encode(("client:" + outer.secret).encode()).decode()
                challenge = base64.urlsafe_b64encode(hashlib.sha256(values.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if (not attempt or self.headers.get("Authorization") != expected or challenge != attempt["code_challenge"] or
                        values.get("redirect_uri") != attempt["redirect_uri"]):
                    return self.send_json({}, 400)
                claims = {"iss": outer.issuer, "aud": "client", "sub": "member-subject", "nonce": attempt["nonce"],
                    "iat": int(time.time()), "exp": int(time.time()) + 300, "auth_time": int(time.time()),
                    "amr": ["pwd", "mfa"], "email": "untrusted-email@example.test", "groups": ["administrator"], **outer.claim_overrides}
                self.send_json({"id_token": jwt.encode(claims, outer.key, algorithm="RS256", headers={"kid": "test-key"}),
                                "access_token": "upstream-token-must-not-persist"})

        cls.server = HTTPServer(("127.0.0.1", 0), Provider)
        cls.issuer = f"http://127.0.0.1:{cls.server.server_port}/realm"
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.directory = tempfile.TemporaryDirectory()
        cls.override = override_settings(CORE_IDP_SECRET_ROOT=cls.directory.name)
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.directory.cleanup()
        super().tearDownClass()

    def setUp(self):
        type(self).claim_overrides = {}
        self.org = Organization.objects.create(name="Federation tenant", slug="federation")
        folder = Path(self.directory.name) / str(self.org.pk)
        folder.mkdir(mode=0o700)
        self.secret_path = folder / "client.secret"
        self.secret_path.write_text(self.secret)
        self.secret_path.chmod(0o600)
        self.user = User.objects.create_user("linked@example.test")
        self.membership = Membership.objects.create(organization=self.org, user=self.user)
        self.provider = IdentityProvider.objects.create(organization=self.org, slug="keycloak", name="Realm sign-in", kind="keycloak",
            issuer=self.issuer, client_id="client", client_secret_file=str(self.secret_path), active=True, mfa_amr=["mfa"])
        self.identity = FederatedIdentity.objects.create(provider=self.provider, subject="member-subject", user=self.user)

    def attempt(self, client=None, **parameters):
        client = client or self.client
        start = client.get(reverse("provider_begin", args=[self.provider.pk]), parameters)
        self.assertEqual(start.status_code, 302)
        upstream = requests.get(start["Location"], timeout=5, allow_redirects=False)
        callback = urlparse(upstream.headers["Location"])
        return client, callback.path + "?" + callback.query

    def test_real_http_oidc_code_pkce_signature_links_only_existing_subject(self):
        client, callback = self.attempt(next="/organizations/federation/")
        response = client.get(callback)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/organizations/federation/")
        evidence = AuthenticationEvidence.objects.get(pk=client.session["authentication_evidence"])
        self.assertEqual(evidence.user_id, self.user.pk)
        self.assertTrue(evidence.mfa)
        self.assertEqual(evidence.identity_id, self.identity.pk)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.kind, "member")
        self.assertFalse(User.objects.filter(email="untrusted-email@example.test").exists())
        self.assertNotIn("upstream-token", json.dumps(dict(client.session)))
        self.assertEqual(client.get(callback).status_code, 400)

    def test_wrong_state_consumes_attempt_and_never_authenticates(self):
        client, callback = self.attempt()
        params = parse_qs(urlparse(callback).query)
        response = client.get(urlparse(callback).path, {"code": params["code"][0], "state": "wrong"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(client.get(callback).status_code, 400)

    def test_unlinked_verified_email_cannot_take_over_existing_account(self):
        type(self).claim_overrides = {"sub": "unknown-subject", "email": self.user.email, "email_verified": True}
        client, callback = self.attempt()
        self.assertEqual(client.get(callback).status_code, 403)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(User.objects.count(), 1)

    def test_identity_claim_binding_nonce_audience_issuer_and_expiry(self):
        for claims in [{"nonce": "wrong"}, {"aud": "wrong"}, {"iss": "https://attacker.example"}, {"exp": 1},
                       {"aud": ["client", "another"]}, {"amr": "mfa"}]:
            with self.subTest(claims=claims):
                type(self).claim_overrides = claims
                client = Client()
                _, callback = self.attempt(client)
                self.assertEqual(client.get(callback).status_code, 403)
                self.assertNotIn("_auth_user_id", client.session)

    def test_session_binding_blocks_other_browser_and_account_switching(self):
        client, callback = self.attempt()
        self.assertEqual(Client().get(callback).status_code, 400)
        outsider = User.objects.create_user("different@example.test")
        client.force_login(outsider)
        self.assertEqual(client.get(callback).status_code, 403)
        self.assertEqual(client.session["_auth_user_id"], str(outsider.pk))

    def test_provider_and_link_changes_revoke_existing_evidence(self):
        client, callback = self.attempt()
        client.get(callback)
        evidence = AuthenticationEvidence.objects.get(pk=client.session["authentication_evidence"])
        self.assertTrue(authentication_valid(self.user, self.org, evidence))
        self.provider.active = False
        self.provider.save()
        self.provider.active = True
        self.provider.save()
        evidence = AuthenticationEvidence.objects.select_related("identity__provider").get(pk=evidence.pk)
        self.assertFalse(authentication_valid(self.user, self.org, evidence))
        self.provider.issuer = "http://localhost:9000/other"
        with self.assertRaises(ValidationError):
            self.provider.save()
        self.identity.active = False
        self.identity.save()
        evidence.refresh_from_db()
        self.assertTrue(evidence.revoked)

    def test_provider_urls_and_secrets_remain_operator_scoped(self):
        with override_settings(DEBUG=False, CORE_IDP_ALLOWED_HOSTS=["keycloak.example.test"]):
            for value in ["http://keycloak.example.test/realms/main", "https://evil.example.test", "https://keycloak.example.test@evil.example.test"]:
                with self.assertRaises(ValidationError):
                    trusted_url(value)
            for value in ["https://keycloak.example.test/realms/main", "https://login.microsoftonline.com/tenant/v2.0", "https://cognito-idp.us-east-1.amazonaws.com/pool"]:
                self.assertEqual(trusted_url(value), value)
        self.provider.client_secret_file = "/etc/passwd"
        with self.assertRaises(ValidationError):
            self.provider.full_clean()
        self.provider.client_secret_file = str(Path(self.directory.name) / "another-organization" / "client.secret")
        with self.assertRaises(ValidationError):
            self.provider.full_clean()

    def test_symmetric_or_unsigned_identity_tokens_are_rejected(self):
        with self.assertRaises(FederationError):
            verify_id_token(self.provider, {"jwks_uri": self.issuer + "/keys"}, jwt.encode({"sub": "x"}, self.secret, algorithm="HS256"), "nonce")

    def test_next_redirect_is_local_and_policy_can_require_mfa(self):
        AuthenticationPolicy.objects.create(organization=self.org, require_federated=True, require_mfa=True)
        type(self).claim_overrides = {"amr": ["pwd"]}
        client, callback = self.attempt(next="https://attacker.example.test/")
        self.assertEqual(client.get(callback).status_code, 403)
        type(self).claim_overrides = {}
        client, callback = self.attempt(next="https://attacker.example.test/")
        self.assertEqual(client.get(callback)["Location"], "/")

    def test_tagged_provider_uses_bound_purpose_without_trusting_callback_override(self):
        from identity.models import AuditEvent, PermissionTag
        tag = PermissionTag.objects.create(organization=self.org, slug="provider-control", name="Provider control",
            requires_mfa=True, allowed_purposes=["treatment"])
        self.provider.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        browser, callback = self.attempt(purpose_code="research")
        self.assertEqual(browser.get(callback + "&purpose_code=treatment").status_code, 403)
        browser, callback = self.attempt(purpose_code="treatment")
        self.assertEqual(browser.get(callback + "&purpose_code=research").status_code, 302)
        self.assertEqual(browser.session["access_purposes"][str(self.org.pk)], "treatment")
        self.assertEqual(AuditEvent.objects.filter(action="authentication.provider_access", outcome="allowed").get().purpose_code, "treatment")

    def test_fresh_authentication_request_rejects_stale_provider_session(self):
        type(self).claim_overrides = {"auth_time": int(time.time()) - 300}
        client, callback = self.attempt(reauth="1")
        self.assertEqual(client.get(callback).status_code, 403)

    def test_federated_assurance_survives_core_oidc_exchange_and_refresh_revocation(self):
        from identity.models import Application, OAuthEvidence, PermissionTag, Service
        from django.conf import settings
        client, callback = self.attempt()
        client.get(callback)
        service = Service.objects.create(organization=self.org, name="Registered application", slug="registered")
        secret = secrets.token_urlsafe(32)
        application = Application.objects.create(service=service, name="Client", client_type="confidential", client_secret=secret,
            authorization_grant_type="authorization-code", algorithm="RS256", redirect_uris="https://app.example.test/callback")
        tag = PermissionTag.objects.create(organization=self.org, slug="restricted-login", name="Protected application",
            requires_mfa=True, max_auth_age=300, allowed_purposes=["treatment"])
        application.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        client.post(reverse("access_purpose", args=[self.org.slug]), {"purpose_code": "treatment"})
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        page = client.get("/oauth/authorize/", {"client_id": application.client_id, "response_type": "code", "redirect_uri": application.redirect_uris,
            "scope": "openid authorization", "state": "app-state", "nonce": "app-nonce", "code_challenge_method": "S256", "code_challenge": challenge})
        self.assertEqual(page.status_code, 200)
        values = {field.name: field.value() or "" for field in page.context["form"]}
        values["allow"] = "true"
        response = client.post("/oauth/authorize/", values)
        code = parse_qs(urlparse(response["Location"]).query)["code"][0]
        headers = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{application.client_id}:{secret}".encode()).decode()}
        tokens_response = client.post("/oauth/token/", {"grant_type": "authorization_code", "code": code,
            "redirect_uri": application.redirect_uris, "code_verifier": verifier}, **headers)
        self.assertEqual(tokens_response.status_code, 200, tokens_response.content.decode())
        tokens = tokens_response.json()
        binding = OAuthEvidence.objects.get(token_hash=hashlib.sha256(tokens["access_token"].encode()).hexdigest())
        self.assertEqual(binding.evidence.identity_id, self.identity.pk)
        self.assertEqual(binding.purpose_code, "treatment")
        self.assertEqual(client.post("/oauth/introspect/", {"token": tokens["access_token"], "purpose_code": "treatment"}, **headers).json()["authentication"]["mfa"], True)
        jwk = client.get("/oauth/jwks/").json()["keys"][0]
        claims = jwt.decode(tokens["id_token"], jwt.PyJWK.from_dict(jwk).key, algorithms=["RS256"], audience=application.client_id, issuer=settings.ISSUER)
        self.assertEqual(claims["amr"], ["mfa"])
        self.assertEqual(claims["acr"], "urn:core:assurance:mfa")
        refresh = client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, **headers)
        self.assertEqual(refresh.status_code, 200)
        new = refresh.json()
        self.assertEqual(OAuthEvidence.objects.get(token_hash=hashlib.sha256(new["access_token"].encode()).hexdigest()).evidence_id, binding.evidence_id)
        self.membership.access_tags.clear()
        self.assertFalse(client.post("/oauth/introspect/", {"token": new["access_token"], "purpose_code": "treatment"}, **headers).json()["active"])
        self.assertEqual(client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": new["refresh_token"]}, **headers).status_code, 400)
        self.membership.access_tags.add(tag)
        self.provider.active = False
        self.provider.save()
        self.assertFalse(client.post("/oauth/introspect/", {"token": new["access_token"]}, **headers).json()["active"])
        self.assertEqual(client.post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": new["refresh_token"]}, **headers).status_code, 400)
