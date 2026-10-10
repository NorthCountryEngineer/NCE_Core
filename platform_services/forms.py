from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError

from identity.models import Organization
from .models import (
    AgentDefinition, AgentTask, BillingProfile, Dataset, DeploymentConfiguration, Installation,
    OrganizationProfile, Pipeline, RuntimeNode, Workflow, DeploymentEnvironment,
)


class OrganizationOnboardingForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "slug", "identity_name"]


class MetadataForm(forms.Form):
    legal_name = forms.CharField(max_length=200)
    country = forms.CharField(min_length=2, max_length=2, help_text="Two-letter country code")
    support_email = forms.EmailField()
    timezone = forms.CharField(max_length=80, initial="UTC")
    billing_email = forms.EmailField()
    currency = forms.CharField(min_length=3, max_length=3, initial="USD")
    provider = forms.ChoiceField(choices=BillingProfile.PROVIDERS, help_text="Preference only. Payments are not enabled.")

    def clean_country(self):
        value = self.cleaned_data["country"].upper()
        if not value.isalpha() or not value.isascii():
            raise ValidationError("Use a two-letter country code.")
        return value

    def clean_currency(self):
        value = self.cleaned_data["currency"].upper()
        if not value.isalpha() or not value.isascii():
            raise ValidationError("Use a three-letter currency code.")
        return value

    def clean_timezone(self):
        value = self.cleaned_data["timezone"]
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("Use an IANA timezone, such as America/New_York.")
        return value


class DeploymentConfigurationForm(forms.ModelForm):
    class Meta:
        model = DeploymentConfiguration
        fields = ["environment", "namespace", "application_origin", "ingress_class", "tls_secret_name", "auto_deploy"]
        help_texts = {"auto_deploy": "Start approved marketplace applications on the connected cluster. No cluster is provisioned.",
                      "application_origin": "Default application address. Each deployment can use a different address."}


class ReviewForm(forms.Form):
    confirm = forms.BooleanField(label="Create my organization's core workspace with administrator access. Other members start with no service permissions.")


class InstallationForm(forms.Form):
    grants = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
                                       label="Application access to core services")

    def __init__(self, *args, package, manifest=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["grants"].choices = [(c, c) for c in (manifest if manifest is not None else package.manifest).get("capabilities", [])]


class InstallationSettingsForm(InstallationForm):
    enabled = forms.BooleanField(required=False)
    header_mode = forms.ChoiceField(required=False, choices=Installation._meta.get_field('header_mode').choices,
        label='Shared organization header', help_text='Applications include the reusable header component and backend adapter to use this setting.')


class NodeForm(forms.Form):
    name = forms.CharField(max_length=100)
    environment = forms.ModelChoiceField(queryset=DeploymentEnvironment.objects.none(), required=False)

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['environment'].queryset = organization.environments.filter(active=True)
            self.fields['environment'].required = True
        else:
            del self.fields['environment']


class DeploymentForm(forms.Form):
    node = forms.ModelChoiceField(queryset=RuntimeNode.objects.none(), label="Connected Kubernetes runtime")
    endpoint = forms.URLField(label="Application origin")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["node"].queryset = organization.runtime_nodes.filter(active=True)
        from .validation import validate_endpoint
        self.fields["endpoint"].validators.append(validate_endpoint)


class AdoptControllerForm(forms.Form):
    node = forms.ModelChoiceField(queryset=RuntimeNode.objects.none())
    resource_name = forms.RegexField(regex=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", max_length=63,
        help_text="Existing Core Platform Kubernetes Deployment. Its core.platform/organization label must match this organization.")

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["node"].queryset = organization.runtime_nodes.filter(active=True)


class DatasetForm(forms.ModelForm):
    class Meta:
        model = Dataset
        fields = ["slug", "name", "schema", "data_class", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            self.fields['data_class'].disabled = True


class PipelineForm(forms.ModelForm):
    class Meta:
        model = Pipeline
        fields = ["slug", "name", "dataset", "transforms", "active"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dataset"].queryset = organization.datasets.filter(active=True)


class AgentForm(forms.ModelForm):
    class Meta:
        model = AgentDefinition
        fields = ["slug", "name", "kind", "configuration", "active"]


class WorkflowForm(forms.ModelForm):
    class Meta:
        model = Workflow
        fields = ["slug", "name", "steps", "active"]


class RunForm(forms.Form):
    input = forms.JSONField(initial={})
    environment = forms.ChoiceField(choices=[(v, v.title()) for v in ('sandbox', 'development', 'staging', 'uat', 'production')], required=False, initial='development')
    task = forms.ModelChoiceField(queryset=AgentTask.objects.none(), required=False)

    def __init__(self, *args, organization, principal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['task'].queryset = organization.agent_tasks.filter(active=True)
        if principal:
            from identity.access import visible_resources
            self.fields['task'].queryset = self.fields['task'].queryset.filter(
                pk__in=[item.pk for item in visible_resources(principal, self.fields['task'].queryset)])

    def clean_input(self):
        value = self.cleaned_data["input"]
        if not isinstance(value, dict):
            raise ValidationError("Use a JSON object.")
        return value
