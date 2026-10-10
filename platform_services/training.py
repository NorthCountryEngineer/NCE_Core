"""Durable synthetic-data harness. AI actors plan and evaluate through scoped tools."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from identity.access import Principal, decision, effective_tags, record_decision, require_access
from identity.models import Membership, Organization
from identity.policy import authorization_for
from .agent_identity import bind_snapshot, binding_valid, require_capabilities, step_principal
from .agent_tools import possible_tools
from .data import IdempotencyConflict, publish, transform
from .models import Dataset, Installation, TrainingEvent, TrainingFeedback, TrainingRun, WorkloadCredential, WorkflowRun
from .services import CAPABILITY_PACKAGE, credential_allows, feature_enabled
from .training_plan import evaluate, generate, samples, validate_plan
from .validation import validate_schema, validate_transforms
from .workflows import enqueue as enqueue_workflow, execution_resources, required_capabilities, snapshot_agents


ACTIVE = ['queued', 'running', 'waiting']


def require_capability(principal, organization, capabilities):
    if principal.agent_binding:
        if principal.agent_binding.organization_id != organization.pk:
            raise PermissionDenied('Wrong organization.')
        require_capabilities(principal, capabilities)
        return
    if principal.credential:
        credential = WorkloadCredential.objects.select_related('installation__organization', 'installation__service', 'installation__package').get(pk=principal.credential.pk)
        if credential.installation.organization_id != organization.pk or not credential_allows(credential, capabilities):
            raise PermissionDenied('The application permission has been revoked.')
        require_access(principal, credential, 'training.application')
        return
    for capability in capabilities:
        package = CAPABILITY_PACKAGE[capability]
        installation = Installation.objects.select_related('service').filter(organization=organization, package__slug=package).first()
        if not installation or not feature_enabled(organization, package):
            raise PermissionDenied('A required core service is unavailable.')
        require_access(principal, installation, 'training.service')
        membership = Membership.objects.filter(organization=organization, user=principal.user, active=True).first()
        if not principal.user or not principal.user.is_active or (not principal.user.is_superuser and
                (not membership or capability.replace('.', '-') not in authorization_for(membership, installation.service, principal)['permissions'])):
            raise PermissionDenied('A training service role is required.')
        permission = installation.service.permissions.get(codename=capability.replace('.', '-'))
        require_access(principal, permission, 'training.permission')


def workflow_resources(organization, steps):
    resources = execution_resources(organization, steps)
    if any(possible_tools(step) - {'inspect', 'select', 'publish'} for step in steps):
        raise ValidationError('Training workflows use inspect, select, publish, or rules over those tools. External side effects are excluded.')
    for resource in resources:
        if isinstance(resource, Dataset) and (resource.data_class != 'synthetic' or not resource.active):
            raise ValidationError('Every training workflow publication target must be an active synthetic dataset.')
    return resources


def scenario_resources(scenario, steps):
    return [scenario, scenario.runner_binding, scenario.pipeline, *([scenario.workflow] if scenario.workflow_id else []),
        *workflow_resources(scenario.organization, steps)]


def required(steps, has_workflow):
    return sorted({'training.run', 'data.write', *(required_capabilities('workflow', steps) if has_workflow else [])})


def runner_principal(scenario, purpose=''):
    binding = scenario.runner_binding
    if not binding_valid(binding) or binding.environment == 'production':
        raise PermissionDenied('Use an active non-production training agent approval.')
    return Principal(agent_binding=binding, purpose=purpose)


def authorize_scenario(principal, scenario, capability='training.manage'):
    require_capability(principal, scenario.organization, [capability])
    require_access(principal, scenario, 'training.scenario')
    if principal.agent_binding and principal.agent_binding.pk != scenario.runner_binding_id:
        raise PermissionDenied('An agent can manage and run only scenarios bound to its own approved account context.')
    if principal.credential and principal.credential.installation_id != scenario.runner_binding.installation_id:
        raise PermissionDenied('The scenario agent is not approved for this consuming application.')


def snapshot_scenario(scenario, principal):
    scenario.full_clean()
    if not scenario.active or not scenario.pipeline.active or not scenario.pipeline.dataset.active:
        raise PermissionDenied('The scenario or pipeline is disabled.')
    validate_schema(scenario.pipeline.dataset.schema)
    validate_transforms(scenario.pipeline.transforms)
    runner = runner_principal(scenario, principal.purpose)
    steps = []
    if scenario.workflow_id:
        scenario.workflow.full_clean()
        if not scenario.workflow.active:
            raise PermissionDenied('The training workflow is disabled.')
        binding = scenario.runner_binding
        steps = bind_snapshot(scenario.organization, snapshot_agents(scenario.organization, scenario.workflow.steps),
            binding.installation, binding.environment, binding.task, principal.purpose)
    require_capability(principal, scenario.organization, required(steps, bool(scenario.workflow_id)))
    require_capabilities(runner, required(steps, bool(scenario.workflow_id)))
    for resource in scenario_resources(scenario, steps):
        require_access(principal, resource, 'training.submit')
        require_access(runner, resource, 'training.agent_submit')
    return {'generator_version': 1, 'revision': scenario.revision, 'goal': scenario.goal, 'plan': deepcopy(scenario.plan),
        'pipeline_id': str(scenario.pipeline_id), 'dataset_id': str(scenario.pipeline.dataset_id),
        'transforms': deepcopy(scenario.pipeline.transforms), 'schema': deepcopy(scenario.pipeline.dataset.schema),
        'workflow_id': scenario.workflow_id, 'workflow_snapshot': steps}


@transaction.atomic
def enqueue(scenario, request_id, principal):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
        raise ValidationError('Use a request ID of 1–160 characters.')
    authorize_scenario(principal, scenario, 'training.run')
    snapshot = snapshot_scenario(scenario, principal)
    Organization.objects.select_for_update().get(pk=scenario.organization_id)
    existing = TrainingRun.objects.filter(organization=scenario.organization, request_id=request_id).first()
    if not existing and TrainingRun.objects.filter(organization=scenario.organization, status__in=ACTIVE).count() >= 8:
        raise ValidationError('This organization already has eight active training runs.')
    run, created = TrainingRun.objects.get_or_create(organization=scenario.organization, request_id=request_id, defaults={
        'scenario': scenario, 'runner_binding': scenario.runner_binding, 'snapshot': snapshot, 'actor': principal.user,
        'credential': principal.credential, 'trigger_binding': principal.agent_binding, 'trigger_credential': principal.agent_credential,
        'purpose_code': principal.purpose, 'authentication_evidence': principal.evidence})
    if (run.scenario_id != scenario.pk or run.snapshot != snapshot or run.actor_id != (principal.user.pk if principal.user else None) or
            run.credential_id != (principal.credential.pk if principal.credential else None) or
            run.trigger_binding_id != (principal.agent_binding.pk if principal.agent_binding else None) or
            run.trigger_credential_id != (principal.agent_credential.pk if principal.agent_credential else None) or run.purpose_code != principal.purpose):
        raise IdempotencyConflict('Request ID already belongs to a different training submission.')
    if created:
        run.permission_tags.set(scenario.permission_tags.all())
        record_decision(principal, run, 'training.enqueued', True, 'approved_simulation', effective_tags(run))
    return run, created


def trigger_principal(run):
    return Principal(user=run.actor, evidence=run.authentication_evidence, credential=run.credential,
        agent_binding=run.trigger_binding, agent_credential=run.trigger_credential, purpose=run.purpose_code)


def run_authorized(run):
    run = TrainingRun.objects.select_related('scenario__pipeline__dataset', 'scenario__workflow', 'scenario__runner_binding__task',
        'runner_binding__task', 'actor', 'credential', 'trigger_binding', 'trigger_credential').get(pk=run.pk)
    if run.status not in ACTIVE or not feature_enabled(run.organization, 'core-training'):
        raise PermissionDenied('Training is cancelled, completed, or disabled.')
    scenario = run.scenario
    if (not scenario.active or not scenario.pipeline.active or not scenario.pipeline.dataset.active or
            scenario.pipeline.dataset.data_class != 'synthetic' or scenario.runner_binding_id != run.runner_binding_id or
            str(scenario.pipeline_id) != run.snapshot['pipeline_id'] or str(scenario.pipeline.dataset_id) != run.snapshot['dataset_id'] or
            scenario.workflow_id != run.snapshot['workflow_id'] or scenario.workflow_id and not scenario.workflow.active):
        raise PermissionDenied('Training routing or destination is no longer available.')
    validate_plan(run.snapshot['plan'])
    validate_schema(run.snapshot['schema'])
    validate_schema(scenario.pipeline.dataset.schema)
    validate_transforms(run.snapshot['transforms'])
    runner = runner_principal(scenario, run.purpose_code)
    caller = trigger_principal(run)
    capabilities = required(run.snapshot['workflow_snapshot'], bool(scenario.workflow_id))
    require_capability(caller, run.organization, capabilities)
    require_capabilities(runner, capabilities)
    for resource in [run, *scenario_resources(scenario, run.snapshot['workflow_snapshot'])]:
        require_access(caller, resource, 'training.execute')
        require_access(runner, resource, 'training.agent_execute')
    binding = scenario.runner_binding
    context = SimpleNamespace(organization=run.organization, consumer_id=binding.installation_id, environment=binding.environment,
        task_id=binding.task_id, purpose_code=run.purpose_code)
    for step in run.snapshot['workflow_snapshot']:
        step_principal(context, step)
    return runner


def event_output(event):
    return event.workflow_run.output if event.workflow_run_id else event.record.data if event.record_id else None


def summary(run):
    cases = {}
    events = list(run.events.all())
    for event in events:
        group = cases.setdefault(event.case_name, {'total': 0, 'passed': 0, 'ingested': 0, 'rejected': 0})
        group['total'] += 1
        group['passed'] += bool(event.evaluation.get('passed'))
        group['ingested'] += bool(event.record_id)
        group['rejected'] += event.status == 'rejected'
    total, passed = len(events), sum(bool(event.evaluation.get('passed')) for event in events)
    return {'total': total, 'passed': passed, 'failed': total - passed, 'pass_rate': passed / total if total else 0,
        'all_passed': bool(total and total == len(samples(run.snapshot['plan'])) and total == passed), 'cases': cases}


def process_one():
    now, owner = timezone.now(), uuid.uuid4()
    eligible = Q(lease_until__isnull=True) | Q(lease_until__lte=now)
    candidate = TrainingRun.objects.filter(status__in=ACTIVE).filter(eligible).order_by('updated').first()
    if not candidate:
        return False
    if not TrainingRun.objects.filter(pk=candidate.pk, status__in=ACTIVE).filter(eligible).update(
            lease_owner=owner, lease_until=now + timedelta(seconds=60), status='running'):
        return True
    try:
        with transaction.atomic():
            # Lock the run only: optional task joins cannot be locked on PostgreSQL.
            run = TrainingRun.objects.select_for_update(of=('self',)).select_related('scenario__pipeline__dataset', 'runner_binding__task').get(pk=candidate.pk)
            if run.lease_owner != owner:
                return True
            principal = run_authorized(run)
            if run.next_index >= len(samples(run.snapshot['plan'])):
                run.result, run.status, run.completed = summary(run), 'completed', timezone.now()
                record_decision(principal, run, 'training.completed', True, 'evaluation_recorded', effective_tags(run))
            else:
                case, data = generate(run.snapshot['plan'], run.next_index)
                event, _ = TrainingEvent.objects.get_or_create(run=run, index=run.next_index,
                    defaults={'case_name': case['name'], 'input': data})
                if event.status == 'pending':
                    transformed = transform(data, run.snapshot['transforms'])
                    try:
                        Draft202012Validator(run.snapshot['schema']).validate(transformed)
                        # Validate current schema independently; a later schema edit cannot bypass policy.
                        Draft202012Validator(run.scenario.pipeline.dataset.schema).validate(transformed)
                    except SchemaError:
                        event.status = 'rejected'
                        event.evaluation = evaluate(case, data, None, False)
                    else:
                        event.record, _ = publish(run.scenario.pipeline.dataset, f'training:{run.pk}:{event.index}', transformed, training_run_id=run.pk)
                        event.status = 'ingested'
                        if run.scenario.workflow_id:
                            binding = run.runner_binding
                            event.workflow_run, _ = enqueue_workflow(run.organization, f'training:{run.pk}:{event.index}',
                                run.scenario.workflow.name, 'workflow', run.snapshot['workflow_snapshot'], event.record.data,
                                principal=principal, resource=run.scenario.workflow, environment=binding.environment,
                                task=binding.task, training_run=run)
                            event.status = 'waiting'
                        else:
                            event.evaluation = evaluate(case, data, event.record.data, True)
                    record_decision(principal, event, 'training.event_generated', True, 'synthetic_only', effective_tags(event))
                if event.status == 'waiting':
                    event.workflow_run.refresh_from_db()
                    if event.workflow_run.status in ['succeeded', 'failed']:
                        event.evaluation = evaluate(case, data, event.workflow_run.output, True, event.workflow_run.status == 'succeeded')
                        event.status = 'evaluated'
                event.save()
                if event.status != 'waiting':
                    run.next_index += 1
                run.status = 'waiting' if event.status == 'waiting' else 'running'
            run.lease_owner = None
            run.lease_until = timezone.now() + timedelta(seconds=1) if run.status == 'waiting' and event.workflow_run.status == 'running' else None
            run.save()
    except Exception as error:
        message = 'Training authorization revoked.' if isinstance(error, PermissionDenied) else 'Training configuration or execution failed.'
        TrainingRun.objects.filter(pk=candidate.pk, lease_owner=owner).update(status='failed', error=message,
            lease_owner=None, lease_until=None, completed=timezone.now(), updated=timezone.now())
        record_decision(trigger_principal(candidate), candidate, 'training.failed', False,
            'authorization_revoked' if isinstance(error, PermissionDenied) else 'execution_failed', effective_tags(candidate))
    return True


@transaction.atomic
def cancel(run, principal):
    run = TrainingRun.objects.select_for_update().get(pk=run.pk)
    require_capability(principal, run.organization, ['training.manage'])
    require_access(principal, run, 'training.cancel')
    if run.status in ACTIVE:
        run.status, run.completed, run.lease_owner, run.lease_until = 'cancelled', timezone.now(), None, None
        run.save()
        run.workflow_runs.filter(status__in=['queued', 'running']).update(status='failed', error='Training cancelled.',
            lease_owner=None, lease_until=None, completed=timezone.now())
        record_decision(principal, run, 'training.cancelled', True, 'explicit_cancel', effective_tags(run))
    return run


@transaction.atomic
def add_feedback(run, principal, request_id, verdict, rationale, suggestions):
    require_capability(principal, run.organization, ['training.evaluate'])
    require_access(principal, run, 'training.evaluate')
    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 160 or verdict not in ['pass', 'fail', 'review'] or
            not isinstance(rationale, str) or not 1 <= len(rationale) <= 4000 or not isinstance(suggestions, list) or
            len(suggestions) > 20 or any(not isinstance(value, str) or len(value) > 500 for value in suggestions)):
        raise ValidationError('Use a verdict, rationale, and at most 20 short suggestions.')
    TrainingRun.objects.select_for_update().get(pk=run.pk)
    principal_id = principal.agent_binding.account_id if principal.agent_binding else principal.credential.pk if principal.credential else principal.user.pk
    kind = 'agent' if principal.agent_binding else 'workload' if principal.credential else 'user'
    item = TrainingFeedback.objects.filter(run=run, principal_kind=kind, principal_id=principal_id, request_id=request_id).first()
    if item:
        if (item.verdict, item.rationale, item.suggestions) != (verdict, rationale, suggestions):
            raise IdempotencyConflict('Feedback request ID already used.')
        return item, False
    if run.feedback.count() >= 100:
        raise ValidationError('The run already has 100 evaluation feedback entries.')
    item = TrainingFeedback.objects.create(run=run, request_id=request_id, verdict=verdict, rationale=rationale, suggestions=suggestions,
        principal_kind=kind, principal_id=principal_id)
    record_decision(principal, item, 'training.feedback_added', True, 'attributed_evaluation', effective_tags(item))
    return item, True
