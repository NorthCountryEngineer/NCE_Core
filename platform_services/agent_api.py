from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404

from identity.access import decision, effective_tags, record_decision, require_access
from .api import body, response, workload_api
from .agent_identity import authorization, issue_credential, principal_for
from .models import AgentBinding


@workload_api(['agents.run'], methods=('POST',))
def exchange(request):
    # Only an approved consuming application's own integration credential can vend an agent token.
    if not request.credential:
        raise PermissionDenied('Agent accounts cannot mint credentials for other agents.')
    values = body(request)
    try:
        binding = get_object_or_404(AgentBinding, pk=values.get('binding_id'),
            account__organization=request.organization, installation=request.credential.installation)
    except (ValueError, ValidationError):
        raise ValidationError('Use an approved agent binding ID.')
    credential, raw = issue_credential(binding, request.access_principal, values.get('expires_in', 900))
    return response({'access_token': raw, 'token_type': 'Bearer', 'expires_in': max(0, int(
        (credential.expires_at - credential.created).total_seconds())), 'agent_id': str(binding.account_id),
        'binding_id': str(binding.pk), 'environment': binding.environment,
        'task_id': str(binding.task_id) if binding.task_id else None}, 201)


@workload_api(methods=('POST',))
def introspect(request):
    if not request.credential:
        raise PermissionDenied('Only a registered service can introspect agent credentials.')
    values = body(request)
    raw = values.get('token')
    if not isinstance(raw, str) or not 1 <= len(raw) <= 256:
        raise ValidationError('Use an agent token.')
    principal = principal_for(raw, request.access_principal.purpose, request.access_principal.request_id)
    if not principal or principal.agent_binding.organization_id != request.organization.pk:
        return response({'active': False})
    binding = principal.agent_binding
    if request.credential.environment_id and binding.environment != request.credential.environment.name:
        return response({'active': False})
    task_id = str(binding.task_id) if binding.task_id else None
    service = request.credential.installation.service
    if values.get('environment') != binding.environment or values.get('task_id') != task_id:
        record_decision(principal, service, 'agent.introspected', False, 'task_context_mismatch', [])
        return response({'active': False})
    if not decision(principal, request.credential.installation)[0]:
        record_decision(principal, service, 'agent.introspected', False, 'target_service_policy', effective_tags(service))
        return response({'active': False})
    require_access(principal, request.credential.installation, 'agent.target_service')
    grants = authorization(principal, service)
    active = bool(grants['permissions'])
    if 'permission' in values:
        active = active and values['permission'] in grants['permissions']
    if values.get('resource_type') or values.get('resource_id'):
        from identity.models import ProtectedResource
        resource = ProtectedResource.objects.filter(service=service, active=True,
            resource_type=values.get('resource_type'), external_id=values.get('resource_id')).first()
        active = bool(active and resource and decision(principal, resource)[0])
        if resource:
            record_decision(principal, resource, 'agent.resource_authorization', active,
                'policy_satisfied' if active else 'resource_denied', effective_tags(resource))
    record_decision(principal, service, 'agent.introspected', active,
        'live_service_roles' if active else 'agent_role_required', effective_tags(service))
    if not active:
        return response({'active': False})
    from django.conf import settings
    return response({'active': True, 'iss': settings.ISSUER, 'sub': f'urn:core:agent:{binding.account_id}',
        'principal_kind': 'agent', 'agent_id': str(binding.account_id), 'organization_id': str(request.organization.pk),
        'aud': service.audience, 'consumer_service_id': str(binding.installation.service_id),
        'binding_id': str(binding.pk), 'environment': binding.environment, 'task_id': task_id,
        'exp': int(min(principal.agent_credential.expires_at, binding.task.expires_at).timestamp()) if binding.task_id
            else int(principal.agent_credential.expires_at.timestamp()), **grants})
