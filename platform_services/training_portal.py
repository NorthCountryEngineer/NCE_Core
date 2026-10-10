import secrets

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from identity.access import request_principal, require_access, visible_resources
from identity.views import organization_admin
from .data import IdempotencyConflict
from .models import AgentBinding, Pipeline, TrainingRun, TrainingScenario, Workflow
from .training import add_feedback, authorize_scenario, cancel, enqueue, require_capability
from .training_api import run_document


EXAMPLE_PLAN = {'seed': 42, 'cases': [{'name': 'nominal', 'count': 5,
    'fields': {'title': {'kind': 'sequence', 'prefix': 'Simulated report '}, 'actor': {'kind': 'email'}},
    'expected_ingest': 'accept', 'assertions': [{'field': 'title', 'operator': 'exists'}]}]}


@organization_admin
def index(request, organization):
    principal = request_principal(request)
    require_capability(principal, organization, ['training.read'])
    return render(request, 'platform_services/training.html', {'organization': organization,
        'scenarios': visible_resources(principal, organization.training_scenarios.all()),
        'runs': visible_resources(principal, organization.training_runs.order_by('-created')[:50])})


@organization_admin
def scenario(request, organization, pk=None):
    principal = request_principal(request)
    require_capability(principal, organization, ['training.manage'])
    item = get_object_or_404(TrainingScenario, organization=organization, pk=pk) if pk else TrainingScenario(organization=organization, plan=EXAMPLE_PLAN)
    if pk:
        authorize_scenario(principal, item)
    class ScenarioForm(forms.ModelForm):
        class Meta:
            model = TrainingScenario
            fields = ['slug', 'name', 'goal', 'pipeline', 'workflow', 'runner_binding', 'plan', 'active']
    form = ScenarioForm(request.POST or None, instance=item)
    for field, queryset in [('pipeline', Pipeline.objects.filter(organization=organization, active=True, dataset__data_class='synthetic', dataset__active=True)),
            ('workflow', Workflow.objects.filter(organization=organization, active=True)),
            ('runner_binding', AgentBinding.objects.filter(account__organization=organization, active=True).exclude(environment='production'))]:
        form.fields[field].queryset = queryset.filter(pk__in=[resource.pk for resource in visible_resources(principal, queryset)])
    if pk:
        for field in ['pipeline', 'workflow', 'runner_binding']:
            form.fields[field].disabled = True
    if request.method == 'POST' and form.is_valid():
        for resource in [form.cleaned_data['pipeline'], form.cleaned_data['runner_binding'], form.cleaned_data.get('workflow')]:
            if resource:
                require_access(principal, resource, 'training.scenario_manage')
        with transaction.atomic():
            updated = form.save(commit=False)
            if pk:
                # Read the revision under a lock so concurrent edits never reuse it.
                previous = TrainingScenario.objects.select_for_update().get(pk=pk)
                updated.revision = previous.revision + 1
            updated.save()
            from identity.access import effective_tags, record_decision
            record_decision(principal, updated, 'training.scenario_updated', True, 'administrator_reviewed', effective_tags(updated))
        return redirect('training', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'AI training scenario and synthetic pipeline'})


@organization_admin
@require_POST
def start(request, organization, pk):
    item = get_object_or_404(TrainingScenario, organization=organization, pk=pk)
    principal = request_principal(request)
    try:
        run, _ = enqueue(item, secrets.token_hex(24), principal)
    except (PermissionDenied, ValidationError, IdempotencyConflict):
        return render(request, 'platform_services/training_error.html', {'organization': organization,
            'message': 'Approve the non-production runner and workflow agents, service roles, synthetic targets, and UAT resource scope before running.'}, status=400)
    return redirect('training_run', slug=organization.slug, pk=run.pk)


@organization_admin
def detail(request, organization, pk):
    run = get_object_or_404(TrainingRun, organization=organization, pk=pk)
    principal = request_principal(request)
    require_capability(principal, organization, ['training.read'])
    class ReviewForm(forms.Form):
        request_id = forms.CharField(widget=forms.HiddenInput, initial=lambda: secrets.token_hex(24))
        verdict = forms.ChoiceField(choices=[('review', 'Needs review'), ('pass', 'Pass'), ('fail', 'Fail')])
        rationale = forms.CharField(widget=forms.Textarea, max_length=4000)
        suggestions = forms.JSONField(required=False, initial=[])
    form = ReviewForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            add_feedback(run, principal, form.cleaned_data['request_id'], form.cleaned_data['verdict'],
                form.cleaned_data['rationale'], form.cleaned_data['suggestions'] or [])
        except (ValidationError, IdempotencyConflict):
            form.add_error(None, 'Feedback needs a unique request ID and at most 20 short suggestions.')
        else:
            return redirect('training_run', slug=organization.slug, pk=pk)
    document = run_document(run, principal, limit=100)
    return render(request, 'platform_services/training_run.html', {'organization': organization, 'run': run, 'document': document, 'review_form': form})


@organization_admin
@require_POST
def stop(request, organization, pk):
    run = get_object_or_404(TrainingRun, organization=organization, pk=pk)
    cancel(run, request_principal(request))
    return redirect('training_run', slug=organization.slug, pk=pk)
