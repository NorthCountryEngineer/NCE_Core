"""Synthetic Core identities and actual PKCE exchanges; no external services."""
import json
import secrets
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from identity.models import ApplicationAccountAccess, ApplicationAccountInvitation, ApplicationConsent, Invitation, Membership, Role, Service, ServicePermission, User
from platform_services.tests.test_external_apps import ExternalApplicationTests
from platform_services.external_apps import register_external_deployment
from platform_services.validation import validate_manifest

PASSWORD = secrets.token_urlsafe(32)


class AppAccountTests(TestCase):
    setUpTestData = classmethod(ExternalApplicationTests.setUpTestData.__func__)
    basic = ExternalApplicationTests.basic
    exchange = ExternalApplicationTests.exchange
    introspect = ExternalApplicationTests.introspect

    def token(self, user=None):
        original = self.member
        self.member = user or self.admin
        ApplicationConsent.objects.filter(user=self.member, application=self.browser).delete()
        try: return self.exchange()['access_token']
        finally: self.member = original

    def operation(self, operation, payload=None, token=None, **headers):
        return self.client.post('/api/v1/application-accounts/', {'operation': operation,
            'payload': json.dumps(payload or {}), 'token': token or self.token()}, **(headers or self.basic()))

    def invite(self, email='new-reader@example.test', role='editor', token=None):
        return self.operation('invite', {'email': email, 'name': 'Synthetic reader', 'roleIds': [role]}, token)

    def accept(self, response, email='new-reader@example.test', existing=None):
        link = response.json()['invitationUrl']
        self.client.logout()
        if existing: self.client.force_login(existing)
        result = self.client.post(urlparse(link).path, {'first_name': 'Synthetic', 'last_name': 'Reader',
            'password1': PASSWORD, 'password2': PASSWORD})
        self.assertEqual(result.status_code, 302, result.content)
        self.assertEqual(result.url, 'https://axolotl.example.test/studio?account=created')
        return User.objects.get(email=email)

    def test_invitations_create_one_core_account_and_only_explicit_app_grants(self):
        response = self.invite()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(User.objects.filter(email='new-reader@example.test').exists())
        self.assertEqual(response.json()['invitation']['status'], 'pending')
        raw = urlparse(response.json()['invitationUrl']).path.strip('/').split('/')[-1]
        record = ApplicationAccountInvitation.objects.get()
        self.assertNotEqual(record.invitation.token_hash, raw)
        user = self.accept(response)
        member = Membership.objects.get(user=user, organization=self.organization)
        self.assertEqual(member.kind, 'member')
        self.assertEqual(member.roles.count(), 0)
        self.assertEqual(member.access_tags.count(), 0)
        self.assertFalse(user.is_staff or user.is_superuser or user.is_developer)
        access = ApplicationAccountAccess.objects.get(membership=member, deployment=self.deployment)
        self.assertEqual(access.role_ids, ['editor'])
        claims = self.introspect(self.token(user)).json()
        self.assertEqual(claims['sub'], str(user.pk))
        self.assertEqual(claims['permissions'], ['posts-write'])
        self.assertEqual(User.objects.filter(email=user.email).count(), 1)

    def test_existing_identity_requires_existing_login_and_preserves_global_membership(self):
        existing = User.objects.create_user('existing-global@example.test', PASSWORD)
        Membership.objects.create(organization=self.organization, user=existing)
        old_password = existing.password
        response = self.invite(existing.email, 'publisher')
        link = response.json()['invitationUrl']
        self.client.logout()
        self.assertEqual(self.client.get(urlparse(link).path).status_code, 302)
        user = self.accept(response, existing.email, existing)
        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(user.password, old_password)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.kind, 'member')
        self.assertEqual(Membership.objects.get(user=user, organization=self.organization).roles.count(), 0)
        self.assertEqual(self.introspect(self.token(user)).json()['permissions'], ['posts-publish', 'posts-write'])

    def test_editor_and_publisher_cannot_manage_accounts(self):
        self.assertEqual(self.operation('list', token=self.token(self.member)).status_code, 403)
        self.role.permissions.add(self.publish)
        self.assertEqual(self.invite(token=self.token(self.member)).status_code, 403)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_app_administrator_can_create_accounts_without_becoming_core_admin(self):
        response = self.invite(role='administrator')
        user = self.accept(response)
        token = self.token(user)
        claims = self.introspect(token).json()
        self.assertIn('accounts-manage', claims['permissions'])
        self.assertNotIn('admin', claims['permissions'])
        self.assertEqual(self.invite('second@example.test', token=token).status_code, 200)
        self.assertEqual(self.client.get('/organizations/' + self.organization.slug + '/invite/').status_code, 403)
        access = ApplicationAccountAccess.objects.get(membership__user=user)
        access.active = False; access.save(update_fields=['active'])
        self.assertEqual(self.operation('list', token=token).status_code, 401)

    def test_application_access_does_not_grant_another_app_environment_or_permissions(self):
        response = self.invite()
        user = self.accept(response)
        other_service = Service.objects.create(organization=self.organization, slug='other-app', name='Other app')
        permission = ServicePermission.objects.create(service=other_service, codename='posts-write', name='Other edit')
        role = Role.objects.create(organization=self.organization, slug='other-editor', name='Other editor')
        role.permissions.add(permission)
        member = Membership.objects.get(user=user, organization=self.organization)
        member.roles.add(role)
        from identity.access import Principal, decision
        principal = Principal(user=user, application=self.browser)
        from identity.policy import authorization_for
        self.assertEqual(authorization_for(member, other_service, principal)['permissions'], [])
        other_service.permission_tags.add(self.environment.boundary_tag)
        self.assertFalse(decision(principal, other_service)[0])
        self.assertEqual(member.access_tags.count(), 0)

    def test_cross_app_and_cross_environment_resource_clients_are_rejected(self):
        token = self.token()
        from platform_services.environments import create_environment
        other_environment = create_environment(self.organization, name='staging', namespace='account-staging',
            application_origin='https://staging.example.test')
        other_deployment = register_external_deployment(self.installation, other_environment, 'https://staging.example.test', self.admin)
        from platform_services.external_apps import external_resource_credentials
        self.assertEqual(self.operation('list', token=token,
            **self.basic(external_resource_credentials(other_deployment))).status_code, 401)

    def test_pending_invitation_is_idempotently_revocable_without_email_or_existing_account_changes(self):
        token = self.token()
        response = self.invite(token=token)
        self.assertEqual(self.invite(token=token).status_code, 409)
        ident = response.json()['invitation']['id']
        self.assertEqual(self.operation('revoke', {'id': ident}, token).status_code, 200)
        self.assertEqual(self.operation('revoke', {'id': ident}, token).status_code, 200)
        self.assertEqual(self.client.get(urlparse(response.json()['invitationUrl']).path).status_code, 410)
        listing = self.operation('list', token=token).json()
        self.assertEqual(listing['invitations'][0]['status'], 'revoked')
        self.assertNotIn('invitationUrl', json.dumps(listing))
        self.assertFalse(User.objects.filter(email='new-reader@example.test').exists())

    def test_accounts_list_is_app_scoped_and_raw_tokens_are_never_relisted(self):
        outsider = User.objects.create_user('other-app-person@example.test', PASSWORD)
        Membership.objects.create(organization=self.organization, user=outsider)
        response = self.invite()
        listing = self.operation('list').json()
        self.assertNotIn(outsider.email, json.dumps(listing))
        self.assertNotIn(response.json()['invitationUrl'], json.dumps(listing))
        self.assertEqual([role['id'] for role in listing['roles']], ['editor', 'publisher', 'administrator'])

    def test_foreign_invitation_cannot_be_revoked_or_redeemed_as_another_app(self):
        token = self.token()
        response = self.invite(token=token)
        self.assertEqual(self.operation('revoke', {'id': '999999'}, token).status_code, 404)
        record = ApplicationAccountInvitation.objects.get()
        record.deployment.desired_state = 'stopped'; record.deployment.save(update_fields=['desired_state'])
        self.client.logout()
        result = self.client.post(urlparse(response.json()['invitationUrl']).path,
            {'first_name': 'Synthetic', 'password1': PASSWORD, 'password2': PASSWORD})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(User.objects.filter(email='new-reader@example.test').exists())
        record.invitation.refresh_from_db(); self.assertFalse(record.invitation.accepted)

    def test_expired_or_consumed_invitation_never_recreates_grants(self):
        response = self.invite()
        user = self.accept(response)
        self.assertEqual(self.client.get(urlparse(response.json()['invitationUrl']).path).status_code, 410)
        self.assertEqual(ApplicationAccountAccess.objects.filter(membership__user=user).count(), 1)

    def test_published_roles_cannot_name_org_admin_or_other_services_permissions(self):
        manifest = json.loads(json.dumps(self.package.manifest))
        manifest['account_roles'][0]['permissions'] = ['admin']
        with self.assertRaises(ValidationError): validate_manifest(manifest)
        manifest['account_roles'][0]['permissions'] = ['other-application-edit']
        with self.assertRaises(ValidationError): validate_manifest(manifest)

    def test_request_cannot_choose_organization_service_scope_or_password(self):
        token = self.token()
        for extra in [{'organization_id': str(self.other.pk)}, {'service_id': 'forged'}, {'password': 'forged'}, {'kind': 'administrator'}]:
            payload = {'email': 'new@example.test', 'name': '', 'roleIds': ['editor'], **extra}
            self.assertEqual(self.operation('invite', payload, token).status_code, 400)
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertEqual(self.operation('invite', {'email': 'new@example.test', 'name': '', 'roleIds': ['admin']}, token).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/application-accounts/', {'operation': 'list'}).status_code, 401)

    def test_wrong_existing_identity_cannot_accept_invitation_or_obtain_app_grant(self):
        existing = User.objects.create_user('existing-wrong-binding@example.test', PASSWORD)
        response = self.invite(existing.email)
        wrong = User.objects.create_user('wrong-person@example.test', PASSWORD)
        self.client.force_login(wrong)
        result = self.client.post(urlparse(response.json()['invitationUrl']).path, {'first_name': 'Forged'})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(ApplicationAccountAccess.objects.exists())
        self.assertFalse(Invitation.objects.get().accepted)

    def test_account_manager_cannot_delegate_permissions_they_do_not_hold(self):
        manage = self.installation.service.permissions.get(codename='accounts-manage')
        self.role.permissions.add(manage)
        token = self.token(self.member)
        self.assertEqual(self.invite('other@example.test', 'administrator', token).status_code, 403)
        self.assertEqual(self.invite('other@example.test', 'publisher', token).status_code, 403)
        self.assertEqual(self.invite('other@example.test', 'editor', token).status_code, 200)

    def test_identity_package_upgrade_preserves_registration_and_is_retry_safe(self):
        from django.core.management import call_command
        from io import StringIO
        from pathlib import Path
        from django.conf import settings
        self.installation.version = '1.0.0'
        old_manifest = json.loads(json.dumps(self.installation.manifest))
        old_manifest.pop('account_roles')
        old_manifest['permissions'] = [row for row in old_manifest['permissions'] if row['codename'] not in {'accounts-manage', 'agents-manage'}]
        self.installation.manifest = old_manifest
        self.installation.save(update_fields=['version', 'manifest'])
        options = dict(organization=self.organization.slug, service_id=str(self.installation.service_id),
            package=str(Path(settings.BASE_DIR) / 'examples/axolotl-news-package.json'), expected_version='1.0.0', actor_email=self.admin.email)
        output = StringIO()
        call_command('upgrade_external_application', **options, stdout=output)
        self.assertTrue(json.loads(output.getvalue())['changed'])
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.application_id, self.browser.pk)
        self.assertEqual(self.deployment.resource_application_id, self.resource.pk)
        output = StringIO()
        call_command('upgrade_external_application', **options, stdout=output)
        self.assertFalse(json.loads(output.getvalue())['changed'])
        self.assertEqual(ApplicationAccountAccess.objects.count(), 0)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_existing_application_account_and_legacy_author_access_cannot_be_reinvited(self):
        token = self.token()
        self.assertEqual(self.invite(self.member.email, token=token).status_code, 409)
        response = self.invite(token=token)
        user = self.accept(response)
        token = self.token()
        self.assertEqual(self.invite(user.email, token=token).status_code, 409)
        access = ApplicationAccountAccess.objects.get(membership__user=user)
        access.active = False; access.save(update_fields=['active'])
        self.assertEqual(self.invite(user.email, token=token).status_code, 409)

    def test_boundary_grants_require_every_chain_object_to_match_exact_application(self):
        response = self.invite()
        user = self.accept(response)
        member = Membership.objects.get(user=user, organization=self.organization)
        from identity.access import Principal
        from identity.app_accounts import application_boundary_grants
        from platform_services.models import MarketplacePackage
        principal = Principal(user=user, application=self.browser)
        other_package = MarketplacePackage.objects.exclude(pk=self.package.pk).first()
        for resource in [self.other, self.other_environment, other_package]:
            self.assertEqual(application_boundary_grants(member, principal, self.deployment, [resource], []), set())
        principal.application = None
        self.assertEqual(application_boundary_grants(member, principal, self.deployment, [self.service] if hasattr(self, 'service') else [], []), set())

    def test_permission_tagged_account_and_invitation_are_not_exposed_or_revocable(self):
        token = self.token()
        response = self.invite(token=token)
        record = ApplicationAccountInvitation.objects.get()
        from identity.models import PermissionTag
        tag = PermissionTag.objects.create(organization=self.organization, slug='private-account', name='Private account')
        record.invitation.permission_tags.add(tag)
        self.membership.permission_tags.add(tag)
        listing = self.operation('list', token=token).json()
        self.assertNotIn(self.member.email, json.dumps(listing))
        self.assertEqual(listing['invitations'], [])
        self.assertEqual(self.operation('revoke', {'id': str(record.invitation_id)}, token).status_code, 403)
