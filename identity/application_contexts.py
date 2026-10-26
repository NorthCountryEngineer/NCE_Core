"""Metadata discovery for explicitly configured, independently authorized app tenants."""
import json
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from platform_services.external_apps import application_deployment, external_available
from platform_services.models import Deployment

from .access import Principal, evidence_for, record_decision, require_access
from .models import User
from .policy import authorization_for, membership_for
from .policy_evaluation import policy_evaluation
from .views import authenticate_resource_client, machine_response, token_claims


def exact_registration(deployment):
    """A usable pair must still match its exact registered callback and origin."""
    if not external_available(deployment):
        return False
    browser = deployment.application
    callback = deployment.installation.manifest.get('callback_path', '/oauth/callback')
    return (browser.allowed_origins == deployment.endpoint and
            browser.redirect_uris == deployment.endpoint + callback)


@csrf_exempt
@require_POST
@policy_evaluation()
def application_contexts(request):
    resource = authenticate_resource_client(request)
    if not resource:
        return machine_response({'error': 'invalid_client'}, 401)
    try:
        if request.content_type != 'application/json' or len(request.body) > 16384:
            raise ValueError
        values = json.loads(request.body)
        if not isinstance(values, dict) or set(values) != {'token', 'deployment_ids'}:
            raise ValueError
        raw, candidates = values['token'], values['deployment_ids']
        if (not isinstance(raw, str) or not 1 <= len(raw) <= 4096 or
                not isinstance(candidates, list) or not 1 <= len(candidates) <= 20 or
                any(not isinstance(value, str) for value in candidates)):
            raise ValueError
        selected = [UUID(value) for value in candidates]
        if len(set(selected)) != len(selected):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        return machine_response({'error': 'invalid_request'}, 400)

    purpose = request.headers.get('X-Core-Purpose', '')
    request_id = getattr(request, 'security_request_id', None)
    claims = token_claims(raw, resource, purpose, request_id)
    if not claims:
        return machine_response({'error': 'invalid_user_token'}, 401)
    source = application_deployment(resource)
    if (not source or not exact_registration(source) or
            'workspace-use' not in claims['permissions']):
        return machine_response({'error': 'insufficient_permission'}, 403)

    user = User.objects.get(pk=claims['sub'])
    evidence = evidence_for(raw)
    source_principal = Principal(user=user, evidence=evidence, application=source.application,
                                 purpose=purpose, request_id=request_id)
    try:
        require_access(source_principal, source, 'application.context_source_access')
    except PermissionDenied:
        return machine_response({'error': 'insufficient_permission'}, 403)
    deployments = {deployment.pk: deployment for deployment in
        Deployment.objects.select_related('installation__organization', 'installation__service',
            'installation__package', 'environment', 'application', 'resource_application').filter(
                pk__in=selected, target='external', endpoint=source.endpoint)}
    contexts = []
    for selected_id in selected:
        deployment = deployments.get(selected_id)
        if (not deployment or not exact_registration(deployment) or
                deployment.application.redirect_uris != source.application.redirect_uris):
            continue
        membership = membership_for(user, deployment.installation.service)
        if not membership:
            continue
        principal = Principal(user=user, evidence=evidence, application=deployment.application,
                              purpose=purpose, request_id=request_id)
        try:
            require_access(principal, deployment, 'application.context_access')
            require_access(principal, deployment.application, 'application.context_client_access')
        except PermissionDenied:
            continue
        if 'workspace-use' not in authorization_for(membership, deployment.installation.service, principal)['permissions']:
            record_decision(principal, deployment, 'application.context_access', False,
                            'workspace_permission_required', [])
            continue
        contexts.append({'organization_id': str(deployment.installation.organization_id),
            'organization_name': deployment.installation.organization.name,
            'service_id': str(deployment.installation.service_id), 'deployment_id': str(deployment.pk),
            'browser_client_id': deployment.application.client_id,
            'environment': deployment.environment.name, 'public_origin': deployment.endpoint})
        record_decision(principal, deployment, 'application.context_discovered', True, 'live_workspace_access', [])
    return machine_response({'contexts': contexts})
