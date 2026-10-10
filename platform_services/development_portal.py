"""Central administrator workspace for recipes, experiments, and explicit sharing."""
import json
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from identity.access import effective_tags, record_decision, request_principal, require_access, visible_resources
from identity.views import organization_admin
from .ai_rules import RuleConflict, RuleDenied
from .development import (authorize_run, create_object, export_bundle, import_bundle, publish_revision,
    require_development, run_revision, save_revision, share_object)
from .models import DevelopmentObject, DevelopmentRevision, DevelopmentRun
from .rules_portal import PrettyJSONEncoder


class RecipeForm(forms.Form):
    slug = forms.SlugField(max_length=80)
    name = forms.CharField(max_length=160)
    kind = forms.ChoiceField(choices=DevelopmentObject._meta.get_field('kind').choices)
    installation_id = forms.ModelChoiceField(queryset=None, label='Owning application')
    environment_id = forms.ModelChoiceField(queryset=None, label='Environment')
    spec = forms.JSONField(encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 22, 'cols': 90}))


class RevisionForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    spec = forms.JSONField(encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 22, 'cols': 90}))


class RunForm(forms.Form):
    request_key = forms.RegexField(regex='^[a-zA-Z0-9_-]{1,100}$', max_length=100)
    inputs = forms.JSONField(required=False, encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 10, 'cols': 90}))


class SharingForm(forms.Form):
    consumers = forms.ModelMultipleChoiceField(queryset=None, required=False, label='Approved consuming applications')
    active = forms.BooleanField(required=False)


class ImportForm(forms.Form):
    specification = forms.JSONField(encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 18, 'cols': 90}),
        help_text='Bundle, explicit local bindings, installation_id, environment_id, and prefix. Imported recipes remain drafts.')


@organization_admin
@never_cache
def index(request, organization):
    principal = request_principal(request)
    require_development(principal, organization, 'development.read')
    entries = []
    for obj in organization.development_objects.select_related('environment', 'installation__package').order_by('-created')[:200]:
        try:
            require_access(principal, obj, 'development.catalog')
            visible = [v for v in obj.versions.order_by('-number')[:20] if _visible_revision(principal, v)]
            if visible:
                entries.append({'object': obj, 'version': visible[0]})
        except PermissionDenied:
            continue
    form = ImportForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            obj, _ = import_bundle(organization, principal, form.cleaned_data['specification'])
            return redirect('development_object', slug=organization.slug, pk=obj.pk)
        except (ValidationError, RuleConflict) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'A concurrent change requires reloading.')
    return render(request, 'platform_services/development_index.html', {'organization': organization, 'entries': entries, 'form': form})


def _visible_revision(principal, revision):
    try:
        require_access(principal, revision, 'development.workspace_read')
        return True
    except PermissionDenied:
        return False


@organization_admin
@never_cache
def create(request, organization):
    principal = request_principal(request)
    require_development(principal, organization, 'development.write')
    initial = {'kind': 'pipeline', 'spec': {'operator': 'transform', 'inputs': {'rows': 'rows'},
        'config': {'fields': ['id', 'value'], 'rename': {}, 'where': {}}}}
    form = RecipeForm(request.POST or None, initial=initial)
    form.fields['installation_id'].queryset = organization.installations.filter(pk__in=[i.pk for i in visible_resources(principal, organization.installations.filter(enabled=True))])
    form.fields['environment_id'].queryset = organization.environments.filter(pk__in=[i.pk for i in visible_resources(principal, organization.environments.filter(active=True))])
    if request.method == 'POST' and form.is_valid():
        values = {**form.cleaned_data, 'installation_id': str(form.cleaned_data['installation_id'].pk), 'environment_id': str(form.cleaned_data['environment_id'].pk)}
        try:
            obj = create_object(organization, principal, values)
            return redirect('development_object', slug=organization.slug, pk=obj.pk)
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, 'identity/form.html', {'title': 'New composable development object', 'form': form})


