"""Live multi-organization metadata discovery; no hosting or external operations."""
import json
from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase

from identity.models import (ApplicationConsent, AuthenticationEvidence, AuthenticationPolicy, Membership,
                             PermissionTag, Role, ServicePermission)
from platform_services.environments import create_environment
from platform_services.external_apps import external_resource_credentials, register_external_deployment
from platform_services.models import MarketplacePackage
from platform_services.services import install_package
from platform_services.tests import test_external_apps


class ApplicationContextTests(TestCase):
    setUpTestData = classmethod(test_external_apps.ExternalApplicationTests.setUpTestData.__func__)
    basic = test_external_apps.ExternalApplicationTests.basic
    exchange = test_external_apps.ExternalApplicationTests.exchange

    def setUp(self):
        self.workspace = ServicePermission.objects.create(service=self.installation.service,
            codename='workspace-use', name='Use workspace')
        self.role.permissions.add(self.workspace)
        self.target_installation = install_package(self.other, self.package)
        self.target = register_external_deployment(self.target_installation, self.other_environment,
            self.deployment.endpoint)
        self.target_workspace = ServicePermission.objects.create(service=self.target_installation.service,
            codename='workspace-use', name='Use workspace')
        self.target_role = Role.objects.create(organization=self.other, slug='workspace', name='Workspace')
        self.target_role.permissions.add(self.target_workspace)
        self.target_member = Membership.objects.create(organization=self.other, user=self.member)
        self.target_member.roles.add(self.target_role)
        self.target_member.access_tags.add(self.other_environment.boundary_tag)
        self.token = self.exchange()['access_token']

    def discover(self, candidates=None, *, token=None, credentials=None, **headers):
        return self.client.post('/oauth/application-contexts/', json.dumps({
            'token': token or self.token,
            'deployment_ids': candidates if candidates is not None else [str(self.deployment.pk), str(self.target.pk)],
        }), content_type='application/json', **self.basic(credentials), **headers)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Pragma'], 'no-cache')
        self.assertNotIn('Access-Control-Allow-Origin', response)
        return [row['deployment_id'] for row in response.json()['contexts']]

    def test_same_origin_distinct_organizations_have_independent_live_contexts(self):
        before = (Membership.objects.count(), Role.objects.count(), self.target_member.roles.count(),
                  self.target_member.access_tags.count())
        response = self.discover()
        self.assertEqual(self.ids(response), [str(self.deployment.pk), str(self.target.pk)])
        target = response.json()['contexts'][1]
        self.assertEqual(target, {'organization_id': str(self.other.pk), 'organization_name': self.other.name,
            'service_id': str(self.target_installation.service_id), 'deployment_id': str(self.target.pk),
            'browser_client_id': self.target.application.client_id, 'environment': 'production',
            'public_origin': self.deployment.endpoint})
        self.assertNotEqual(self.target.application.client_id, self.browser.client_id)
        self.assertNotIn('secret', json.dumps(response.json()))
        self.assertNotIn(self.token, response.content.decode())
        self.assertNotIn(external_resource_credentials(self.target)['client_secret'], response.content.decode())
        self.assertEqual(before, (Membership.objects.count(), Role.objects.count(), self.target_member.roles.count(),
                                 self.target_member.access_tags.count()))

    def test_only_explicit_candidates_are_considered_and_missing_ids_reveal_nothing(self):
        self.assertEqual(self.ids(self.discover([str(self.target.pk)])), [str(self.target.pk)])
        self.assertEqual(self.ids(self.discover([str(uuid4())])), [])

    def test_source_token_requires_exact_confidential_deployment_pair(self):
        response = self.discover(credentials=external_resource_credentials(self.target))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'invalid_user_token'})
        self.assertEqual(self.discover(token='not-an-access-token').status_code, 401)
        self.assertEqual(self.discover(credentials={'client_id': self.browser.client_id,
                                                   'client_secret': uuid4().hex}).status_code, 401)

    def test_source_requires_exact_workspace_permission_even_with_other_access(self):
        self.role.permissions.remove(self.workspace)
        self.assertEqual(self.discover().status_code, 403)
        self.role.permissions.add(self.workspace)
        self.membership.active = False
        self.membership.save(update_fields=['active'])
        self.assertEqual(self.discover().status_code, 401)

    def test_source_deployment_tags_cannot_be_bypassed_by_a_valid_user_token(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug='source-restricted', name='Restricted')
        self.deployment.permission_tags.add(tag)
        self.assertIn(self.discover().status_code, [401, 403])
        self.membership.access_tags.add(tag)
        self.assertEqual(len(self.ids(self.discover())), 2)

    def test_target_membership_revocation_is_immediate_and_never_granted_by_discovery(self):
        self.assertEqual(len(self.ids(self.discover())), 2)
        self.target_member.active = False
        self.target_member.save(update_fields=['active'])
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.target_member.delete()
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.assertFalse(Membership.objects.filter(organization=self.other, user=self.member).exists())

    def test_target_requires_workspace_permission_and_cannot_inherit_source_roles(self):
        self.target_role.permissions.remove(self.target_workspace)
        extra = ServicePermission.objects.create(service=self.target_installation.service,
            codename='workspace-use-extra', name='Not the exact permission')
        self.target_role.permissions.add(extra)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.target_role.permissions.add(self.target_workspace)
        self.assertEqual(len(self.ids(self.discover())), 2)

    def test_cross_origin_or_different_callback_candidates_are_not_enumerated(self):
        staging = create_environment(self.other, name='staging', namespace='other-staging',
            application_origin='https://elsewhere.example.test')
        foreign = register_external_deployment(self.target_installation, staging, 'https://elsewhere.example.test')
        self.assertEqual(self.ids(self.discover([str(foreign.pk)])), [])
        from identity.models import Application
        Application.objects.filter(pk=self.target.application_id).update(
            redirect_uris=self.deployment.endpoint + '/different/callback')
        self.assertEqual(self.ids(self.discover([str(self.target.pk)])), [])

    def test_mismatched_allowed_origin_is_not_enumerated(self):
        from identity.models import Application
        Application.objects.filter(pk=self.target.application_id).update(allowed_origins='https://elsewhere.example.test')
        self.assertEqual(self.ids(self.discover([str(self.target.pk)])), [])

    def test_target_environment_deployment_clients_and_installation_are_live(self):
        for item, field, inactive, active in [
            (self.other, 'active', False, True),
            (self.other_environment, 'active', False, True),
            (self.target_installation, 'enabled', False, True),
            (self.target_installation.service, 'active', False, True),
            (self.target.application, 'active', False, True),
            (self.target.resource_application, 'active', False, True),
            (self.target, 'desired_state', 'stopped', 'running'),
        ]:
            with self.subTest(model=item._meta.label, field=field):
                type(item).objects.filter(pk=item.pk).update(**{field: inactive})
                self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
                type(item).objects.filter(pk=item.pk).update(**{field: active})
                self.assertEqual(len(self.ids(self.discover())), 2)

    def test_target_environment_and_permission_tags_are_evaluated_in_target_context(self):
        self.target_member.access_tags.clear()
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.target_member.access_tags.add(self.other_environment.boundary_tag)
        tag = PermissionTag.objects.create(organization=self.other, slug='target-private', name='Private')
        self.target_workspace.permission_tags.add(tag)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.target_member.access_tags.add(tag)
        self.assertEqual(len(self.ids(self.discover())), 2)
        PermissionTag.objects.filter(pk=tag.pk).update(active=False)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])

    def test_target_authentication_and_purpose_policy_are_not_inherited_from_source(self):
        tag = PermissionTag.objects.create(organization=self.other, slug='mfa-purpose', name='MFA',
            requires_mfa=True, allowed_purposes=['capture'])
        self.target_member.access_tags.add(tag)
        self.target_workspace.permission_tags.add(tag)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        AuthenticationEvidence.objects.filter(user=self.member).update(mfa=True)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        self.assertEqual(len(self.ids(self.discover(HTTP_X_CORE_PURPOSE='capture'))), 2)
        AuthenticationEvidence.objects.filter(user=self.member).update(revoked=True)
        self.assertEqual(self.discover(HTTP_X_CORE_PURPOSE='capture').status_code, 401)

    def test_target_organization_authentication_policy_is_independent(self):
        AuthenticationPolicy.objects.update_or_create(organization=self.other, defaults={'require_mfa': True})
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])
        AuthenticationEvidence.objects.filter(user=self.member).update(mfa=True)
        self.assertEqual(len(self.ids(self.discover())), 2)

    def test_revoked_target_consent_can_be_reapproved_without_restoring_old_tokens(self):
        source_browser = self.browser
        self.browser = self.target.application
        try:
            target_token = self.exchange()['access_token']
        finally:
            self.browser = source_browser
        credentials = external_resource_credentials(self.target)

        def target_claims(token):
            return self.client.post('/oauth/introspect/', {'token': token}, **self.basic(credentials)).json()

        self.assertTrue(target_claims(target_token)['active'])
        consent = ApplicationConsent.objects.get(user=self.member, application=self.target.application)
        response = self.client.post(f'/accounts/consents/{consent.pk}/revoke/')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(target_claims(target_token)['active'])
        # Metadata eligibility is independent of OAuth approval, so the user
        # can still choose this organization and reach explicit re-consent.
        self.assertEqual(len(self.ids(self.discover())), 2)
        self.browser = self.target.application
        try:
            # The real PKCE helper requires GET to show the approval form,
            # submits explicit approval, and exchanges its new code.
            replacement = self.exchange()['access_token']
        finally:
            self.browser = source_browser
        self.assertTrue(target_claims(replacement)['active'])
        self.assertFalse(target_claims(target_token)['active'])
        consent.refresh_from_db()
        self.assertIsNone(consent.revoked_at)

    def test_revoked_source_consent_cannot_authorize_metadata_discovery(self):
        consent = ApplicationConsent.objects.get(user=self.member, application=self.browser)
        self.assertEqual(self.client.post(f'/accounts/consents/{consent.pk}/revoke/').status_code, 302)
        self.assertEqual(self.discover().status_code, 401)

    def test_superuser_source_still_needs_explicit_active_target_membership(self):
        type(self.member).objects.filter(pk=self.member.pk).update(is_superuser=True)
        self.target_member.delete()
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])

    def test_wrong_target_client_service_cannot_be_used_as_a_context(self):
        type(self.target.resource_application).objects.filter(pk=self.target.resource_application_id).update(
            service=self.installation.service)
        self.assertEqual(self.ids(self.discover()), [str(self.deployment.pk)])

    def test_get_missing_candidates_unbounded_and_malformed_payloads_are_rejected(self):
        self.assertEqual(self.client.get('/oauth/application-contexts/').status_code, 405)
        for payload in [None, {}, {'token': self.token}, {'token': self.token, 'deployment_ids': []},
            {'token': self.token, 'deployment_ids': [str(uuid4()) for _ in range(21)]},
            {'token': self.token, 'deployment_ids': [str(self.target.pk)] * 2},
            {'token': self.token, 'deployment_ids': ['not-a-uuid']},
            {'token': self.token, 'deployment_ids': [1]},
            {'token': self.token, 'deployment_ids': [str(self.target.pk)], 'organization_id': str(self.other.pk)},
            {'token': 'x' * 4097, 'deployment_ids': [str(self.target.pk)]}]:
            with self.subTest(payload=type(payload).__name__):
                response = self.client.post('/oauth/application-contexts/', json.dumps(payload),
                    content_type='application/json', **self.basic())
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.client.post('/oauth/application-contexts/', '{',
            content_type='application/json', **self.basic()).status_code, 400)
        self.assertEqual(self.client.post('/oauth/application-contexts/', ' ' * 16385,
            content_type='application/json', **self.basic()).status_code, 400)
        self.assertEqual(self.client.post('/oauth/application-contexts/', {'token': self.token},
            **self.basic()).status_code, 400)

    def test_discovery_performs_no_hosting_or_network_operations(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No external request')):
            self.assertEqual(len(self.ids(self.discover())), 2)
        self.assertEqual(MarketplacePackage.objects.filter(organization=self.other).count(), 0)
