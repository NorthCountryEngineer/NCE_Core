"""Central, live authorization for non-human agent accounts. No human impersonation."""
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from identity.access import Principal, decision, effective_tags, record_decision, require_access
from identity.models import ServicePermission
from .models import AgentAccount, AgentBinding, AgentCredential, AgentDefinition, AgentTask, Installation, WorkloadCredential
from .services import feature_enabled


def binding_valid(binding):
    # Reload all controls: long-running tasks must observe administrative revocation.
    binding = AgentBinding.objects.select_related('account__organization', 'account__listing',
        'installation__service', 'installation__package', 'task').filter(pk=binding.pk).first()
    if not binding or not (binding.active and binding.account.active and binding.organization.active and
            binding.installation.enabled and binding.installation.service.active and binding.installation.package.active and
            binding.installation.organization_id == binding.organization_id and
            binding.installation.service.organization_id == binding.organization_id and
            feature_enabled(binding.organization, 'core-agents')):
        return False
    if binding.account.listing_id and not binding.account.listing.active:
        return False
    if not AgentDefinition.objects.filter(account_id=binding.account_id, organization_id=binding.organization_id, active=True).exists():
        return False
    if binding.environment not in {'sandbox', 'development', 'staging', 'uat', 'production'}:
        return False
    if binding.organization.environments.exists() and not binding.organization.environments.filter(name=binding.environment, active=True).exists():
        return False
    if binding.environment == 'uat' and not binding.task_id:
        return False
    if binding.task_id and (not binding.task.active or binding.task.expires_at <= timezone.now() or
            binding.task.organization_id != binding.organization_id or binding.task.installation_id != binding.installation_id or
            binding.task.environment != binding.environment):
        return False
    return True


def credential_valid(credential, binding):
    return bool(credential and AgentCredential.objects.filter(pk=credential.pk, binding_id=binding.pk, active=True,
        expires_at__gt=timezone.now()).exists())


def principal_for(raw, purpose='', request_id=None):
    credential = AgentCredential.objects.select_related('binding__account__organization').filter(
        token_hash=WorkloadCredential.digest(raw), active=True, expires_at__gt=timezone.now()).first() if raw else None
    if not credential or not binding_valid(credential.binding):
        return None
    return Principal(agent_binding=credential.binding, agent_credential=credential, purpose=purpose, request_id=request_id)


def authorization(principal, service):
    binding = principal.agent_binding
    if (not binding or service.organization_id != binding.organization_id or not service.active or
            not binding_valid(binding) or
            principal.agent_credential and not credential_valid(principal.agent_credential, binding)):
        return {'roles': [], 'permissions': []}
    if not decision(principal, service)[0] or not decision(principal, binding)[0]:
        return {'roles': [], 'permissions': []}
    roles = binding.roles.filter(organization_id=service.organization_id)
    roles = roles.filter(pk__in=[role.pk for role in roles if decision(principal, role)[0]])
    permissions = []
    for permission in ServicePermission.objects.filter(service=service, roles__in=roles).distinct():
        # The reserved organization-administration capability is human-only.
        if permission.codename != 'admin' and decision(principal, permission)[0]:
            permissions.append(permission.codename)
    return {'roles': sorted(set(roles.values_list('slug', flat=True))), 'permissions': sorted(permissions)}


def require_permission(principal, service, codename):
    allowed = codename in authorization(principal, service)['permissions']
    record_decision(principal, service, 'agent.permission', allowed,
        'role_granted' if allowed else 'agent_role_required', effective_tags(service))
    if not allowed:
        raise PermissionDenied('The agent has no live role grant for this service operation.')
    permission = ServicePermission.objects.get(service=service, codename=codename)
    require_access(principal, permission, 'agent.permission_tags')


def require_capabilities(principal, capabilities):
    from .services import CAPABILITY_PACKAGE
    for name in capabilities:
        package = CAPABILITY_PACKAGE.get(name)
        installation = Installation.objects.select_related('service').filter(
            organization_id=principal.agent_binding.organization_id, package__slug=package).first()
        if not installation or not feature_enabled(installation.organization, package):
            raise PermissionDenied('The required core service is unavailable.')
        require_access(principal, installation, 'agent.core_service')
        require_permission(principal, installation.service, name.replace('.', '-'))


