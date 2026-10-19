"""Actual loopback HTTP acceptance with synthetic people and no providers."""
import re
import secrets
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.test import LiveServerTestCase, TestCase
from django.urls import reverse

from identity.access import Principal
from identity.directory import create_profile, invite_profile
from identity.models import EmployeeProfile, Membership, Organization, PermissionTag, ReportingAssignment, User


class DirectoryInvitationPolicyTests(TestCase):
    def test_manager_page_lists_only_authorized_direct_reports(self):
        organization = Organization.objects.create(slug='directory-reports', name='Directory reports')
        admin = User.objects.create_user('reports-admin@example.test')
        Membership.objects.create(organization=organization, user=admin, kind='administrator')
        manager = create_profile(Principal(user=admin), organization,
            {'work_email': 'reports-manager@example.test', 'is_manager': True})
        employee = create_profile(Principal(user=admin), organization, {'work_email': 'direct-report@example.test'})
        ReportingAssignment.objects.create(organization=organization, manager=manager, employee=employee)
        self.client.force_login(admin)
        manager_url = reverse('directory_person', args=[organization.slug, manager.pk])
        employee_url = reverse('directory_person', args=[organization.slug, employee.pk])
        self.assertContains(self.client.get(manager_url), employee_url)
        tag = PermissionTag.objects.create(organization=organization, slug='hidden-report', name='Hidden report')
        employee.permission_tags.add(tag)
        self.assertNotContains(self.client.get(manager_url), employee_url)

    def test_central_invitation_list_inherits_current_employee_visibility(self):
        organization = Organization.objects.create(slug='directory-policy', name='Directory policy')
        owner = User.objects.create_user('directory-owner@example.test')
        other = User.objects.create_user('directory-other-admin@example.test')
        member = Membership.objects.create(organization=organization, user=owner, kind='administrator')
        Membership.objects.create(organization=organization, user=other, kind='administrator')
        tag = PermissionTag.objects.create(organization=organization, slug='personnel', name='Personnel')
        member.access_tags.add(tag)
        person = create_profile(Principal(user=owner), organization, {'work_email': 'private-person@example.test'})
        person.permission_tags.add(tag)
        invitation, _ = invite_profile(Principal(user=owner), person, person.revision)
        self.client.force_login(other)
        response = self.client.get(reverse('organization', args=[organization.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, person.work_email)
        self.client.force_login(owner)
        response = self.client.get(reverse('organization', args=[organization.slug]))
        self.assertContains(response, invitation.email)
        person.permission_tags.clear()
        self.client.force_login(other)
        self.assertContains(self.client.get(reverse('organization', args=[organization.slug])), invitation.email)


class DirectoryHTTPAcceptanceTests(LiveServerTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(slug='directory-http', name='Directory HTTP')
        self.admin = User.objects.create_user('directory-http-admin@example.test')
        Membership.objects.create(organization=self.organization, user=self.admin, kind='administrator')
        self.client.force_login(self.admin)
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.cookies.set(settings.SESSION_COOKIE_NAME, self.client.cookies[settings.SESSION_COOKIE_NAME].value)
        self.addCleanup(self.http.close)

    def get(self, path, session=None):
        return (session or self.http).get(self.live_server_url + path, timeout=10, allow_redirects=False)

    def post(self, path, values, session=None):
        session = session or self.http
        return session.post(self.live_server_url + path, data={**values,
            'csrfmiddlewaretoken': session.cookies.get(settings.CSRF_COOKIE_NAME)}, timeout=10, allow_redirects=False)

    def test_people_manager_cycle_and_bound_account_setup_over_http(self):
        directory_url = reverse('directory_people', args=[self.organization.slug])
        self.assertEqual(self.get(directory_url).status_code, 200)
        people = []
        for name in ['manager', 'employee']:
            response = self.post(directory_url, {'work_email': f'{name}-http@example.test',
                'first_name': name.title(), 'status': 'active', 'is_manager': 'on'})
            self.assertEqual(response.status_code, 302)
            people.append(EmployeeProfile.objects.get(work_email=f'{name}-http@example.test'))
        manager, employee = people
        employee_url = reverse('directory_person', args=[self.organization.slug, employee.pk])
        response = self.post(employee_url, {'action': 'managers', 'expected_revision': employee.revision,
            'managers': [str(manager.pk)], 'primary_manager': str(manager.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReportingAssignment.objects.get(employee=employee, manager=manager).is_primary)
        manager_url = reverse('directory_person', args=[self.organization.slug, manager.pk])
        response = self.post(manager_url, {'action': 'managers', 'expected_revision': manager.revision,
            'managers': [str(employee.pk)], 'primary_manager': str(employee.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertIn('reporting cycle', response.text)
        self.assertFalse(ReportingAssignment.objects.filter(employee=manager, active=True).exists())
        response = self.post(employee_url, {'action': 'invite',
            'expected_revision': EmployeeProfile.objects.get(pk=employee.pk).revision})
        self.assertEqual(response.status_code, 200)
        match = re.search(r'/invitations/[^\s"<>]+/', response.text)
        self.assertIsNotNone(match)
        invitation_path = urlparse(match.group(0)).path
        with requests.Session() as signup:
            signup.trust_env = False
            self.assertEqual(self.get(invitation_path, signup).status_code, 200)
            password = secrets.token_urlsafe(32)
            response = self.post(invitation_path, {'first_name': 'Synthetic', 'last_name': 'Employee',
                'password1': password, 'password2': password}, signup)
            self.assertEqual(response.status_code, 302)
        employee.refresh_from_db()
        self.assertIsNotNone(employee.membership_id)
        self.assertEqual(employee.membership.user.email, employee.work_email)
        self.assertEqual(employee.membership.kind, Membership.Kind.MEMBER)
        self.assertFalse(employee.membership.roles.exists())
        self.assertFalse(employee.membership.access_tags.exists())
