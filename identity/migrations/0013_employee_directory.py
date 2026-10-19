"""Add organization directory metadata without changing accounts or grants."""
import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [("identity", "0012_application_accounts")]

    operations = [
        migrations.CreateModel(
            name="EmployeeProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("work_email", models.EmailField(max_length=254)),
                ("first_name", models.CharField(blank=True, max_length=150)),
                ("last_name", models.CharField(blank=True, max_length=150)),
                ("title", models.CharField(blank=True, max_length=160)),
                ("department", models.CharField(blank=True, max_length=160)),
                ("employee_code", models.CharField(blank=True, max_length=80)),
                ("status", models.CharField(choices=[("active", "Active"), ("leave", "On leave"), ("inactive", "Inactive")], default="active", max_length=12)),
                ("is_manager", models.BooleanField(default=False)),
                ("revision", models.PositiveIntegerField(default=1, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("invitation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="employee_profile", to="identity.invitation")),
                ("membership", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="employee_profile", to="identity.membership")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_profiles", to="identity.organization")),
                ("permission_tags", models.ManyToManyField(blank=True, related_name="%(app_label)s_%(class)s_resources", to="identity.permissiontag")),
            ],
            options={
                "ordering": ["last_name", "first_name", "work_email", "id"],
                "constraints": [models.UniqueConstraint(Lower("work_email"), models.F("organization"), name="unique_org_employee_email")],
            },
        ),
        migrations.CreateModel(
            name="ReportingAssignment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("active", models.BooleanField(default=True)),
                ("is_primary", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="manager_assignments", to="identity.employeeprofile")),
                ("manager", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="report_assignments", to="identity.employeeprofile")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reporting_assignments", to="identity.organization")),
                ("permission_tags", models.ManyToManyField(blank=True, related_name="%(app_label)s_%(class)s_resources", to="identity.permissiontag")),
            ],
            options={
                "ordering": ["-is_primary", "created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "manager", "employee"), name="unique_org_reporting_pair"),
                    models.UniqueConstraint(condition=models.Q(active=True, is_primary=True), fields=("employee",), name="one_active_primary_manager"),
                    models.CheckConstraint(condition=~models.Q(manager=models.F("employee")), name="reporting_not_self"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ExternalPersonLink",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_namespace", models.SlugField(max_length=80)),
                ("object_type", models.SlugField(max_length=80)),
                ("external_id", models.CharField(max_length=255)),
                ("source_revision", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_person_links", to="identity.organization")),
                ("permission_tags", models.ManyToManyField(blank=True, related_name="%(app_label)s_%(class)s_resources", to="identity.permissiontag")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="external_links", to="identity.employeeprofile")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("organization", "source_namespace", "object_type", "external_id"), name="unique_org_external_person_ref")]},
        ),
    ]
