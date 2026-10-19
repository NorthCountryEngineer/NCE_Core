"""Organization-owned employment records, independent of login privileges."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from .models import TaggedResource


class EmployeeProfile(TaggedResource):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        LEAVE = "leave", "On leave"
        INACTIVE = "inactive", "Inactive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("identity.Organization", on_delete=models.CASCADE, related_name="employee_profiles")
    membership = models.OneToOneField("identity.Membership", on_delete=models.SET_NULL, null=True, blank=True, related_name="employee_profile")
    invitation = models.OneToOneField("identity.Invitation", on_delete=models.SET_NULL, null=True, blank=True, related_name="employee_profile")
    work_email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    title = models.CharField(max_length=160, blank=True)
    department = models.CharField(max_length=160, blank=True)
    employee_code = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    is_manager = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "identity"
        ordering = ["last_name", "first_name", "work_email", "id"]
        constraints = [models.UniqueConstraint(Lower("work_email"), "organization", name="unique_org_employee_email")]

    @property
    def display_name(self):
        return " ".join(part for part in [self.first_name, self.last_name] if part) or self.work_email

    def __str__(self):
        return self.display_name

    def clean(self):
        self.work_email = self.work_email.strip().lower()
        if self.membership_id and self.membership.organization_id != self.organization_id:
            raise ValidationError({"membership": "Choose a membership from this organization."})
        if self.invitation_id and self.invitation.organization_id != self.organization_id:
            raise ValidationError({"invitation": "Choose an invitation from this organization."})
        previous = type(self).objects.filter(pk=self.pk).first()
        if previous and previous.organization_id != self.organization_id:
            raise ValidationError("The employee's organization cannot change.")


class ReportingAssignment(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("identity.Organization", on_delete=models.CASCADE, related_name="reporting_assignments")
    manager = models.ForeignKey(EmployeeProfile, on_delete=models.PROTECT, related_name="report_assignments")
    employee = models.ForeignKey(EmployeeProfile, on_delete=models.PROTECT, related_name="manager_assignments")
    active = models.BooleanField(default=True)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "identity"
        ordering = ["-is_primary", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "manager", "employee"], name="unique_org_reporting_pair"),
            models.UniqueConstraint(fields=["employee"], condition=models.Q(active=True, is_primary=True), name="one_active_primary_manager"),
            models.CheckConstraint(condition=~models.Q(manager=models.F("employee")), name="reporting_not_self"),
        ]

    def clean(self):
        if self.manager_id == self.employee_id:
            raise ValidationError("An employee cannot report to themselves.")
        for field in ["manager", "employee"]:
            endpoint = getattr(self, field, None)
            if endpoint and endpoint.organization_id != self.organization_id:
                raise ValidationError("Both people must belong to this organization.")
        if self.active and self.manager_id and not self.manager.is_manager:
            raise ValidationError("The assigned manager must be marked as a manager.")


class ExternalPersonLink(TaggedResource):
    """A provider reference is provenance and never an authentication identity."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("identity.Organization", on_delete=models.CASCADE, related_name="external_person_links")
    profile = models.ForeignKey(EmployeeProfile, on_delete=models.PROTECT, related_name="external_links")
    source_namespace = models.SlugField(max_length=80)
    object_type = models.SlugField(max_length=80)
    external_id = models.CharField(max_length=255)
    source_revision = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "identity"
        constraints = [models.UniqueConstraint(fields=["organization", "source_namespace", "object_type", "external_id"], name="unique_org_external_person_ref")]

    def clean(self):
        if self.profile_id and self.profile.organization_id != self.organization_id:
            raise ValidationError("The referenced person must belong to this organization.")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.external_id + self.source_revision):
            raise ValidationError("External references must not contain control characters.")
