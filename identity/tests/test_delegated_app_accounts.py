"""Delegated app administration is usable without granting organization access."""
from datetime import timedelta
import secrets

from django.test import TestCase
from django.utils import timezone

from identity.access import Principal
from identity.directory import create_profile, invite_profile
from identity.models import (ApplicationAccountAccess, ApplicationAccountInvitation,
    EmployeeProfile, Invitation, Membership, PermissionTag)
from identity.tests import test_app_accounts as account_tests
from platform_services.environments import create_environment
from platform_services.external_apps import register_external_deployment
from platform_services.tests import test_external_apps as external_tests


class DelegatedAccountAdministrationTests(TestCase):
    setUpTestData = classmethod(external_tests.ExternalApplicationTests.setUpTestData.__func__)
    basic = external_tests.ExternalApplicationTests.basic
    exchange = external_tests.ExternalApplicationTests.exchange
    introspect = external_tests.ExternalApplicationTests.introspect
    token = account_tests.AppAccountTests.token
    operation = account_tests.AppAccountTests.operation
    invite = account_tests.AppAccountTests.invite
    accept = account_tests.AppAccountTests.accept

    def setUp(self):
        self.delegated = self.accept(self.invite('delegated-admin@example.test', role='administrator'),
            'delegated-admin@example.test')
        self.delegated_membership = Membership.objects.get(user=self.delegated, organization=self.organization)
        self.assertEqual(self.delegated_membership.kind, 'member')
        self.assertFalse(self.delegated_membership.access_tags.exists())
        self.delegated_token = self.token(self.delegated)

    def test_delegated_administrator_can_list_and_revoke_its_app_invitations(self):
        created = self.invite('delegated-target@example.test', token=self.delegated_token)
        self.assertEqual(created.status_code, 200)
        listing = self.operation('list', token=self.delegated_token)
        self.assertEqual(listing.status_code, 200)
        self.assertIn(self.delegated.email, [row['email'] for row in listing.json()['accounts']])
        ident = created.json()['invitation']['id']
        self.assertIn(ident, [row['id'] for row in listing.json()['invitations']])
        self.assertEqual(self.operation('revoke', {'id': ident}, self.delegated_token).status_code, 200)
        self.assertEqual(self.operation('revoke', {'id': ident}, self.delegated_token).status_code, 200)
        self.assertFalse(self.delegated_membership.access_tags.exists())

    def test_foreign_app_and_directory_only_invitations_are_never_in_scope(self):
        environment = create_environment(self.organization, name='staging', namespace='delegated-staging',
            application_origin='https://staging.example.test')
        other = register_external_deployment(self.installation, environment,
            'https://staging.example.test', self.admin)
        raw = secrets.token_urlsafe(32)
        foreign = Invitation.objects.create(organization=self.organization, email='foreign-app@example.test',
            kind='member', token_hash=Invitation.digest(raw), expires=timezone.now()+timedelta(hours=1))
        ApplicationAccountInvitation.objects.create(invitation=foreign, deployment=other, role_ids=['editor'])
        person = create_profile(Principal(user=self.admin), self.organization,
            {'work_email':'directory-only@example.test'})
        directory_invitation, _ = invite_profile(Principal(user=self.admin), person, person.revision)
        listing = self.operation('list', token=self.delegated_token).json()
        ids = [row['id'] for row in listing['invitations']]
        for invitation in [foreign, directory_invitation]:
            self.assertNotIn(str(invitation.pk), ids)
            self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 404)
        self.assertFalse(ApplicationAccountAccess.objects.filter(membership=self.delegated_membership, deployment=other).exists())

    def test_explicit_membership_and_invitation_tags_remain_required(self):
        created = self.invite('tagged-target@example.test', token=self.delegated_token)
        target = self.accept(created, 'tagged-target@example.test')
        self.delegated_token = self.token(self.delegated)
        membership = Membership.objects.get(user=target, organization=self.organization)
        invitation = ApplicationAccountInvitation.objects.get(invitation_id=created.json()['invitation']['id']).invitation
        tag = PermissionTag.objects.create(organization=self.organization, slug='private-account-metadata', name='Private metadata')
        membership.permission_tags.add(tag)
        invitation.permission_tags.add(tag)
        response = self.operation('list', token=self.delegated_token)
        self.assertEqual(response.status_code, 200, response.content)
        listing = response.json()
        self.assertNotIn(target.email, [row['email'] for row in listing['accounts']])
        self.assertNotIn(str(invitation.pk), [row['id'] for row in listing['invitations']])
        self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 403)
        self.delegated_membership.access_tags.add(tag)
        authorized = self.operation('list', token=self.delegated_token).json()
        self.assertIn(target.email, [row['email'] for row in authorized['accounts']])
        self.assertIn(str(invitation.pk), [row['id'] for row in authorized['invitations']])
        self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 409)

    def test_directory_inheritance_and_mfa_policy_are_not_bypassed(self):
        created = self.invite('private-person@example.test', token=self.delegated_token)
        invitation = ApplicationAccountInvitation.objects.get(invitation_id=created.json()['invitation']['id']).invitation
        person = EmployeeProfile.objects.create(organization=self.organization,
            work_email=invitation.email, invitation=invitation)
        tag = PermissionTag.objects.create(organization=self.organization,
            slug='private-personnel', name='Private personnel')
        person.permission_tags.add(tag)
        listing = self.operation('list', token=self.delegated_token).json()
        self.assertNotIn(str(invitation.pk), [row['id'] for row in listing['invitations']])
        self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 403)
        self.delegated_membership.access_tags.add(tag)
        tag.requires_mfa = True
        tag.save(update_fields=['requires_mfa'])
        self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 403)
        tag.requires_mfa = False
        tag.allowed_purposes = ['personnel']
        tag.save(update_fields=['requires_mfa','allowed_purposes'])
        self.assertEqual(self.operation('revoke', {'id':str(invitation.pk)}, self.delegated_token).status_code, 403)
