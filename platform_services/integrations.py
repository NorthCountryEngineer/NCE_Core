"""Explicit consumption grants; no cached configurations confer authorization."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from identity.access import effective_tags, record_decision, require_access, resource_organization, visible_resources
from identity.models import Membership, ServicePermission
from .integration_models import ApplicationIntegration
from .secrets import decrypt_bundle, encrypt_bundle
from .services import credential_allows, feature_enabled


class IntegrationVersionConflict(Exception):
    pass


def deny(principal, resource, reason):
    record_decision(principal, resource, 'integration.denied', False, reason, effective_tags(resource))
    error = PermissionDenied('The integration operation has no current approval.')
    error.integration_reason = reason
    raise error


def require_management(principal, resource):
    require_access(principal, resource, 'integration.manage')
    if not principal.user or not principal.user.is_active or not (principal.user.is_superuser or Membership.objects.filter(
            organization=resource_organization(resource), user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('A current organization administrator is required.')


@transaction.atomic
def publish_definition(definition, principal):
    require_management(principal, definition.organization)
    if not definition._state.adding:
        require_management(principal, definition)
    require_access(principal, definition.service, 'integration.publish')
    if definition.provider_id:
        require_access(principal, definition.provider, 'integration.provider')
    definition.full_clean()
    definition.save()
    permission, _ = ServicePermission.objects.get_or_create(service=definition.service, codename=definition.use_permission,
        defaults={'name': ('Use ' + definition.name)[:160]})
    record_decision(principal, definition, 'integration.published', True, 'administrator_approved', effective_tags(definition))
    return permission


def replace_credential(resource, credential=None, clear=False, previous_configuration=None):
    if credential == '':
        credential = None
    if type(clear) is not bool or credential is not None and (not isinstance(credential, str) or not 1 <= len(credential) <= 8192 or
            any(ord(char) < 32 for char in credential)):
        raise ValidationError('Use a bounded replacement credential or explicitly clear it.')
    if clear and credential:
        raise ValidationError('Choose replacement or clear, not both.')
    if resource.encrypted_credential and previous_configuration is not None and previous_configuration != resource.configuration and not (clear or credential):
        raise ValidationError('Configuration changed. Explicitly replace or clear the credential to prevent accidental reuse.')
    if clear:
        resource.encrypted_credential = ''
    elif credential:
        resource.encrypted_credential = encrypt_bundle({'credential': credential})


def selected_connection(binding):
    if binding.mode == 'platform':
        connection = binding.connection
        if (not binding.allow_inheritance or not connection or not connection.active or
                connection.organization_id != binding.organization_id or connection.definition_id != binding.definition_id or
                connection.environment_id != binding.environment_id):
            raise PermissionDenied('The approved platform connection is unavailable.')
        return connection
    if binding.mode == 'application':
        return binding
    raise PermissionDenied('This integration is disabled.')


def authorize(principal, binding):
    # Reload placement and grants on every operation, including held worker objects.
    binding = ApplicationIntegration.objects.select_related('definition__service', 'definition__provider__package',
        'installation__package', 'installation__service', 'environment', 'connection').get(pk=binding.pk)
    definition = binding.definition
    if (not binding.organization.active or not binding.environment.active or not definition.active or not definition.service.active or
            not binding.installation.enabled or not binding.installation.package.active or not binding.installation.service.active or
            not feature_enabled(binding.organization, 'core-platform')):
        deny(principal, binding, 'integration_unavailable')
    if definition.provider_id and (not definition.provider.enabled or not definition.provider.package.active or not definition.provider.service.active):
        deny(principal, binding, 'provider_unavailable')
    require_access(principal, binding, 'integration.consume')
    if principal.credential:
        require_access(principal, principal.credential, 'integration.application_context')
    elif principal.agent_binding:
        require_access(principal, principal.agent_binding, 'integration.agent_context')
    connection = selected_connection(binding)
    require_access(principal, connection, 'integration.connection')
    if principal.credential:
        if (not credential_allows(principal.credential, []) or principal.credential.installation_id != binding.installation_id or
                principal.credential.environment_id != binding.environment_id or not binding.workload_access):
            deny(principal, binding, 'application_consumption_required')
    elif principal.agent_binding:
        if principal.agent_binding.installation_id != binding.installation_id or principal.agent_binding.environment != binding.environment.name:
            raise PermissionDenied('The agent placement does not match this consumer.')
        from .agent_identity import require_permission
        require_permission(principal, definition.service, definition.use_permission)
    elif principal.user:
        membership = Membership.objects.filter(organization=binding.organization, user=principal.user, active=True).first()
        permission = ServicePermission.objects.filter(service=definition.service, codename=definition.use_permission).first()
        allowed = bool(permission and (principal.user.is_superuser or membership and (
            membership.kind == 'administrator' or membership.roles.filter(organization=binding.organization,
                pk__in=[role.pk for role in visible_resources(principal, membership.roles.filter(organization=binding.organization))],
                permissions=permission).exists())))
        if not allowed:
            record_decision(principal, binding, 'integration.use', False, 'role_required', effective_tags(binding))
            raise PermissionDenied('The user has no live integration role grant.')
        require_access(principal, permission, 'integration.permission')
    else:
        raise PermissionDenied
    record_decision(principal, binding, 'integration.use', True, 'live_consumer_grant', effective_tags(binding))
    return binding, connection


def descriptor(binding, connection=None):
    connection = connection or selected_connection(binding)
    definition = binding.definition
    return {'id': str(binding.pk), 'slug': definition.slug, 'name': definition.name, 'kind': definition.kind,
        'organization_id': str(binding.organization_id), 'installation_id': str(binding.installation_id),
        'environment': binding.environment.name, 'mode': binding.mode, 'revision': binding.revision,
        'connection_revision': connection.revision, 'configuration': connection.configuration,
        'credential_configured': bool(connection.encrypted_credential), 'use_permission': definition.use_permission,
        'authorization_service_id': str(definition.service_id),
        'provider_installation_id': str(definition.provider_id) if definition.provider_id else None}


def settings_document(binding):
    value = {'id': str(binding.pk), 'slug': binding.definition.slug, 'name': binding.definition.name,
        'organization_id': str(binding.organization_id), 'installation_id': str(binding.installation_id), 'environment': binding.environment.name,
        'mode': binding.mode, 'revision': binding.revision, 'configuration': binding.configuration,
        'configuration_schema': binding.definition.configuration_schema, 'credential_configured': bool(binding.encrypted_credential),
        'allow_inheritance': binding.allow_inheritance, 'allow_application_configuration': binding.allow_application_configuration,
        'platform_connection_available': bool(binding.connection_id and binding.connection.active),
        'platform_credential_configured': bool(binding.connection_id and binding.connection.encrypted_credential)}
    return value


@transaction.atomic
def configure_application(binding, principal, values):
    binding = ApplicationIntegration.objects.select_for_update(of=('self',)).get(pk=binding.pk)
    require_management(principal, binding)
    if not binding.allow_application_configuration:
        raise PermissionDenied('Application configuration has not been delegated by the platform administrator.')
    if set(values) - {'revision', 'mode', 'configuration', 'credential', 'clear_credential'}:
        raise ValidationError('Application settings cannot change platform grants or connection placement.')
    if type(values.get('revision')) is not int or values['revision'] != binding.revision:
        raise IntegrationVersionConflict
    previous = binding.configuration
    binding.mode = values.get('mode', binding.mode)
    binding.configuration = values.get('configuration', binding.configuration)
    if binding.mode == 'platform':
        require_access(principal, selected_connection(binding), 'integration.inherit')
    replace_credential(binding, values.get('credential'), values.get('clear_credential', False), previous)
    binding.full_clean()
    binding.revision += 1
    binding.save()
    record_decision(principal, binding, 'integration.application_configured', True, 'delegated_administrator', effective_tags(binding))
    return settings_document(binding)


def backend_credential(principal, binding):
    binding, connection = authorize(principal, binding)
    if not principal.credential or not binding.credential_access:
        deny(principal, binding, 'backend_credential_grant_required')
    record_decision(principal, binding, 'integration.credential_read', True, 'backend_credential_grant', effective_tags(binding))
    return decrypt_bundle(connection.encrypted_credential)['credential'] if connection.encrypted_credential else None
