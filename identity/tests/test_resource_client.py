import time
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from examples.protected_service import make_application
from identity_client import Forbidden, IdentityUnavailable, ResourceClient, Unauthenticated


class ResourceClientTests(SimpleTestCase):
    def setUp(self):
        self.client = ResourceClient("https://identity.example.test", "backend-client", "test-secret", "org", "svc")
        self.claims = {"active": True, "iss": self.client.issuer, "organization_id": "org", "service_id": "svc",
                       "aud": "urn:core:service:svc", "exp": int(time.time()) + 300, "sub": "user", "permissions": ["read"]}

    def response(self, claims=None, status=200):
        return Mock(status_code=status, json=Mock(return_value=self.claims if claims is None else claims))

    def test_requires_current_permission_and_pins_context(self):
        with patch("identity_client.requests.post", return_value=self.response()) as post:
            self.assertEqual(self.client.require("access-token", "read")["sub"], "user")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertEqual(post.call_args.kwargs["timeout"], 5.0)
            with self.assertRaises(Forbidden):
                self.client.require("access-token", "admin")
            for field in ["iss", "organization_id", "service_id", "aud", "exp", "sub"]:
                claims = {**self.claims, field: "" if field == "sub" else "wrong"}
                post.return_value = self.response(claims)
                with self.assertRaises(Unauthenticated):
                    self.client.require("access-token", "read")

    def test_network_failure_and_malformed_response_fail_closed(self):
        with patch("identity_client.requests.post", side_effect=requests.Timeout):
            with self.assertRaises(IdentityUnavailable):
                self.client.require("access-token", "read")
        for status in [301, 401, 500]:
            with patch("identity_client.requests.post", return_value=self.response(status=status)):
                with self.assertRaises(IdentityUnavailable):
                    self.client.require("access-token", "read")
        with patch("identity_client.requests.post", return_value=self.response({**self.claims, "permissions": "read"})):
            with self.assertRaises(IdentityUnavailable):
                self.client.require("access-token", "read")
        with patch("identity_client.requests.post", return_value=self.response({"active": False})):
            with self.assertRaises(Unauthenticated):
                self.client.require("access-token", "read")

    def test_requires_tls_except_explicit_loopback_development(self):
        for issuer in ["http://identity.example.test", "https://example.test/path", "https://user:pass@example.test"]:
            with self.assertRaises(ValueError):
                ResourceClient(issuer, "client", "secret", "org", "svc", allow_local_http=True)
        ResourceClient("http://localhost:8000", "client", "secret", "org", "svc", allow_local_http=True)

    def test_registered_resource_decision_checks_subject_purpose_and_fails_closed(self):
        with patch("identity_client.requests.post", side_effect=[self.response(), self.response({"allowed": True, "subject": "user"})]) as post:
            self.client.require("access-token", "read", purpose_code="treatment", resource_type="record", resource_id="opaque-id")
            self.assertEqual(post.call_args.kwargs["data"]["organization_id"], "org")
            self.assertEqual(post.call_args.kwargs["data"]["purpose_code"], "treatment")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
        for value, exception in [({"allowed": False, "subject": None}, Forbidden),
                                 ({"allowed": True, "subject": "other-user"}, IdentityUnavailable),
                                 ({"allowed": "true", "subject": "user"}, IdentityUnavailable)]:
            with patch("identity_client.requests.post", side_effect=[self.response(), self.response(value)]):
                with self.assertRaises(exception):
                    self.client.require("access-token", "read", resource_type="record", resource_id="opaque-id")
        with patch("identity_client.requests.post", side_effect=[self.response(), requests.Timeout]):
            with self.assertRaises(IdentityUnavailable):
                self.client.require("access-token", "read", resource_type="record", resource_id="opaque-id")

    def test_example_service_admin_requires_admin_permission(self):
        app = make_application(self.client)
        with patch("identity_client.requests.post", return_value=self.response()):
            status = []
            start = lambda response_status, headers: status.append(response_status)
            app({"REQUEST_METHOD": "GET", "PATH_INFO": "/admin", "HTTP_AUTHORIZATION": "Bearer access-token"}, start)
            self.assertEqual(status[-1], "403 Forbidden")
            app({"REQUEST_METHOD": "GET", "PATH_INFO": "/reports", "HTTP_AUTHORIZATION": "Bearer access-token"}, start)
            self.assertEqual(status[-1], "200 OK")
