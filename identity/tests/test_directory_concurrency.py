"""Concurrent directory changes must serialize on the organization's row."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from identity.access import Principal
from identity.directory import DirectoryVersionConflict, create_profile, replace_manager_assignments, update_profile
from identity.models import EmployeeProfile, Membership, Organization, ReportingAssignment, User


@skipUnless(connection.vendor == 'postgresql', 'Requires disposable PostgreSQL row locking')
class DirectoryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(slug='directory-race', name='Directory race')
        self.admin = User.objects.create_user('directory-race-admin@example.test')
        Membership.objects.create(organization=self.organization, user=self.admin, kind='administrator')
        self.principal = Principal(user=self.admin)

    def concurrent(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=20)
                operation()
                return 'saved'
            except (ValidationError, DirectoryVersionConflict) as error:
                return type(error).__name__
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(run, operation) for operation in operations]
            return [future.result(timeout=30) for future in futures]

    def test_opposing_reporting_updates_cannot_create_a_cycle(self):
        first = create_profile(self.principal, self.organization, {'work_email': 'first@example.test', 'is_manager': True})
        second = create_profile(self.principal, self.organization, {'work_email': 'second@example.test', 'is_manager': True})
        result = self.concurrent([
            lambda: replace_manager_assignments(self.principal, first,
                [{'manager_id': second.pk, 'is_primary': True}], first.revision),
            lambda: replace_manager_assignments(self.principal, second,
                [{'manager_id': first.pk, 'is_primary': True}], second.revision),
        ])
        self.assertCountEqual(result, ['saved', 'ValidationError'])
        self.assertEqual(ReportingAssignment.objects.filter(active=True).count(), 1)

    def test_simultaneous_edits_reject_the_stale_revision(self):
        person = create_profile(self.principal, self.organization, {'work_email': 'version@example.test'})
        result = self.concurrent([
            lambda: update_profile(self.principal, person, {'title': 'First title'}, 1),
            lambda: update_profile(self.principal, person, {'title': 'Second title'}, 1),
        ])
        self.assertCountEqual(result, ['saved', 'DirectoryVersionConflict'])
        saved = EmployeeProfile.objects.get(pk=person.pk)
        self.assertEqual(saved.revision, 2)
        self.assertIn(saved.title, ['First title', 'Second title'])

    def test_ordinary_revoke_serializes_with_directory_acceptance(self):
        from threading import Event
        from django.db import transaction
        from django.test import Client
        from django.urls import reverse
        from identity.directory import invite_profile, link_accepted_invitation
        from identity.models import Invitation
        person = create_profile(self.principal, self.organization, {'work_email':'accept-revoke@example.test'})
        invitation, _ = invite_profile(self.principal, person, person.revision)
        account = User.objects.create_user(person.work_email)
        membership = Membership.objects.create(organization=self.organization, user=account)
        # Establish the session before the concurrent organization lock: sign-in
        # itself writes an audit FK and would otherwise wait before reaching revoke.
        client = Client()
        client.force_login(self.admin)
        held, revoke_attempted = Event(), Event()

        def accept():
            close_old_connections()
            try:
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=self.organization.pk)
                    locked = Invitation.objects.select_for_update().get(pk=invitation.pk)
                    held.set()
                    self.assertTrue(revoke_attempted.wait(10),
                        'Revocation must acquire the organization before invitation/person writes')
                    link_accepted_invitation(locked, membership, principal=Principal(user=account))
                    locked.accepted = True
                    locked.save(update_fields=['accepted'])
                return 'accepted'
            finally:
                close_old_connections()

        def revoke():
            close_old_connections()
            try:
                self.assertTrue(held.wait(10))
                def observe(execute, sql, params, many, context):
                    if '"identity_organization"' in sql and 'FOR UPDATE' in sql:
                        revoke_attempted.set()
                    return execute(sql, params, many, context)
                with connection.execute_wrapper(observe):
                    return client.post(reverse('revoke_invitation',args=[self.organization.slug,invitation.pk])).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            acceptance, revocation = pool.submit(accept), pool.submit(revoke)
            self.assertEqual(acceptance.result(timeout=25),'accepted')
            self.assertEqual(revocation.result(timeout=25),302)
        person.refresh_from_db()
        self.assertEqual(person.membership_id,membership.pk)
        self.assertIsNone(person.invitation_id)
        self.assertFalse(Invitation.objects.filter(pk=invitation.pk).exists())
