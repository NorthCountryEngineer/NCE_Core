from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render

from identity.access import effective_tags, record_decision, request_principal, require_access, visible_resources
from identity.views import organization_admin
from .integration_models import ApplicationIntegration, IntegrationConnection, IntegrationDefinition
from .integrations import publish_definition, replace_credential


SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'base_url': {'type': 'string', 'format': 'uri'}}, 'required': ['base_url']}


class DefinitionForm(forms.ModelForm):
    class Meta:
        model = IntegrationDefinition
        fields = ['slug', 'name', 'kind', 'service', 'provider', 'configuration_schema', 'active']


class ConnectionForm(forms.ModelForm):
    credential = forms.CharField(required=False, max_length=8192, widget=forms.PasswordInput,
        help_text='Blank preserves the encrypted credential. A changed configuration requires replacement or explicit clear.')
    clear_credential = forms.BooleanField(required=False)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    class Meta:
        model = IntegrationConnection
        fields = ['definition', 'environment', 'configuration', 'active']


class ApplicationForm(ConnectionForm):
    class Meta:
        model = ApplicationIntegration
        fields = ['installation', 'definition', 'environment', 'mode', 'connection', 'configuration',
            'allow_inheritance', 'allow_application_configuration', 'workload_access', 'credential_access']


@organization_admin
def integration_settings(request, organization):
    principal = request_principal(request)
    return render(request, 'platform_services/integrations.html', {'organization': organization,
        'definitions': visible_resources(principal, organization.integration_definitions.select_related('service', 'provider__package')),
        'connections': visible_resources(principal, organization.integration_connections.select_related('definition', 'environment')),
        'bindings': visible_resources(principal, organization.application_integrations.select_related('installation__package', 'definition', 'environment'))})


def edit(request, organization, model, form_class, title, pk=None):
    principal = request_principal(request)
    instance = get_object_or_404(model, pk=pk, organization=organization) if pk else model(organization=organization)
    if pk:
        require_access(principal, instance, 'integration.manage')
    previous = getattr(instance, 'configuration', None)
    initial = {'expected_revision': getattr(instance, 'revision', 1)}
    if model is IntegrationDefinition and not pk:
        initial['configuration_schema'] = SCHEMA
        initial['service'] = organization.services.filter(slug='core-platform').first()
    form = form_class(request.POST or None, instance=instance, initial=initial)
    if 'credential' in form.fields:
        status = 'configured' if getattr(instance, 'encrypted_credential', '') else 'not configured'
        form.fields['credential'].help_text = 'Credential ' + status + '. ' + form.fields['credential'].help_text
    querysets = {'service': organization.services.filter(active=True), 'provider': organization.installations.filter(enabled=True),
        'installation': organization.installations.filter(enabled=True), 'definition': organization.integration_definitions.all(),
        'environment': organization.environments.all(), 'connection': organization.integration_connections.all()}
    immutable = ['organization', 'slug', 'service', 'provider', 'kind', 'configuration_schema'] if model is IntegrationDefinition else ['installation', 'definition', 'environment']
    for field, queryset in querysets.items():
        if field in form.fields:
            form.fields[field].queryset = queryset.filter(pk__in=[item.pk for item in visible_resources(principal, queryset)])
    if pk:
        for field in immutable:
            if field in form.fields:
                form.fields[field].disabled = True
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                if pk:
                    current = model.objects.select_for_update(of=('self',)).get(pk=pk)
                    require_access(principal, current, 'integration.manage')
                    if hasattr(current, 'revision') and current.revision != form.cleaned_data['expected_revision']:
                        raise ValidationError('Settings changed. Reload before saving.')
                    # ModelForm mutated instance before validation; compare the persisted configuration.
                    previous = getattr(current, 'configuration', None)
                value = form.save(commit=False)
                for field in querysets:
                    related = getattr(value, field, None)
                    if related is not None:
                        require_access(principal, related, 'integration.configuration_resource')
                if model is IntegrationDefinition:
                    publish_definition(value, principal)
                else:
                    replace_credential(value, form.cleaned_data.get('credential') or None,
                        form.cleaned_data['clear_credential'], previous)
                    value.full_clean()
                    value.revision = getattr(current, 'revision', 0) + 1 if pk else 1
                    value.save()
                    record_decision(principal, value, 'integration.platform_configured', True, 'administrator_approved', effective_tags(value))
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'This definition or placement already exists.')
        else:
            return redirect('integration_settings', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': title,
        'credential_configured': bool(getattr(instance, 'encrypted_credential', ''))})


@organization_admin
def integration_definition(request, organization, pk=None):
    return edit(request, organization, IntegrationDefinition, DefinitionForm, 'Publish an integration contract', pk)


@organization_admin
def integration_connection(request, organization, pk=None):
    return edit(request, organization, IntegrationConnection, ConnectionForm, 'Platform connection settings', pk)


@organization_admin
def application_integration(request, organization, pk=None):
    return edit(request, organization, ApplicationIntegration, ApplicationForm, 'Application integration permissions and settings', pk)
