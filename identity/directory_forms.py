"""Allowlisted, organization-scoped people administration forms."""
from django import forms

from .models import EmployeeProfile, Membership


class EmployeeProfileForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        fields = ["first_name", "last_name", "work_email", "title", "department", "employee_code", "status", "is_manager"]
        labels = {"is_manager": "May manage employees", "employee_code": "Employee reference"}
        help_texts = {
            "work_email": "Work contact address. This does not change a linked account's sign-in email.",
            "status": "Directory status only. Manage sign-in and application permissions separately through membership access.",
            "is_manager": "Allows reporting assignments. This grants no administrator or application permissions.",
        }

    def clean_work_email(self):
        return self.cleaned_data["work_email"].strip().lower()


class RevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class EmployeeUpdateForm(EmployeeProfileForm):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class MembershipChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.user.email


class DirectoryMembershipForm(RevisionForm):
    membership = MembershipChoiceField(queryset=Membership.objects.none(), required=False,
        empty_label="No linked sign-in account", label="Organization member",
        help_text="Explicitly link an existing member. This does not change their name, email, password, roles or access.")

    def __init__(self, *args, memberships, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["membership"].queryset = memberships


class DirectoryCreateForm(EmployeeProfileForm):
    membership = MembershipChoiceField(queryset=Membership.objects.none(), required=False,
        empty_label="Create the directory record first", label="Link an existing organization member",
        help_text="Optional. You can invite this person or explicitly link a member later.")

    def __init__(self, *args, memberships, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["membership"].queryset = memberships


def manager_available(person):
    return person.status == EmployeeProfile.Status.ACTIVE and person.is_manager and (
        not person.membership_id or person.membership.active and person.membership.user.is_active)


def manager_label(person):
    return person.display_name if manager_available(person) else f"{person.display_name} (unavailable; remove or restore this assignment)"


class ManagerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return manager_label(obj)


class ManagersChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return manager_label(obj)


class ManagerAssignmentForm(RevisionForm):
    managers = ManagersChoiceField(queryset=EmployeeProfile.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
        error_messages={"invalid_choice": "Choose a currently available manager.", "invalid_pk_value": "Choose a currently available manager.", "invalid_list": "Choose managers from the list."},
        help_text="A manager may also report to other managers. Existing unavailable assignments remain selected until explicitly removed. Reporting relationships grant no application access.")
    primary_manager = ManagerChoiceField(queryset=EmployeeProfile.objects.none(), required=False,
        empty_label="No primary manager", help_text="Optional. Select a manager included above.")

    def __init__(self, *args, managers, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["managers"].queryset = managers
        self.fields["primary_manager"].queryset = managers

    def clean(self):
        values = super().clean()
        primary = values.get("primary_manager")
        if primary and primary not in values.get("managers", []):
            self.add_error("primary_manager", "Select the primary manager in the managers list too.")
        return values


class ExternalPersonReferenceForm(RevisionForm):
    source_namespace = forms.SlugField(max_length=80, label="Source namespace",
        help_text="A local source label, for example greenhouse. Saving a reference does not connect to that service.")
    object_type = forms.SlugField(max_length=80, label="External record type")
    external_id = forms.CharField(max_length=255, label="External record ID")
    source_revision = forms.CharField(max_length=255, required=False)
