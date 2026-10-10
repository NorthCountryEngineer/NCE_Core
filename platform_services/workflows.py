import uuid
from copy import deepcopy
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from identity.models import Membership
from .data import publish
from .agent_tools import choose_tool, possible_tools
from .models import AgentDefinition, Dataset, Installation, RunStep, WorkflowRun, WorkloadCredential
from .services import credential_allows, feature_enabled
from identity.access import Principal, decision, effective_tags, require_access


def snapshot_agents(organization, slugs):
    agents = {a.slug: a for a in AgentDefinition.objects.filter(organization=organization, active=True, slug__in=slugs)}
    if set(slugs) - agents.keys():
        raise ValidationError("A required agent is unavailable.")
    snapshot = []
    for slug in slugs:
        agent = agents[slug]
        agent.full_clean()
        snapshot.append({"slug": slug, "kind": agent.kind, "configuration": deepcopy(agent.configuration)})
    return snapshot


def required_capabilities(source, snapshot):
    capabilities = {"agents.run" if source == "agent" else "workflows.run"}
    if any("publish" in possible_tools(step) for step in snapshot):
        capabilities.add("data.write")
    if any('chat_send' in possible_tools(step) for step in snapshot):
        capabilities.update(['chat.send', 'chat.ai'])
    return sorted(capabilities)


def execution_resources(organization, snapshot):
    resources = list(AgentDefinition.objects.filter(organization=organization, slug__in=[step["slug"] for step in snapshot]))
    for step in snapshot:
        tools = [step] if step["kind"] != "rules" else [
            {"kind": tool["tool"], "configuration": tool.get("configuration", {})}
            for tool in [*step["configuration"]["rules"], step["configuration"]["default"]]]
        for tool in tools:
            if tool["kind"] == "publish":
                resources.append(Dataset.objects.get(organization=organization, slug=tool["configuration"]["dataset"], active=True))
            if tool['kind'] == 'chat_send':
                from core_chat.models import Conversation
                resources.append(Conversation.objects.get(organization=organization, pk=tool['configuration']['conversation_id'], active=True))
    return resources


def enqueue(organization, request_id, name, source, snapshot, data, credential=None, actor=None, principal=None, resource=None,
            environment='development', task=None, training_run=None):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160 or not isinstance(data, dict):
        raise ValidationError("A request ID and JSON object input are required.")
    capabilities = required_capabilities(source, snapshot)
    if credential and not credential_allows(credential, capabilities):
        raise PermissionDenied("A required tool capability has not been granted.")
    principal = principal or Principal(user=actor, credential=credential)
    if credential and credential.environment_id and environment != credential.environment.name:
        raise PermissionDenied('A workload cannot change its deployment environment.')
    if principal.agent_binding:
        from .agent_identity import require_capabilities
        require_capabilities(principal, capabilities)
        consumer = principal.agent_binding.installation
        if environment != principal.agent_binding.environment or (task.pk if task else None) != principal.agent_binding.task_id:
            raise PermissionDenied('Agent task context cannot be changed by a caller.')
    else:
        consumer = credential.installation if credential else task.installation if task else Installation.objects.get(
            organization=organization, package__slug='core-workflows' if source == 'workflow' else 'core-agents')
    require_access(principal, consumer, 'workflow.consumer')
    if task:
        require_access(principal, task, 'workflow.task')
    from .agent_identity import bind_snapshot
    snapshot = bind_snapshot(organization, snapshot, consumer, environment, task, principal.purpose)
    resources = execution_resources(organization, snapshot) + ([resource] if resource else [])
    tags = {}
    for item in resources:
        tags.update({tag.pk: tag for tag in require_access(principal, item, "workflow.enqueue")})
        if item._meta.label_lower == 'core_chat.conversation':
            from core_chat.services import authorize
            authorize(principal, item, 'send', ai=True)
    try:
        with transaction.atomic():
            run, created = WorkflowRun.objects.get_or_create(organization=organization, request_id=request_id,
                defaults={"definition_name": name, "source": source, "snapshot": snapshot, "input": data,
                          "credential": credential, "actor": actor, "required_capabilities": capabilities,
                          'consumer': consumer, 'environment': environment, 'task': task,
                          'trigger_binding': principal.agent_binding, 'trigger_credential': principal.agent_credential,
                          'training_run': training_run,
                          "source_workflow": resource if source == "workflow" else None})
            if created:
                run.permission_tags.set(tags)
                run.purpose_code = principal.purpose
                run.authentication_evidence = principal.evidence
                run.save(update_fields=["purpose_code", "authentication_evidence"])
    except IntegrityError:
        run, created = WorkflowRun.objects.get(organization=organization, request_id=request_id), False
    if (run.input != data or run.snapshot != snapshot or run.credential_id != (credential.pk if credential else None)
            or run.actor_id != (actor.pk if actor else None) or run.source != source or run.purpose_code != principal.purpose
            or run.environment != environment or run.task_id != (task.pk if task else None)
            or run.trigger_binding_id != (principal.agent_binding.pk if principal.agent_binding else None)
            or run.training_run_id != (training_run.pk if training_run else None)
            or run.trigger_credential_id != (principal.agent_credential.pk if principal.agent_credential else None)):
        from .data import IdempotencyConflict
        raise IdempotencyConflict("The request ID has already been used for another run.")
    return run, created


