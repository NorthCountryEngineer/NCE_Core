from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render

from identity.access import request_principal, require_access, visible_resources
from identity.views import audit, edit_form, organization_admin
from .environments import create_environment
from .models import DeploymentEnvironment, RuntimeApplication
from .modernization import adopt_application
from .validation import CAPABILITIES, validate_endpoint, validate_manifest


class EnvironmentForm(forms.ModelForm):
    class Meta:
        model = DeploymentEnvironment
        fields = ['name', 'namespace', 'application_origin', 'ingress_class', 'tls_secret_name', 'auto_deploy', 'active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            self.fields['name'].disabled = True
            self.fields['namespace'].disabled = True


@organization_admin
def environments(request, organization):
    return render(request, 'platform_services/environments.html', {'organization': organization,
        'environments': visible_resources(request_principal(request), organization.environments.all())})


@organization_admin
def environment_edit(request, organization, pk=None):
    instance = get_object_or_404(organization.environments, pk=pk) if pk else DeploymentEnvironment(organization=organization)
    if pk:
        require_access(request_principal(request), instance, 'environment.manage')
    form = EnvironmentForm(request.POST or None, instance=instance)
    if pk:
        return edit_form(request, form, 'Environment configuration', '/organizations/' + organization.slug + '/platform/environments/', 'environment.updated')
    if request.method == 'POST' and form.is_valid():
        try:
            environment = create_environment(organization, **form.cleaned_data)
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'The environment or namespace already exists.')
        else:
            audit(request, 'environment.created', environment.pk)
            return redirect('platform_environments', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Create a segregated deployment environment'})


class ModernizationForm(forms.Form):
    application = forms.ModelChoiceField(queryset=RuntimeApplication.objects.none(), label='Discovered Kubernetes Deployment')
    container = forms.RegexField(regex=r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$', max_length=63,
        help_text='Container name from the inventory below. Other containers keep their existing configuration.')
    name = forms.CharField(max_length=160, label='Application display name')
    slug = forms.SlugField(max_length=50, label='Application reference')
    endpoint = forms.URLField(validators=[validate_endpoint], label='Existing application origin')
    callback_path = forms.CharField(initial='/oauth/callback', max_length=200)
    capabilities = forms.MultipleChoiceField(choices=[(c, c) for c in sorted(CAPABILITIES)], required=False,
        widget=forms.CheckboxSelectMultiple, label='Approved Core service capabilities')
    confirm = forms.BooleanField(label='Connect this container to Core. Injecting integration configuration rolls its pods; existing replicas, images, storage, Services, and Ingress remain managed by the application owner.')

    def __init__(self, *args, organization, principal, **kwargs):
        super().__init__(*args, **kwargs)
        node_ids = [node.pk for node in visible_resources(principal, organization.runtime_nodes.filter(active=True, environment__active=True))]
        self.fields['application'].queryset = RuntimeApplication.objects.filter(node_id__in=node_ids).select_related('node__environment')

    def clean_callback_path(self):
        value = self.cleaned_data['callback_path']
        validate_manifest({'callback_path': value})
        return value


@organization_admin
def modernize(request, organization):
    principal = request_principal(request)
    form = ModernizationForm(request.POST or None, organization=organization, principal=principal)
    if request.method == 'POST' and form.is_valid():
        values = form.cleaned_data
        try:
            adopt_application(organization, values['application'], values['container'], values['slug'], values['name'],
                values['endpoint'], values['capabilities'], values['callback_path'], principal)
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'This application is already registered.')
        else:
            return redirect('platform_dashboard', slug=organization.slug)
    return render(request, 'platform_services/modernize.html', {'form': form,
        'applications': form.fields['application'].queryset, 'organization': organization})