@organization_admin
@never_cache
def detail(request, organization, pk):
    principal = request_principal(request)
    obj = get_object_or_404(DevelopmentObject, pk=pk, organization=organization)
    require_development(principal, organization, 'development.read')
    require_access(principal, obj, 'development.workspace')
    versions = [v for v in obj.versions.order_by('-number')[:100] if _visible_revision(principal, v)]
    if not versions:
        raise PermissionDenied('No accessible revisions.')
    selected = versions[0]
    revision_form = RevisionForm(request.POST if request.POST.get('action') == 'revision' else None,
        initial={'revision': obj.revision, 'spec': selected.spec})
    run_form = RunForm(request.POST if request.POST.get('action') == 'run' else None, initial={'inputs': {}})
    share_form = SharingForm(request.POST if request.POST.get('action') == 'sharing' else None,
        initial={'consumers': obj.consumers.all(), 'active': obj.active})
    share_form.fields['consumers'].queryset = organization.installations.filter(pk__in=[i.pk for i in visible_resources(principal, organization.installations.filter(enabled=True))])
    error = None
    if request.method == 'POST':
        audit_resource = obj
        try:
            with transaction.atomic():
                action = request.POST.get('action')
                if action == 'revision' and revision_form.is_valid():
                    save_revision(obj, principal, **{'expected': revision_form.cleaned_data['revision'], 'spec': revision_form.cleaned_data['spec']})
                elif action == 'sharing' and share_form.is_valid():
                    share_object(obj, principal, [str(i.pk) for i in share_form.cleaned_data['consumers']], share_form.cleaned_data['active'])
                elif action in {'publish', 'withdraw', 'run'}:
                    version = get_object_or_404(DevelopmentRevision, pk=request.POST.get('revision_id'), development_object=obj)
                    audit_resource = version
                    if action == 'run':
                        if not run_form.is_valid():
                            raise ValidationError('Review the request key and typed inputs.')
                        run_revision(version, principal, {'request_key': run_form.cleaned_data['request_key'], 'inputs': run_form.cleaned_data['inputs'] or {}})
                    else:
                        publish_revision(version, principal, withdraw=action == 'withdraw')
                else:
                    raise ValidationError('Review the selected action and form.')
            return redirect('development_object', slug=organization.slug, pk=obj.pk)
        except (ValidationError, RuleDenied, RuleConflict) as exc:
            if isinstance(exc, RuleDenied):
                record_decision(principal, audit_resource, 'development.quota_denied', False, exc.reason, effective_tags(audit_resource))
            error = '; '.join(exc.messages) if isinstance(exc, ValidationError) else ('A concurrent change requires reloading.' if isinstance(exc, RuleConflict) else 'Central AI rules rejected the operation: ' + exc.reason)
        except PermissionDenied:
            record_decision(principal, audit_resource, 'development.operation_denied', False, 'live_resource_permission_denied', effective_tags(audit_resource))
            raise
    runs = []
    for row in DevelopmentRun.objects.filter(object_revision__development_object=obj).order_by('-created')[:50]:
        try:
            authorize_run(row, principal)
            runs.append({'run': row, 'result': json.dumps(row.result, indent=2), 'lineage': json.dumps(row.lineage, indent=2)})
        except PermissionDenied:
            continue
    return render(request, 'platform_services/development_object.html', {'organization': organization, 'object': obj,
        'versions': versions, 'revision_form': revision_form, 'run_form': run_form, 'share_form': share_form, 'error': error,
        'runs': runs, 'recipe': json.dumps(selected.spec, indent=2)})


@organization_admin
@never_cache
def export(request, organization, pk):
    if request.method != 'GET':
        raise PermissionDenied('Use a read-only export.')
    revision = get_object_or_404(DevelopmentRevision, pk=pk, development_object__organization=organization)
    result = JsonResponse(export_bundle(revision, request_principal(request)))
    result['Cache-Control'] = 'no-store, private'
    result['Content-Disposition'] = 'attachment; filename="development-bundle.json"'
    return result
