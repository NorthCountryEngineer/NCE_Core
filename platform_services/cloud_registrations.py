"""Explicit Core-owned registrations sharing one existing application runtime.

No SSM reads, organization scans, foreign bundles, or browser-selected authority.
Only reviewed deployment IDs in the hosting application's configuration are used.
"""
from uuid import UUID

from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError

from identity.models import Application
from .external_apps import external_available, external_resource_credentials
from .models import Deployment

REGISTRY_KEY = 'core_registration_deployment_ids'
MAX_ADDITIONAL = 19


def registration_ids(application):
    config = application.config
    if not isinstance(config, dict) or set(config) - {REGISTRY_KEY}:
        raise ValidationError('Unsupported application configuration.')
    values = config.get(REGISTRY_KEY, [])
    if (not isinstance(values, list) or len(values) > MAX_ADDITIONAL or
            any(not isinstance(value, str) for value in values)):
        raise ValidationError('Choose at most 19 explicit additional Core deployments.')
    try:
        parsed = [str(UUID(value)) for value in values]
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Use canonical, distinct Core deployment UUIDs.') from None
    if parsed != values or len(set(parsed)) != len(parsed):
        raise ValidationError('Use canonical, distinct Core deployment UUIDs.')
    if values and application.archetype != 'amplify-fargate':
        raise ValidationError('Additional Core registrations require the reviewed Fargate application recipe.')
    return values


def _descriptor(deployment):
    installation, browser, resource = deployment.installation, deployment.application, deployment.resource_application
    if (not external_available(deployment) or installation.service.organization_id != installation.organization_id or
            deployment.environment.organization_id != installation.organization_id or
            browser.allowed_origins != deployment.endpoint or
            browser.redirect_uris != deployment.endpoint + installation.manifest.get('callback_path', '/oauth/callback') or
            resource.redirect_uris != deployment.endpoint + '/oauth/resource-callback' or
            browser.authorization_grant_type != Application.GRANT_AUTHORIZATION_CODE or
            resource.authorization_grant_type != Application.GRANT_AUTHORIZATION_CODE or
            browser.algorithm != Application.RS256_ALGORITHM or resource.algorithm != Application.RS256_ALGORITHM or
            resource.allowed_origins != '' or browser.skip_authorization or resource.skip_authorization or
            not browser.hash_client_secret or not resource.hash_client_secret):
        raise ValidationError('A configured Core registration is unavailable or its exact identity binding changed.')
    return {'organization_id': str(installation.organization_id), 'service_id': str(installation.service_id),
        'deployment_id': str(deployment.pk), 'browser_client_id': browser.client_id,
        'resource_client_id': resource.client_id, 'environment': deployment.environment.name,
        'public_origin': deployment.endpoint, 'redirect_uri': browser.redirect_uris}


def primary_registration_binding(application, primary=None):
    primary = primary or Deployment.objects.select_related('installation__organization', 'installation__service',
        'installation__package', 'environment', 'application', 'resource_application').filter(
            installation_id=application.installation_id, environment_id=application.environment_id, target='external').first()
    if not primary:
        raise ValidationError('The primary Core registration is unavailable.')
    descriptor = _descriptor(primary)
    if (primary.installation_id != application.installation_id or primary.environment_id != application.environment_id or
            primary.installation.organization_id != application.organization_id or
            descriptor['public_origin'] != application.public_origin):
        raise ValidationError('The primary Core registration does not match this hosting application.')
    return descriptor


def registration_bindings(application, primary=None):
    """Secret-free authority descriptors included in the exact reviewed plan."""
    selected = registration_ids(application)
    if not selected:
        return []
    if not application.installation_id:
        raise ValidationError('Register the primary application before adding another Core organization.')
    source = primary_registration_binding(application, primary)
    candidates = {str(value.pk): value for value in Deployment.objects.select_related('installation__organization',
        'installation__service', 'installation__package', 'environment', 'application', 'resource_application').filter(pk__in=selected)}
    bindings, seen_orgs, seen_services = [], {source['organization_id']}, {source['service_id']}
    for identifier in selected:
        candidate = candidates.get(identifier)
        if not candidate:
            raise ValidationError('A configured Core registration no longer exists.')
        descriptor = _descriptor(candidate)
        if (descriptor['public_origin'] != source['public_origin'] or descriptor['redirect_uri'] != source['redirect_uri'] or
                descriptor['organization_id'] in seen_orgs or descriptor['service_id'] in seen_services):
            raise ValidationError('Additional Core registrations must use distinct organizations and the exact hosted origin and callback.')
        seen_orgs.add(descriptor['organization_id'])
        seen_services.add(descriptor['service_id'])
        bindings.append(descriptor)
    return bindings


def resource_bundle(application, primary):
    """Rebuild from current Core records; never merge an existing secret-store value."""
    bindings = registration_bindings(application, primary)
    result = external_resource_credentials(primary)
    if not bindings:
        return result  # Preserve the established two-key single-organization contract.
    if not check_password(result['client_secret'], primary.resource_application.client_secret):
        raise ValidationError('The primary resource credential requires a current secure deployment copy.')
    result['registrations'] = []
    for descriptor in bindings:
        deployment = Deployment.objects.select_related('installation__organization', 'installation__service', 'installation__package',
            'environment', 'application', 'resource_application').get(pk=descriptor['deployment_id'])
        if _descriptor(deployment) != descriptor:
            raise ValidationError('A configured Core registration changed while exporting credentials.')
        credentials = external_resource_credentials(deployment)
        if not check_password(credentials['client_secret'], deployment.resource_application.client_secret):
            raise ValidationError('A configured resource credential requires a current secure deployment copy.')
        result['registrations'].append({key: descriptor[key] for key in (
            'organization_id', 'service_id', 'deployment_id', 'browser_client_id', 'resource_client_id', 'environment')} |
            {'resource_client_secret': credentials['client_secret'], 'name': deployment.installation.organization.name})
    return result


def validate_registration_export(application):
    """Preflight current hashes and Standard-size limits before a cloud build."""
    if not registration_ids(application):
        return
    from .cloud_aws import AWSAdapterError, encode_resource_credentials
    primary = Deployment.objects.select_related('installation__organization', 'resource_application').filter(
        installation_id=application.installation_id, environment_id=application.environment_id, target='external').first()
    if not primary:
        raise ValidationError('The primary Core registration is unavailable.')
    try:
        encode_resource_credentials(resource_bundle(application, primary))
    except AWSAdapterError:
        raise ValidationError('The configured Core registrations do not fit the supported Standard credential bundle.') from None
