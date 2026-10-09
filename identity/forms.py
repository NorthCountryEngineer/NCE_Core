from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Application, Invitation, Membership, Organization, Role, Service, ServicePermission, User


class BrandingForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "identity_name"]


class InvitationForm(forms.ModelForm):
    class Meta:
        model = Invitation
        fields = ["email", "kind"]

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class AcceptInvitationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]


class MembershipForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["kind", "active", "roles"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["roles"].queryset = organization.roles.all()


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["slug", "name", "active"]


class PermissionForm(forms.ModelForm):
    class Meta:
        model = ServicePermission
        fields = ["codename", "name"]


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ["slug", "name", "permissions"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["permissions"].queryset = ServicePermission.objects.filter(service__organization=organization)


class ApplicationForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ["name", "client_type", "redirect_uris", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These security properties cannot be changed by tenant administrators.
        self.instance.authorization_grant_type = Application.GRANT_AUTHORIZATION_CODE
        self.instance.algorithm = Application.RS256_ALGORITHM
        self.instance.hash_client_secret = True
        self.instance.skip_authorization = False
