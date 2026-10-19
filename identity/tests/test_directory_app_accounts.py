"""The shared directory and delegated application accounts never imply each other."""
import secrets

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from identity.access import Principal
from identity.directory import create_profile, invite_profile, visible_profiles
from identity.models import ApplicationAccountAccess, EmployeeProfile, Membership, User
from identity.tests import test_app_accounts as account_tests
from platform_services.tests import test_external_apps as external_tests


class DirectoryApplicationBoundaryTests(TestCase):
    setUpTestData = classmethod(external_tests.ExternalApplicationTests.setUpTestData.__func__)
    basic = external_tests.ExternalApplicationTests.basic
    exchange = external_tests.ExternalApplicationTests.exchange
    introspect = external_tests.ExternalApplicationTests.introspect
    token = account_tests.AppAccountTests.token
    operation = account_tests.AppAccountTests.operation
    invite = account_tests.AppAccountTests.invite
    accept = account_tests.AppAccountTests.accept

    def test_application_invitation_does_not_link_same_email_directory_person(self):
        person = create_profile(Principal(user=self.admin), self.organization,
            {'work_email': 'new-reader@example.test'})
        user = self.accept(self.invite())
        person.refresh_from_db()
        self.assertIsNone(person.membership_id)
        self.assertIsNone(person.invitation_id)
        self.assertTrue(ApplicationAccountAccess.objects.filter(membership__user=user).exists())
        self.assertEqual(EmployeeProfile.objects.count(), 1)

    def test_directory_invitation_does_not_assign_application_roles(self):
        person = create_profile(Principal(user=self.admin), self.organization,
            {'work_email': 'directory-only@example.test'})
        _, raw = invite_profile(Principal(user=self.admin), person, person.revision)
        self.client.logout()
        password = secrets.token_urlsafe(32)
        response = self.client.post('/invitations/' + raw + '/',
            {'first_name': 'Directory', 'password1': password, 'password2': password})
        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        self.assertIsNotNone(person.membership_id)
        self.assertEqual(person.membership.kind, Membership.Kind.MEMBER)
        self.assertFalse(person.membership.roles.exists())
        self.assertFalse(person.membership.access_tags.exists())
        self.assertFalse(ApplicationAccountAccess.objects.exists())

    def test_application_administrator_cannot_read_core_personnel_directory(self):
        user = self.accept(self.invite(role='administrator'))
        self.assertEqual(Membership.objects.get(user=user).kind, Membership.Kind.MEMBER)
        with self.assertRaises(PermissionDenied):
            visible_profiles(Principal(user=user, application=self.browser), self.organization)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/organizations/' + self.organization.slug + '/people/').status_code, 403)
