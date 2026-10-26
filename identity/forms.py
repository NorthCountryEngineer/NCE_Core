from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from .models import Application, Invitation, Membership, Organization, Role, Service, ServicePermission, User
from .validators import validate_avatar_url


class CoreAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Let keyboard users reach the skip link before entering credentials.
        self.fields['username'].widget.attrs.pop('autofocus', None)


class ProfileForm(forms.ModelForm):
    def clean_avatar_url(self):
        # URLField may normalize a missing scheme or strip URL control characters.
        # Validate what was submitted before accepting that normalization.
        validate_avatar_url(self.data.get(self.add_prefix("avatar_url"), "").strip())
        return self.cleaned_data["avatar_url"]

    class Meta:
        model = User
        fields = ["first_name", "last_name", "avatar_url"]
        labels = {"avatar_url": "Avatar image URL"}
        help_texts = {
            "avatar_url": "Optional public HTTPS image URL. Your browser loads it from that host. Leave blank to remove your avatar.",
        }
        widgets = {
            "first_name": forms.TextInput(attrs={"autocomplete": "given-name"}),
            "last_name": forms.TextInput(attrs={"autocomplete": "family-name"}),
            "avatar_url": forms.URLInput(attrs={"autocomplete": "off", "placeholder": "https://example.com/avatar.png"}),
        }


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
        fields = ["slug", "name", "active", "auth_name", "auth_theme"]


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
