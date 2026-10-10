from functools import wraps
import json

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache

from core_fabric.models import FabricContract
from identity.access import record_decision, request_principal, require_access, visible_resources
from identity.models import Membership, Organization, PermissionTag, Role
from identity.views import organization_admin
from platform_services.ai_rules import RuleDenied
from platform_services.models import Dataset, DeploymentEnvironment, Installation
from platform_services.training import require_capability
from .models import QuerySource
from .services import catalog, query


def member(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, slug, *args, **kwargs):
        organization = get_object_or_404(Organization, slug=slug, active=True)
        if not Membership.objects.filter(organization=organization, user=request.user, active=True).exists():
            raise PermissionDenied
        request.organization = organization
        return view(request, organization, *args, **kwargs)
    return wrapped


class QueryForm(forms.Form):
    text = forms.CharField(max_length=200, required=False, label='Search accessible data',
        widget=forms.TextInput(attrs={'placeholder': 'A name, status, or phrase…'}))
    sources = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    limit = forms.IntegerField(min_value=1, max_value=200, initial=100, label='Maximum records to inspect')
    cursors = forms.JSONField(required=False, widget=forms.HiddenInput)


@member
def index(request, organization):
    principal = request_principal(request)
    state = catalog(principal, organization)
    form = QueryForm(request.POST or None)
    form.fields['sources'].choices = [(source['id'], f'{source["name"]} · {source["application"]} · {source["environment"]}') for source in state['sources']]
    value = None
    if request.method == 'POST' and form.is_valid():
        try:
            value = query(principal, organization, {**form.cleaned_data, 'cursors': form.cleaned_data['cursors'] or {}})
        except RuleDenied as error:
            form.add_error(None, f'The shared platform budget denied this query: {error.reason}.')
        except ValidationError:
            form.add_error(None, 'Review the selected sources, cursors, and inspection budget.')
    can_manage = request.user.is_superuser or Membership.objects.filter(organization=organization, user=request.user,
        active=True, kind='administrator').exists()
    return render(request, 'core_ai/index.html', {'organization': organization, 'form': form, 'result': value,
        'can_manage': can_manage, 'next_cursors_json': json.dumps(value['next_cursors']) if value else '{}',
        'selected_sources': form.cleaned_data.get('sources') or list(value['next_cursors']) if value else [],
        'search_text': form.cleaned_data.get('text', '') if value else '', 'budget': form.cleaned_data.get('limit', 100) if value else 100})


class SourceForm(forms.ModelForm):
    class Meta:
        model = QuerySource
        fields = ['name', 'installation', 'environment', 'dataset', 'contract', 'fields', 'filters',
            'required_permissions', 'allowed_roles', 'allowed_consumers', 'record_resource_type', 'permission_tags', 'active']

    def __init__(self, *args, organization, principal, **kwargs):
        super().__init__(*args, **kwargs)
        scopes = {'installation': Installation.objects.filter(organization=organization, package__kind='application', enabled=True),
            'allowed_consumers': Installation.objects.filter(organization=organization, package__kind='application', enabled=True),
            'environment': DeploymentEnvironment.objects.filter(organization=organization, active=True),
            'dataset': Dataset.objects.filter(organization=organization, active=True),
            'contract': FabricContract.objects.filter(organization=organization, active=True, direction='import', allow_read=True),
            'allowed_roles': Role.objects.filter(organization=organization),
            'permission_tags': PermissionTag.objects.filter(organization=organization, active=True)}
        for field, queryset in scopes.items():
            self.fields[field].queryset = queryset.filter(pk__in=[item.pk for item in visible_resources(principal, queryset)])
        self.fields['installation'].label = 'Application'
        for field in ['installation', 'allowed_consumers']:
            self.fields[field].label_from_instance = lambda item: f'{item.service.name} · {item.version}'
        self.fields['contract'].label_from_instance = lambda item: f'{item.name} · {item.purpose_code}'


@organization_admin
def administration(request, organization):
    principal = request_principal(request)
    require_capability(principal, organization, ['ai.manage'])
    items = visible_resources(principal, QuerySource.objects.filter(organization=organization).select_related('installation__service', 'environment'))
    return render(request, 'core_ai/admin.html', {'organization': organization, 'sources': items})


@organization_admin
def source_edit(request, organization, pk=None):
    principal = request_principal(request)
    require_capability(principal, organization, ['ai.manage'])
    item = get_object_or_404(QuerySource, organization=organization, pk=pk) if pk else QuerySource(organization=organization)
    if pk:
        require_access(principal, item, 'ai.source_manage')
    form = SourceForm(request.POST or None, instance=item, organization=organization, principal=principal)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        record_decision(principal, item, 'ai.source_configured', True, 'administrator_scope', [])
        return redirect('ai_admin', slug=organization.slug)
    return render(request, 'identity/form.html', {'title': 'Application query source', 'form': form, 'organization': organization})
