from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404

from identity.access import decision, effective_tags, record_decision, require_access
from .api import body, response, workload_api
from .models import Pipeline, TrainingRun, TrainingScenario, Workflow
from .training import add_feedback, authorize_scenario, cancel, enqueue


def scenario_document(scenario):
    return {'id': str(scenario.pk), 'slug': scenario.slug, 'name': scenario.name, 'goal': scenario.goal,
        'plan': scenario.plan, 'pipeline': scenario.pipeline.slug, 'workflow': scenario.workflow.slug if scenario.workflow_id else None,
        'runner_binding_id': str(scenario.runner_binding_id), 'revision': scenario.revision, 'active': scenario.active}


@workload_api(['training.read'])
def scenarios(request):
    rows = []
    for item in TrainingScenario.objects.filter(organization=request.organization, active=True).select_related('pipeline', 'workflow'):
        if decision(request.access_principal, item)[0]:
            rows.append(scenario_document(item))
        if len(rows) == 100:
            break
    return response({'organization_id': str(request.organization.pk), 'scenarios': rows})


@workload_api(['training.manage'], methods=('POST',))
def create_scenario(request):
    principal = request.access_principal
    if not principal.agent_binding:
        raise PermissionDenied('AI scenario creation uses the designing agent account. Administrators can also create scenarios in the panel.')
    values = body(request)
    if set(values) - {'slug', 'name', 'goal', 'plan', 'pipeline', 'workflow'}:
        raise ValidationError('Scenario planning cannot change accounts, credentials, environments, or destination classes.')
    pipeline = get_object_or_404(Pipeline, organization=request.organization, slug=values.get('pipeline'), active=True)
    workflow = get_object_or_404(Workflow, organization=request.organization, slug=values.get('workflow'), active=True) if values.get('workflow') else None
    require_access(principal, pipeline, 'training.scenario_create')
    if workflow:
        require_access(principal, workflow, 'training.scenario_create')
    item = TrainingScenario(organization=request.organization, runner_binding=principal.agent_binding,
        pipeline=pipeline, workflow=workflow, slug=values.get('slug'), name=values.get('name'),
        goal=values.get('goal'), plan=values.get('plan'))
    item.full_clean()
    item.save()
    record_decision(principal, item, 'training.scenario_created', True, 'agent_authored', effective_tags(item))
    return response({'organization_id': str(request.organization.pk), 'scenario': scenario_document(item)}, 201)


@workload_api(['training.manage'], methods=('POST',))
def update_scenario(request, pk):
    item = get_object_or_404(TrainingScenario, pk=pk, organization=request.organization)
    authorize_scenario(request.access_principal, item)
    values = body(request)
    if set(values) - {'goal', 'plan', 'revision'} or type(values.get('revision')) is not int:
        raise ValidationError('Use the current revision and only goal/plan changes.')
    with transaction.atomic():
        item = TrainingScenario.objects.select_for_update().get(pk=item.pk)
        if item.revision != values['revision']:
            return response({'error': 'revision_conflict'}, 409)
        item.goal, item.plan = values.get('goal', item.goal), values.get('plan', item.plan)
        item.revision += 1
        item.full_clean()
        item.save()
    record_decision(request.access_principal, item, 'training.scenario_updated', True, 'agent_iteration', effective_tags(item))
    return response({'organization_id': str(request.organization.pk), 'scenario': scenario_document(item)})


@workload_api(['training.run'], methods=('POST',))
def run_scenario(request, pk):
    item = get_object_or_404(TrainingScenario, pk=pk, organization=request.organization)
    values = body(request)
    if set(values) != {'request_id'}:
        raise ValidationError('Use only a request ID; training scope is fixed by the approved scenario.')
    run, created = enqueue(item, values['request_id'], request.access_principal)
    return response({'organization_id': str(request.organization.pk), 'run_id': str(run.pk), 'status': run.status, 'created': created}, 202 if created else 200)


def run_document(run, principal, after=-1, limit=50):
    if type(after) is not int or after < -1 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError('Use a cursor of -1 or higher and a limit of 1–100.')
    require_access(principal, run, 'training.result_read')
    events = list(run.events.filter(index__gt=after).select_related('record', 'workflow_run')[:limit])
    rows = []
    for event in events:
        if decision(principal, event)[0]:
            rows.append({'index': event.index, 'case': event.case_name, 'input': event.input, 'status': event.status,
                'record_id': event.record_id, 'workflow_run_id': str(event.workflow_run_id) if event.workflow_run_id else None,
                'output': event.workflow_run.output if event.workflow_run_id else event.record.data if event.record_id else None,
                'synthetic': True, 'evaluation': event.evaluation})
    feedback = [{'id': item.pk, 'verdict': item.verdict, 'rationale': item.rationale, 'suggestions': item.suggestions,
        'principal_kind': item.principal_kind, 'principal_id': str(item.principal_id)} for item in run.feedback.all()
        if decision(principal, item)[0]]
    return {'organization_id': str(run.organization_id), 'run_id': str(run.pk), 'scenario_id': str(run.scenario_id),
        'revision': run.snapshot['revision'], 'generator_version': run.snapshot['generator_version'], 'seed': run.snapshot['plan']['seed'],
        'runner_agent_id': str(run.runner_binding.account_id), 'environment': run.runner_binding.environment,
        'task_id': str(run.runner_binding.task_id) if run.runner_binding.task_id else None,
        'status': run.status, 'result': run.result, 'error': run.error, 'events': rows, 'feedback': feedback,
        'next_cursor': events[-1].index if events else after}


@workload_api(['training.read'])
def run_status(request, pk):
    run = get_object_or_404(TrainingRun, pk=pk, organization=request.organization)
    try:
        after, limit = int(request.GET.get('after', -1)), int(request.GET.get('limit', 50))
    except ValueError:
        raise ValidationError('Use integer pagination.')
    return response(run_document(run, request.access_principal, after, limit))


@workload_api(['training.manage'], methods=('POST',))
def cancel_run(request, pk):
    run = get_object_or_404(TrainingRun, pk=pk, organization=request.organization)
    run = cancel(run, request.access_principal)
    return response({'organization_id': str(request.organization.pk), 'run_id': str(run.pk), 'status': run.status})


@workload_api(['training.evaluate'], methods=('POST',))
def feedback(request, pk):
    run = get_object_or_404(TrainingRun, pk=pk, organization=request.organization)
    values = body(request)
    if set(values) - {'request_id', 'verdict', 'rationale', 'suggestions'}:
        raise ValidationError('Feedback cannot replace reference evaluations or run outcomes.')
    item, created = add_feedback(run, request.access_principal, values.get('request_id'), values.get('verdict'), values.get('rationale'), values.get('suggestions', []))
    return response({'organization_id': str(request.organization.pk), 'run_id': str(run.pk), 'feedback_id': item.pk, 'created': created}, 201 if created else 200)