def run_allowed(run):
    organization = run.organization
    if run.training_run_id:
        from .training import run_authorized
        try:
            run_authorized(run.training_run)
        except (PermissionDenied, ValidationError):
            return False
    if not feature_enabled(organization, "core-agents"):
        return False
    if run.source == "workflow" and not feature_enabled(organization, "core-workflows"):
        return False
    if run.trigger_binding_id:
        from .agent_identity import require_capabilities
        principal = Principal(agent_binding=run.trigger_binding, agent_credential=run.trigger_credential, purpose=run.purpose_code)
        try:
            require_capabilities(principal, run.required_capabilities)
        except PermissionDenied:
            return False
        allowed = True
    elif run.credential_id:
        credential = WorkloadCredential.objects.select_related("installation__organization", "installation__service", "installation__package").get(pk=run.credential_id)
        principal = Principal(credential=credential, purpose=run.purpose_code)
        allowed = credential_allows(credential, run.required_capabilities)
    else:
        from identity.models import User
        principal = Principal(user=run.actor, evidence=run.authentication_evidence, purpose=run.purpose_code)
        allowed = bool(run.actor_id and User.objects.filter(pk=run.actor_id, is_active=True).exists() and (
            User.objects.filter(pk=run.actor_id, is_superuser=True).exists() or Membership.objects.filter(
            organization=organization, user_id=run.actor_id, user__is_active=True, active=True, kind="administrator").exists()))
    if not allowed:
        return False
    try:
        from .agent_identity import step_principal
        for step in run.snapshot:
            step_principal(run, step)
        for resource in [run, *execution_resources(organization, run.snapshot)]:
            require_access(principal, resource, "workflow.execute")
            if resource._meta.label_lower == 'core_chat.conversation':
                from core_chat.services import authorize
                authorize(principal, resource, 'send', ai=True)
    except PermissionDenied:
        return False
    return True


def execute_tool(organization, step, data, event_id, principal=None, agent_name='', training_run_id=None):
    if not isinstance(data, dict):
        raise ValidationError("Agents require a JSON object.")
    kind, configuration = step["kind"], step["configuration"]
    if kind == "inspect":
        return {"field_count": len(data), "fields": sorted(data), "types": {key: type(value).__name__ for key, value in data.items()}}
    if kind == "select":
        return {key: data[key] for key in configuration["fields"] if key in data}
    if kind == "publish":
        if not feature_enabled(organization, "core-data"):
            raise PermissionDenied("Core Data is unavailable.")
        dataset = Dataset.objects.get(organization=organization, slug=configuration["dataset"], active=True)
        publish(dataset, event_id, data, training_run_id=training_run_id)
        return data
    if kind == 'chat_send':
        from core_chat.models import Conversation
        from core_chat.services import send_message
        conversation = Conversation.objects.get(organization=organization, pk=configuration['conversation_id'], active=True)
        result = send_message(principal, conversation, event_id, data.get(configuration['text_field']), ai=True, agent_name=agent_name)
        return {**data, 'chat_message_id': result['message']['id']}
    raise ValidationError("Unsupported agent tool.")


def process_one():
    now, owner = timezone.now(), uuid.uuid4()
    candidates = WorkflowRun.objects.filter(Q(status="queued") | Q(status="running", lease_until__lt=now)).order_by("created")
    candidate = candidates.first()
    if candidate is None:
        return False
    claimed = WorkflowRun.objects.filter(pk=candidate.pk).filter(
        Q(status="queued") | Q(status="running", lease_until__lt=now),
    ).update(status="running", lease_owner=owner, lease_until=now + timedelta(seconds=60), attempts=F("attempts") + 1)
    if not claimed:
        return True
    run = WorkflowRun.objects.select_related("organization", "actor").get(pk=candidate.pk)
    try:
        if run.attempts > 3:
            raise ValidationError("Retry limit exceeded.")
        output = run.input
        for index, step in enumerate(run.snapshot):
            with transaction.atomic():
                locked = WorkflowRun.objects.select_for_update().get(pk=run.pk)
                if locked.lease_owner != owner:
                    return True
                if not run_allowed(run):
                    raise PermissionDenied("Authorization was revoked before execution.")
                previous = RunStep.objects.filter(run=run, index=index).first()
                if previous:
                    output = previous.output
                else:
                    chosen = choose_tool(step, output)
                    from .agent_identity import step_principal
                    principal = step_principal(run, step)
                    output = execute_tool(run.organization, chosen, output, f"run:{run.pk}:step:{index}", principal=principal, agent_name=step['slug'],
                        training_run_id=run.training_run_id)
                    RunStep.objects.create(run=run, index=index, agent_slug=step["slug"], tool=chosen["kind"], output=output,
                        agent_account_id=principal.agent_binding.account_id)
                    from identity.access import record_decision
                    record_decision(principal, run, 'agent.step_completed', True, 'task_role_authorized', [])
                locked.lease_until = timezone.now() + timedelta(seconds=60)
                locked.save(update_fields=["lease_until"])
        WorkflowRun.objects.filter(pk=run.pk, lease_owner=owner).update(
            status="succeeded", output=output, completed=timezone.now(), lease_until=None)
    except Exception as error:
        # Never put submitted records, credentials, or tool exception contents in logs.
        message = "Authorization revoked." if isinstance(error, PermissionDenied) else "Tool execution failed; check agent and dataset configuration."
        if isinstance(error, PermissionDenied):
            from identity.access import record_decision
            principal = Principal(user=run.actor, evidence=run.authentication_evidence, credential=run.credential,
                                  purpose=run.purpose_code)
            record_decision(principal, run, "workflow.execution_denied", False, "authorization_revoked", effective_tags(run))
        WorkflowRun.objects.filter(pk=run.pk, lease_owner=owner).update(
            status="failed", error=message, completed=timezone.now(), lease_until=None)
    return True
