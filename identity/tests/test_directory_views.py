"""Synthetic browser contract tests for organization people administration."""
import secrets

from django.test import Client, TestCase
from django.urls import reverse

from identity.models import AuditEvent, EmployeeProfile, ExternalPersonLink, Invitation, Membership, Organization, PermissionTag, ReportingAssignment, User


class DirectoryViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(slug="directory-views", name="Directory views")
        cls.other_org = Organization.objects.create(slug="directory-other", name="Other directory")
        cls.admin = User.objects.create_user("directory-admin@example.test", secrets.token_urlsafe(32))
        cls.member = User.objects.create_user("directory-member@example.test", secrets.token_urlsafe(32))
        cls.admin_membership = Membership.objects.create(organization=cls.org, user=cls.admin, kind="administrator")
        cls.membership = Membership.objects.create(organization=cls.org, user=cls.member)
        cls.person = EmployeeProfile.objects.create(organization=cls.org, first_name="Synthetic", last_name="Employee",
            work_email="synthetic-employee@example.test", title="Engineer", department="Delivery")
        cls.manager = EmployeeProfile.objects.create(organization=cls.org, first_name="Synthetic", last_name="Manager",
            work_email="synthetic-manager@example.test", is_manager=True)
        cls.foreign = EmployeeProfile.objects.create(organization=cls.other_org, first_name="Foreign", last_name="Person",
            work_email="foreign-person@example.test", is_manager=True)

    def setUp(self):
        self.client.force_login(self.admin)
        self.list_url = reverse("directory_people", args=[self.org.slug])
        self.detail_url = reverse("directory_person", args=[self.org.slug, self.person.pk])

    def payload(self, **changes):
        return {"first_name": "Created", "last_name": "Employee", "work_email": "created-employee@example.test",
            "title": "Developer", "department": "Delivery", "employee_code": "synthetic-employee", "status": "active", **changes}

    def test_directory_and_detail_require_current_administrator(self):
        self.client.force_login(self.member)
        for url in [self.list_url, self.detail_url]:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, self.payload()).status_code, 403)
        self.client.force_login(self.admin)
        self.admin_membership.active = False
        self.admin_membership.save(update_fields=["active"])
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_list_search_filter_and_organization_navigation(self):
        response = self.client.get(self.list_url, {"q": "Delivery", "role": "employee", "status": "active"})
        self.assertContains(response, self.person.work_email)
        self.assertNotContains(response, self.manager.work_email)
        self.assertNotContains(response, self.foreign.work_email)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(self.client.get(reverse("organization", args=[self.org.slug])), self.list_url)

    def test_create_person_optional_member_link_preserves_identity_and_roles(self):
        old_password = self.member.password
        response = self.client.post(self.list_url, self.payload(membership=self.membership.pk, is_manager="on",
            password=secrets.token_urlsafe(32), kind="administrator", is_superuser="true"))
        created = EmployeeProfile.objects.get(work_email="created-employee@example.test")
        self.assertRedirects(response, reverse("directory_person", args=[self.org.slug, created.pk]))
        self.assertEqual(created.membership_id, self.membership.pk)
        self.assertTrue(created.is_manager)
        self.member.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.member.password, old_password)
        self.assertEqual(self.member.email, "directory-member@example.test")
        self.assertEqual(self.membership.kind, "member")
        self.assertFalse(self.member.is_staff or self.member.is_superuser)

    def test_profile_update_and_stale_form(self):
        data = self.payload(action="profile", expected_revision=self.person.revision, first_name="Changed",
            work_email=self.person.work_email, employee_code="")
        response = self.client.post(self.detail_url, data)
        self.assertRedirects(response, self.detail_url)
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, "Changed")
        response = self.client.post(self.detail_url, {**data, "first_name": "Stale overwrite"})
        self.assertContains(response, "Reload", status_code=409)
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, "Changed")

    def test_manager_assignment_and_primary_are_explicit(self):
        response = self.client.post(self.detail_url, {"action": "managers", "expected_revision": self.person.revision,
            "managers": [self.manager.pk], "primary_manager": self.manager.pk})
        self.assertRedirects(response, self.detail_url)
        self.assertTrue(ReportingAssignment.objects.filter(employee=self.person, manager=self.manager, active=True, is_primary=True).exists())
        self.assertContains(self.client.get(self.detail_url), "Primary manager")

    def test_primary_manager_must_be_selected_and_foreign_manager_not_offered(self):
        response = self.client.post(self.detail_url, {"action": "managers", "expected_revision": self.person.revision,
            "primary_manager": self.manager.pk})
        self.assertContains(response, "Select the primary manager in the managers list too.")
        response = self.client.post(self.detail_url, {"action": "managers", "expected_revision": self.person.revision,
            "managers": [self.foreign.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReportingAssignment.objects.exists())
        self.assertNotContains(response, self.foreign.work_email)

    def test_cross_organization_profiles_and_memberships_are_unavailable(self):
        self.assertEqual(self.client.get(reverse("directory_person", args=[self.org.slug, self.foreign.pk])).status_code, 404)
        other_user = User.objects.create_user("foreign-member@example.test", secrets.token_urlsafe(32))
        foreign_membership = Membership.objects.create(organization=self.other_org, user=other_user)
        response = self.client.post(self.detail_url, {"action": "membership", "expected_revision": self.person.revision,
            "membership": foreign_membership.pk})
        self.assertEqual(response.status_code, 200)
        self.person.refresh_from_db()
        self.assertIsNone(self.person.membership_id)
        self.assertNotContains(response, other_user.email)

    def test_protected_profiles_and_member_labels_are_hidden(self):
        tag = PermissionTag.objects.create(organization=self.org, slug="private-directory", name="Private directory", requires_mfa=True)
        self.manager.permission_tags.add(tag)
        self.membership.permission_tags.add(tag)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, self.manager.work_email)
        self.assertNotContains(response, self.member.email)
        self.assertEqual(self.client.get(reverse("directory_person", args=[self.org.slug, self.manager.pk])).status_code, 403)

    def test_disabled_members_and_managers_are_not_assignment_choices(self):
        self.membership.active = False
        self.membership.save(update_fields=["active"])
        self.manager.membership = self.membership
        self.manager.save(update_fields=["membership"])
        listing = self.client.get(self.list_url)
        self.assertNotContains(listing, self.member.email)
        detail = self.client.get(self.detail_url)
        self.assertNotIn(self.manager.pk, [item.pk for item in detail.context["forms"]["managers"].fields["managers"].queryset])

    def test_ordinary_employees_cannot_crowd_manager_choices(self):
        EmployeeProfile.objects.bulk_create([EmployeeProfile(organization=self.org, first_name="A", last_name=f"Employee {number:04}",
            work_email=f"ordinary-{number}@example.test") for number in range(1001)])
        response = self.client.get(self.detail_url)
        choices = response.context["forms"]["managers"].fields["managers"].queryset
        self.assertIn(self.manager.pk, [candidate.pk for candidate in choices])
        self.assertTrue(response.context["manager_choices_complete"])

    def test_incomplete_manager_choices_preserve_late_assignment_and_require_search(self):
        EmployeeProfile.objects.bulk_create([EmployeeProfile(organization=self.org, first_name="A", last_name=f"A manager {number:04}",
            work_email=f"manager-{number}@example.test", is_manager=True) for number in range(1001)])
        edge = ReportingAssignment.objects.create(organization=self.org, employee=self.person, manager=self.manager, active=True, is_primary=True)
        response = self.client.get(self.detail_url)
        self.assertFalse(response.context["manager_choices_complete"])
        self.assertIn(self.manager.pk, [candidate.pk for candidate in response.context["forms"]["managers"].fields["managers"].queryset])
        self.assertEqual(response.context["forms"]["managers"].initial["managers"], [self.manager.pk])
        blocked = self.client.post(self.detail_url, {"action": "managers", "expected_revision": self.person.revision})
        self.assertContains(blocked, "Narrow the manager search before saving.")
        edge.refresh_from_db()
        self.assertTrue(edge.active)
        narrowed = self.client.get(self.detail_url, {"manager_q": "synthetic-manager@example.test"})
        self.assertTrue(narrowed.context["manager_choices_complete"])
        self.assertEqual(narrowed.context["forms"]["managers"].initial["managers"], [self.manager.pk])

    def test_history_only_includes_current_authorized_person_events(self):
        AuditEvent.objects.create(organization=self.org, actor=self.admin, action="directory.profile_updated", target=f"{self.person.pk}:2")
        AuditEvent.objects.create(organization=self.org, actor=self.admin, action="directory.unrelated_private_action", target=f"{self.manager.pk}:3")
        response = self.client.get(self.detail_url)
        self.assertContains(response, "directory.profile_updated")
        self.assertNotContains(response, "directory.unrelated_private_action")
        self.assertContains(response, "do not contain a full copy")

    def test_invitation_uses_single_display_setup_link_without_account_creation(self):
        count = User.objects.count()
        response = self.client.post(self.detail_url, {"action": "invite", "expected_revision": self.person.revision})
        self.assertContains(response, "Invitation created")
        self.assertContains(response, "/invitations/")
        self.assertContains(response, "48 hours")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotContains(response, 'name="password"')
        self.assertEqual(User.objects.count(), count)
        self.assertEqual(Invitation.objects.get().kind, "member")
        self.person.refresh_from_db()
        repeated = self.client.post(self.detail_url, {"action": "invite", "expected_revision": self.person.revision})
        self.assertRedirects(repeated, self.detail_url)
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertNotContains(self.client.get(self.detail_url), response.context["invitation_link"])

    def test_external_reference_is_local_metadata_and_does_not_touch_identity(self):
        response = self.client.post(self.detail_url, {"action": "external", "expected_revision": self.person.revision,
            "source_namespace": "greenhouse", "object_type": "candidate", "external_id": "synthetic-234", "source_revision": "r1"})
        self.assertRedirects(response, self.detail_url)
        self.assertEqual(ExternalPersonLink.objects.get().profile, self.person)
        self.assertContains(self.client.get(self.detail_url), "does not connect")

    def test_all_mutations_require_csrf_and_methods_are_bounded(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(self.list_url, self.payload()).status_code, 403)
        self.assertEqual(browser.post(self.detail_url, {"action": "invite", "expected_revision": self.person.revision}).status_code, 403)
        self.assertEqual(self.client.put(self.detail_url).status_code, 405)
        self.assertEqual(self.client.put(self.list_url).status_code, 405)