@transaction.atomic
def issue_credential(binding, principal, lifetime=900):
    if type(lifetime) is not int or not 60 <= lifetime <= 3600:
        raise ValidationError('Agent credentials last 60–3600 seconds.')
    if not binding_valid(binding):
        raise ValidationError('This agent approval is unavailable or expired.')
    require_access(principal, binding, 'agent.credential_issue')
    expires = timezone.now() + timedelta(seconds=lifetime)
    if binding.task_id:
        expires = min(expires, AgentTask.objects.get(pk=binding.task_id).expires_at)
    raw = 'ca_' + secrets.token_urlsafe(48)
    credential = AgentCredential.objects.create(binding=binding, token_hash=WorkloadCredential.digest(raw), expires_at=expires)
    record_decision(principal, binding, 'agent.credential_issued', True, 'explicit_issue', effective_tags(binding))
    return credential, raw


@transaction.atomic
def install_listing(organization, listing, slug, principal):
    if not listing.active or not feature_enabled(organization, 'core-agents'):
        raise ValidationError('Core Agents and the selected marketplace listing must be enabled.')
    account = AgentAccount.objects.create(organization=organization, name=listing.name, listing=listing, listing_version=listing.version)
    agent = AgentDefinition(organization=organization, account=account, slug=slug, name=listing.name,
        kind=listing.kind, configuration=listing.configuration)
    agent.full_clean()
    agent.save()
    record_decision(principal, account, 'agent.marketplace_installed', True, 'no_permissions_granted', [])
    return agent


def bind_snapshot(organization, snapshot, consumer, environment, task, purpose):
    """Pin each account and approval in the durable snapshot before accepting a run."""
    from copy import deepcopy
    result = deepcopy(snapshot)
    if consumer.organization_id != organization.pk or environment not in {'sandbox', 'development', 'staging', 'uat', 'production'}:
        raise ValidationError('Use a consuming application and environment from this organization.')
    for step in result:
        agent = AgentDefinition.objects.get(organization=organization, slug=step['slug'], active=True)
        binding = AgentBinding.objects.filter(account=agent.account, installation=consumer, environment=environment, task=task).first()
        if not binding or not binding_valid(binding):
            raise PermissionDenied('An administrator must approve each agent for this application, environment, and task.')
        principal = Principal(agent_binding=binding, purpose=purpose)
        require_access(principal, agent, 'agent.task_accept')
        # Check every possible branch before queueing; recheck before execution too.
        from .workflows import execution_resources, required_capabilities
        require_capabilities(principal, required_capabilities('agent', [step]))
        for resource in execution_resources(organization, [step]):
            require_access(principal, resource, 'agent.task_accept')
            if resource._meta.label_lower == 'core_chat.conversation':
                from core_chat.services import authorize
                authorize(principal, resource, 'send', ai=True)
        step['account_id'], step['binding_id'] = str(agent.account_id), str(binding.pk)
    return result


def step_principal(run, step):
    binding = AgentBinding.objects.filter(pk=step.get('binding_id'), account_id=step.get('account_id'),
        installation_id=run.consumer_id, environment=run.environment, task_id=run.task_id).first()
    if (not binding or not binding_valid(binding) or not AgentDefinition.objects.filter(
            organization=run.organization, slug=step['slug'], account_id=binding.account_id, active=True).exists()):
        raise PermissionDenied('The agent account or placement has been revoked.')
    principal = Principal(agent_binding=binding, purpose=run.purpose_code)
    require_access(principal, binding, 'agent.execution')
    from .workflows import required_capabilities, execution_resources
    require_capabilities(principal, required_capabilities('agent', [step]))
    for resource in execution_resources(run.organization, [step]):
        require_access(principal, resource, 'agent.execution')
        if resource._meta.label_lower == 'core_chat.conversation':
            from core_chat.services import authorize
            authorize(principal, resource, 'send', ai=True)
    return principal
