"""Synthetic, local directory tests; no provider credentials or outbound calls."""
from datetime import timedelta
import secrets

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from identity import directory
from identity.access import Principal
from identity.models import (AuditEvent, EmployeeProfile, ExternalPersonLink, Invitation, Membership,
    Organization, PermissionTag, ReportingAssignment, Role, Service, ServicePermission, User)
from identity.policy import authorization_for


class EmployeeDirectoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(slug="directory", name="Directory")
        cls.other_org = Organization.objects.create(slug="other-directory", name="Other directory")
        cls.admin = User.objects.create_user("directory-admin@example.test", secrets.token_urlsafe(32))
        cls.admin_member = Membership.objects.create(organization=cls.org, user=cls.admin, kind="administrator")
        cls.user = User.objects.create_user("employee@example.test", secrets.token_urlsafe(32))
        cls.member = Membership.objects.create(organization=cls.org, user=cls.user)
        cls.other_admin = User.objects.create_user("other-directory-admin@example.test", secrets.token_urlsafe(32))
        Membership.objects.create(organization=cls.other_org, user=cls.other_admin, kind="administrator")

    def setUp(self):
        self.principal = Principal(user=self.admin)

    def person(self, email="person@example.test", **values):
        return directory.create_profile(self.principal, self.org, {"work_email": email, **values})

    def assign(self, employee, manager, primary=True):
        return directory.replace_manager_assignments(self.principal, employee,
            [{"manager_id": str(manager.pk), "is_primary": primary}], employee.revision)

    def test_pending_profile_is_not_an_account_or_permission_grant(self):
        before = User.objects.count()
        person = self.person(" Pending@Example.test ", first_name="Synthetic", is_manager=True)
        self.assertEqual(person.work_email, "pending@example.test")
        self.assertEqual(person.display_name, "Synthetic")
        self.assertIsNone(person.membership_id)
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(person.revision, 1)

    def test_case_insensitive_email_unique_within_organization(self):
        self.person("unique@example.test")
        with self.assertRaises(ValidationError):
            self.person("UNIQUE@example.test")
        other = directory.create_profile(Principal(user=self.other_admin), self.other_org, {"work_email": "unique@example.test"})
        self.assertEqual(other.organization, self.other_org)

    def test_current_admin_required_and_stale_user_cannot_retain_access(self):
        for principal in [Principal(user=self.user), Principal(user=self.other_admin), Principal()]:
            with self.subTest(principal=principal), self.assertRaises(PermissionDenied):
                directory.create_profile(principal, self.org, {"work_email": "denied@example.test"})
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.person()

    def test_revoked_administrator_membership_denies_next_call(self):
        person = self.person()
        Membership.objects.filter(pk=self.admin_member.pk).update(kind="member")
        with self.assertRaises(PermissionDenied):
            directory.update_profile(self.principal, person, {"title": "Changed"}, person.revision)

    def test_profile_update_requires_revision_and_rejects_auth_fields(self):
        person = self.person()
        changed = directory.update_profile(self.principal, person, {"title": "Engineer"}, 1)
        self.assertEqual(changed.revision, 2)
        with self.assertRaises(directory.DirectoryVersionConflict):
            directory.update_profile(self.principal, person, {"title": "Stale"}, 1)
        for values in [{"kind": "administrator"}, {"membership_id": self.member.pk}, {"is_manager": "yes"}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                directory.update_profile(self.principal, changed, values, 2)

    def test_cross_org_membership_rejected_without_reassigning_account(self):
        person = self.person()
        foreign = Membership.objects.get(organization=self.other_org, user=self.other_admin)
        from django.http import Http404
        with self.assertRaises(Http404):
            directory.attach_membership(self.principal, person, foreign, person.revision)
        person.refresh_from_db()
        self.assertIsNone(person.membership_id)

    def test_attach_and_employment_status_do_not_change_login_or_service_roles(self):
        person = self.person(is_manager=True)
        service = Service.objects.create(organization=self.org, slug="directory-app", name="App")
        permission = ServicePermission.objects.create(service=service, codename="read", name="Read")
        role = Role.objects.create(organization=self.org, slug="reader", name="Reader")
        role.permissions.add(permission)
        self.member.roles.add(role)
        before = authorization_for(self.member, service)
        person = directory.attach_membership(self.principal, person, self.member, person.revision)
        person = directory.update_profile(self.principal, person, {"status": "inactive"}, person.revision)
        self.member.refresh_from_db(); self.user.refresh_from_db()
        self.assertTrue(self.member.active and self.user.is_active)
        self.assertEqual(authorization_for(self.member, service), before)
        self.assertEqual(self.member.kind, "member")

    def test_inactive_membership_cannot_be_attached(self):
        person = self.person()
        self.member.active = False; self.member.save(update_fields=["active"])
        with self.assertRaises(ValidationError):
            directory.attach_membership(self.principal, person, self.member, person.revision)

    def test_manager_assignment_can_be_multiple_with_one_primary(self):
        employee = self.person()
        first = self.person("manager1@example.test", is_manager=True)
        second = self.person("manager2@example.test", is_manager=True)
        employee = directory.replace_manager_assignments(self.principal, employee, [
            {"manager_id": str(first.pk), "is_primary": True}, {"manager_id": str(second.pk), "is_primary": False}], employee.revision)
        edges = directory.visible_assignments(self.principal, self.org, employee)
        self.assertEqual(len(edges), 2)
        self.assertEqual(sum(edge.is_primary for edge in edges), 1)
        self.assertEqual(employee.revision, 2)

    def test_self_cross_org_and_non_manager_assignments_rejected(self):
        employee = self.person(is_manager=True)
        ordinary = self.person("ordinary@example.test")
        foreign = directory.create_profile(Principal(user=self.other_admin), self.other_org, {"work_email": "foreign@example.test", "is_manager": True})
        from django.http import Http404
        for manager, exception in [(employee, ValidationError), (ordinary, ValidationError), (foreign, Http404)]:
            with self.subTest(manager=manager.pk), self.assertRaises(exception):
                self.assign(employee, manager)

    def test_reporting_cycles_rejected_and_existing_graph_preserved(self):
        first = self.person("first@example.test", is_manager=True)
        second = self.person("second@example.test", is_manager=True)
        third = self.person("third@example.test", is_manager=True)
        second = self.assign(second, first)
        third = self.assign(third, second)
        with self.assertRaises(ValidationError):
            self.assign(first, third)
        self.assertEqual(ReportingAssignment.objects.filter(active=True).count(), 2)

    def test_replacement_retains_inactive_history_and_reuses_edge_identity(self):
        employee = self.person()
        manager = self.person("manager@example.test", is_manager=True)
        employee = self.assign(employee, manager)
        edge_id = ReportingAssignment.objects.get(employee=employee).pk
        employee = directory.replace_manager_assignments(self.principal, employee, [], employee.revision)
        edge = ReportingAssignment.objects.get(pk=edge_id)
        self.assertFalse(edge.active)
        employee = self.assign(employee, manager)
        self.assertEqual(ReportingAssignment.objects.get(employee=employee, active=True).pk, edge_id)
        self.assertTrue(AuditEvent.objects.filter(action="directory.reporting_disabled", target=str(edge_id)).exists())

    def test_leave_disables_relationship_but_keeps_account_and_history(self):
        employee = self.person()
        manager = self.person("manager@example.test", is_manager=True)
        employee = self.assign(employee, manager)
        manager = directory.update_profile(self.principal, manager, {"status": "leave"}, manager.revision)
        self.assertFalse(ReportingAssignment.objects.get(employee=employee).active)
        self.assertEqual(directory.visible_assignments(self.principal, self.org), [])
        employee.refresh_from_db()
        self.assertEqual(employee.revision, 3)

    def test_disabled_linked_account_makes_relationship_ineffective(self):
        employee = directory.attach_membership(self.principal, self.person(), self.member, 1)
        manager = self.person("manager@example.test", is_manager=True)
        employee = self.assign(employee, manager)
        self.assertEqual(len(directory.visible_assignments(self.principal, self.org)), 1)
        Membership.objects.filter(pk=self.member.pk).update(active=False)
        self.assertEqual(directory.visible_assignments(self.principal, self.org), [])
        self.assertTrue(ReportingAssignment.objects.get(employee=employee).active)

    def test_protected_hidden_assignment_cannot_be_silently_removed(self):
        employee = self.person()
        manager = self.person("manager@example.test", is_manager=True)
        employee = self.assign(employee, manager)
        tag = PermissionTag.objects.create(organization=self.org, slug="directory-private", name="Private")
        edge = ReportingAssignment.objects.get(employee=employee)
        edge.permission_tags.add(tag)
        self.assertEqual(directory.visible_assignments(self.principal, self.org), [])
        with self.assertRaises(PermissionDenied):
            directory.replace_manager_assignments(self.principal, employee, [], employee.revision)
        edge.refresh_from_db(); self.assertTrue(edge.active)

    def test_profile_and_linked_membership_tags_protect_reads_and_mutations(self):
        person = self.person()
        tag = PermissionTag.objects.create(organization=self.org, slug="directory-private", name="Private")
        person.permission_tags.add(tag)
        self.assertEqual(directory.visible_profiles(self.principal, self.org), [])
        with self.assertRaises(PermissionDenied):
            directory.update_profile(self.principal, person, {"title": "Changed"}, person.revision)
        self.admin_member.access_tags.add(tag)
        self.assertEqual(directory.profile_detail(self.principal, self.org, person.pk).pk, person.pk)

    def test_read_filters_apply_before_limit_and_do_not_expose_other_org(self):
        self.person("aaa@example.test", first_name="First")
        wanted = self.person("zzz@example.test", first_name="Last", is_manager=True)
        self.assertEqual(directory.visible_profiles(self.principal, self.org, limit=1, is_manager=True), [wanted])
        self.assertEqual(directory.visible_profiles(self.principal, self.org, search="Last"), [wanted])
        with self.assertRaises(PermissionDenied):
            directory.visible_profiles(Principal(user=self.other_admin), self.org)

    def test_external_reference_is_idempotent_namespaced_and_not_login_identity(self):
        person = self.person()
        values = {"source_namespace": "hr-production", "object_type": "employee", "external_id": "synthetic-123", "source_revision": "v1"}
        link = directory.add_external_reference(self.principal, person, values, person.revision)
        person.refresh_from_db()
        duplicate = directory.add_external_reference(self.principal, person, values, person.revision)
        self.assertEqual(link.pk, duplicate.pk)
        self.assertEqual(ExternalPersonLink.objects.count(), 1)
        other = self.person("other@example.test")
        with self.assertRaises(ValidationError):
            directory.add_external_reference(self.principal, other, values, other.revision)
        self.assertIsNone(person.membership_id)
        self.assertFalse(User.objects.filter(email=person.work_email).exists())

    def test_external_payload_cannot_write_profile_or_privileges(self):
        person = self.person()
        with self.assertRaises(ValidationError):
            directory.add_external_reference(self.principal, person, {"source_namespace": "hr", "object_type": "employee", "external_id": "123", "roles": ["admin"]}, 1)

    def test_explicit_invitation_is_hashed_private_single_use_and_ordinary_member(self):
        person = self.person("invited@example.test", is_manager=True)
        invitation, raw = directory.invite_profile(self.principal, person, person.revision)
        self.assertIsNotNone(raw)
        self.assertNotEqual(invitation.token_hash, raw)
        self.assertEqual(invitation.token_hash, Invitation.digest(raw))
        self.assertEqual(invitation.kind, "member")
        person.refresh_from_db()
        same, second_raw = directory.invite_profile(self.principal, person, person.revision)
        self.assertEqual(same.pk, invitation.pk)
        self.assertIsNone(second_raw)
        self.assertEqual(User.objects.filter(email=person.work_email).count(), 0)

    def test_email_change_revokes_pending_invitation(self):
        person = self.person()
        invitation, _ = directory.invite_profile(self.principal, person, person.revision)
        person.refresh_from_db()
        person = directory.update_profile(self.principal, person, {"work_email": "changed@example.test"}, person.revision)
        self.assertIsNone(person.invitation_id)
        self.assertFalse(Invitation.objects.filter(pk=invitation.pk).exists())

    def test_only_explicit_bound_matching_invitation_links_membership(self):
        person = self.person(self.user.email)
        invitation, _ = directory.invite_profile(self.principal, person, person.revision)
        linked = directory.link_accepted_invitation(invitation, self.member, Principal(user=self.user))
        self.assertEqual(linked.membership_id, self.member.pk)
        self.member.refresh_from_db()
        self.assertEqual(self.member.kind, "member")
        self.assertEqual(self.member.roles.count(), 0)
        unrelated = Invitation.objects.create(organization=self.org, email=self.user.email, token_hash=secrets.token_hex(32), expires=timezone.now() + timedelta(hours=1))
        self.assertIsNone(directory.link_accepted_invitation(unrelated, self.member, Principal(user=self.user)))

    def test_mismatched_or_expired_directory_invitation_cannot_link(self):
        person = self.person("different@example.test")
        invitation, _ = directory.invite_profile(self.principal, person, person.revision)
        with self.assertRaises(PermissionDenied):
            directory.link_accepted_invitation(invitation, self.member, Principal(user=self.user))
        Invitation.objects.filter(pk=invitation.pk).update(expires=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PermissionDenied):
            directory.link_accepted_invitation(invitation, self.member, Principal(user=self.user))

    def test_invitation_own_tags_deny_linking_without_grant(self):
        person = self.person(self.user.email)
        invitation, _ = directory.invite_profile(self.principal, person, person.revision)
        tag = PermissionTag.objects.create(organization=self.org, slug="invite-review", name="Invitation review")
        invitation.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            directory.link_accepted_invitation(invitation, self.member, Principal(user=self.user))
        person.refresh_from_db()
        self.assertIsNone(person.membership_id)

    def test_database_enforces_self_edge_and_one_active_primary(self):
        employee = self.person(is_manager=True)
        manager = self.person("manager@example.test", is_manager=True)
        second = self.person("second@example.test", is_manager=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReportingAssignment.objects.create(organization=self.org, manager=employee, employee=employee)
        ReportingAssignment.objects.create(organization=self.org, manager=manager, employee=employee, is_primary=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReportingAssignment.objects.create(organization=self.org, manager=second, employee=employee, is_primary=True)

    def test_audit_contains_identifiers_and_revisions_without_personal_contents(self):
        person = self.person("private-person@example.test", first_name="PrivateName", title="PrivateTitle")
        events = AuditEvent.objects.filter(action__startswith="directory.")
        self.assertTrue(events.filter(target=f"{person.pk}:1").exists())
        for event in events:
            self.assertNotIn(person.work_email, event.target)
            self.assertNotIn("PrivateName", event.target)
            self.assertNotIn("PrivateTitle", event.target)

    def test_directory_read_queries_stay_bounded_as_linked_people_grow(self):
        manager = self.person("manager@example.test", is_manager=True)

        def add_people(start, stop):
            users = [User(email=f"query-{number}@example.test", password="!") for number in range(start, stop)]
            User.objects.bulk_create(users)
            members = [Membership(organization=self.org, user=user) for user in users]
            Membership.objects.bulk_create(members)
            people = [EmployeeProfile(organization=self.org, membership=member, work_email=member.user.email) for member in members]
            EmployeeProfile.objects.bulk_create(people)
            ReportingAssignment.objects.bulk_create([ReportingAssignment(organization=self.org, manager=manager, employee=person) for person in people])

        def read_directory():
            with CaptureQueriesContext(connection) as queries:
                people = directory.visible_profiles(self.principal, self.org)
                assignments = directory.visible_assignments(self.principal, self.org)
            return len(queries), len(people), len(assignments)

        add_people(0, 2)
        small_count, _, _ = read_directory()
        add_people(2, 24)
        large_count, people_count, assignment_count = read_directory()
        self.assertEqual((people_count, assignment_count), (25, 24))
        self.assertLessEqual(large_count, small_count + 8, "Directory reads must not issue a new query for every additional person or reporting edge.")
        Membership.objects.filter(pk=self.admin_member.pk).update(active=False)
        with self.assertRaises(PermissionDenied):
            directory.visible_profiles(self.principal, self.org)
