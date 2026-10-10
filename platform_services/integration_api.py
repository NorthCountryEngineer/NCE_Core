"""Backend APIs for integration consumption and embedded application settings."""
import hashlib
from functools import wraps

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from oauth2_provider.models import get_access_token_model

from identity.access import Principal, decision, effective_tags, evidence_for, record_decision, require_access
from identity.views import token_claims
from .api import body, response, workload_api
from .integration_models import ApplicationIntegration
from .integrations import (IntegrationVersionConflict, authorize, backend_credential, configure_application, deny,
                           descriptor, require_management, settings_document)


def integration_api(methods=('GET',), *, namespace='integration'):
    def decorate(view):
        @workload_api(methods=methods)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            request.integration_source = request.access_principal
            raw = request.headers.get('X-Core-User-Token')
            if raw:
                credential = request.credential
                if not credential or len(raw) > 4096:
                    return response({'error': 'invalid_user_token'}, 401)
                token = get_access_token_model().objects.select_related('application__service', 'user').filter(
                    token_checksum=hashlib.sha256(raw.encode()).hexdigest()).first()
                purpose = request.access_principal.purpose
                claims = token_claims(raw, token.application, purpose) if token and token.application else None
                deployment = getattr(token.application, 'deployment', None) if token and token.application else None
                if (not claims or not claims.get('active') or 'authorization' not in token.scopes or
                        token.application.service_id != credential.installation.service_id or
                        credential.environment_id and (not deployment or deployment.environment_id != credential.environment_id) or
                        credential.deployment_id and (not deployment or deployment.pk != credential.deployment_id)):
                    return response({'error': 'invalid_user_token'}, 401)
                request.access_principal = Principal(user=token.user, evidence=evidence_for(raw), application=token.application,
                    purpose=purpose, request_id=request.integration_source.request_id)
            try:
                with transaction.atomic():
                    return view(request, *args, **kwargs)
            except IntegrationVersionConflict:
                return response({'error': 'version_conflict'}, 409)
            except ValidationError:
                detail = ('Review the documentation query, format, and approved scope.' if namespace == 'documentation'
                    else 'Review the integration schema, revision, and credential replacement controls.')
                return response({'error': 'invalid_request', 'detail': detail}, 400)
            except PermissionDenied as error:
                # The transaction rolled back, including checks' audit writes.
                # Preserve a final denial outside it, without request bodies or keys.
                resource = getattr(request, 'integration_audit_resource', request.organization)
                reason = getattr(error, 'integration_reason', 'consumer_permission_denied')
                for principal in [request.integration_source, request.access_principal] if request.access_principal is not request.integration_source else [request.access_principal]:
                    allowed, policy_reason, tags = decision(principal, resource)
                    record_decision(principal, resource, namespace + '.' + view.__name__ + '_denied', False,
                        reason if allowed else policy_reason, tags)
                return response({'error': 'insufficient_permission'}, 403)
        return wrapped
    return decorate


def bindings(request):
    source = request.integration_source
    installation = source.agent_binding.installation if source.agent_binding else source.credential.installation
    environment = source.agent_binding.environment if source.agent_binding else source.credential.environment.name if source.credential.environment_id else None
    return ApplicationIntegration.objects.select_related('definition', 'environment', 'connection').filter(
        organization=request.organization, installation=installation, environment__name=environment)


def resolved(request, binding):
    request.integration_audit_resource = binding
    binding = ApplicationIntegration.objects.select_for_update(of=('self',)).get(pk=binding.pk)
    if binding.mode == 'platform' and binding.connection_id:
        from .integration_models import IntegrationConnection
        IntegrationConnection.objects.select_for_update().get(pk=binding.connection_id)
    value = authorize(request.integration_source, binding)
    if request.access_principal is not request.integration_source:
        value = authorize(request.access_principal, binding)
    return value


@integration_api()
def catalog(request):
    rows = []
    for binding in bindings(request).order_by('definition__slug'):
        try:
            rows.append(descriptor(*resolved(request, binding)))
        except PermissionDenied:
            continue
        if len(rows) == 100:
            break
    return response({'organization_id': str(request.organization.pk), 'integrations': rows})


@integration_api()
def resolve(request, slug):
    binding = get_object_or_404(bindings(request), definition__slug=slug)
    return response(descriptor(*resolved(request, binding)))


@integration_api(('GET', 'POST'))
def configure(request, slug):
    binding = get_object_or_404(bindings(request), definition__slug=slug)
    request.integration_audit_resource = binding
    require_access(request.integration_source, binding, 'integration.configuration_context')
    # A workload alone, or an agent, cannot manage configuration or grants.
    require_management(request.access_principal, binding)
    if not binding.allow_application_configuration:
        raise PermissionDenied('Application administration is managed centrally.')
    if request.method == 'POST':
        value = configure_application(binding, request.access_principal, body(request))
    else:
        value = settings_document(binding)
    if binding.connection_id and not all(decision(principal, binding.connection)[0]
            for principal in [request.integration_source, request.access_principal]):
        # An unused shared connection can have stricter policy than this app's
        # own settings. Do not disclose even its credential status without access.
        value['platform_connection_available'] = False
        value['platform_credential_configured'] = False
    return response(value)


@integration_api(('POST',))
def credentials(request, slug):
    binding = get_object_or_404(bindings(request), definition__slug=slug)
    binding, connection = resolved(request, binding)
    if not request.credential or not binding.credential_access:
        deny(request.integration_source, binding, 'backend_credential_grant_required')
    values = body(request)
    if set(values) != {'revision', 'connection_revision'} or any(type(values[key]) is not int for key in values):
        raise ValidationError('Supply the resolved configuration revisions.')
    if values['revision'] != binding.revision or values['connection_revision'] != connection.revision:
        raise IntegrationVersionConflict
    return response({'organization_id': str(request.organization.pk), 'integration_id': str(binding.pk),
        'revision': binding.revision, 'connection_revision': connection.revision,
        'credential': backend_credential(request.integration_source, binding)})
