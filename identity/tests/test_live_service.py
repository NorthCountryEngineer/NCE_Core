import secrets
from datetime import timedelta
from threading import Thread
from wsgiref.simple_server import make_server

import requests
from django.test import LiveServerTestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from examples.protected_service import make_application
from identity.models import Application, Membership, Organization, Role, Service, ServicePermission, User
from identity_client import ResourceClient


class FederatedServiceTests(LiveServerTestCase):
    def test_real_http_service_enforces_current_central_policy(self):
        organization = Organization.objects.create(name="Live tenant", slug="live")
        service = Service.objects.create(organization=organization, name="Reports", slug="reports")
        permission = ServicePermission.objects.create(service=service, codename="read", name="Read")
        role = Role.objects.create(organization=organization, slug="reader", name="Reader")
        role.permissions.add(permission)
        user = User.objects.create_user("live@example.test")
        membership = Membership.objects.create(user=user, organization=organization)
        membership.roles.add(role)
        raw_secret = secrets.token_urlsafe(32)
        client = Application.objects.create(service=service, name="Backend", client_type="confidential",
            authorization_grant_type="authorization-code", algorithm="RS256", client_secret=raw_secret,
            redirect_uris="https://app.example.test/callback")
        raw_token = secrets.token_urlsafe(32)
        token = AccessToken(application=client, user=user, scope="openid authorization",
                            expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, raw_token)
        token.save()
        reference = ResourceClient(self.live_server_url, client.client_id, raw_secret,
                                    str(organization.pk), str(service.pk), allow_local_http=True)
        with override_settings(ISSUER=self.live_server_url):
            server = make_server("127.0.0.1", 0, make_application(reference))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                headers = {"Authorization": f"Bearer {raw_token}"}
                response = requests.get(base + "/reports", headers=headers, timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["organization_id"], str(organization.pk))
                self.assertEqual(requests.get(base + "/admin", headers=headers, timeout=5).status_code, 403)
                Membership.objects.filter(pk=membership.pk).update(kind="administrator")
                self.assertEqual(requests.get(base + "/admin", headers=headers, timeout=5).status_code, 200)
                Membership.objects.filter(pk=membership.pk).update(active=False)
                self.assertEqual(requests.get(base + "/admin", headers=headers, timeout=5).status_code, 401)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
