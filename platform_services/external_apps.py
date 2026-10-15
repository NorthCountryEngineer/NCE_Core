"""Register externally hosted application identities without managing their infrastructure."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from identity.models import Application, AuditEvent, Organization
from identity.policy_evaluation import policy_fact
from .models import Deployment
from .secrets import decrypt_bundle, encrypt_bundle
from .validation import validate_endpoint


def application_deployment(application):
    if not application or not application.pk:
        return None
    return policy_fact(("application_deployment", str(application.service.organization_id), str(application.pk)),
        lambda: Deployment.objects.select_related('environment__boundary_tag', 'environment__organization',
            'installation__package', 'installation__service', 'installation__organization', 'node',
            'application', 'resource_application').filter(
            Q(application=application) | Q(resource_application=application)).first())


def external_available(deployment):
    installation = deployment.installation
    return bool(deployment.target == 'external' and deployment.desired_state == 'running' and
        deployment.environment_id and deployment.environment.active and installation.enabled and
        installation.organization.active and installation.service.active and installation.package.active and
        deployment.application_id and deployment.resource_application_id and
        deployment.application.active and deployment.resource_application.active and
        deployment.application.client_type == Application.CLIENT_PUBLIC and
        deployment.resource_application.client_type == Application.CLIENT_CONFIDENTIAL and
        deployment.application.service_id == installation.service_id and
        deployment.resource_application.service_id == installation.service_id)


@transaction.atomic
def register_external_deployment(installation, environment, origin, actor=None):
    """Registration is metadata and OAuth clients only: no AWS, cluster, or remote requests."""
    Organization.objects.select_for_update().get(pk=installation.organization_id)
    validate_endpoint(origin)
    origin = origin.rstrip('/')
    if (installation.package.kind != 'application' or installation.manifest.get('external_runtime') != 'aws-amplify' or
            not installation.enabled or not installation.service.active or not installation.package.active or
            not installation.organization.active):
        raise ValidationError('Choose an active external AWS Amplify application installation.')
    if environment.organization_id != installation.organization_id or not environment.active:
        raise ValidationError('Choose an active environment from this organization.')
    if environment.name == 'production' and not origin.startswith('https://'):
        raise ValidationError('Production external applications require HTTPS.')
    existing = Deployment.objects.filter(installation=installation, environment=environment, target='external').first()
    if existing:
        if existing.endpoint != origin:
            raise ValidationError('The registered application origin is immutable. Use another environment or installation.')
        return existing
    callback = installation.manifest.get('callback_path', '/oauth/callback')
    browser = Application(service=installation.service, name=f'{installation.package.name} {environment.name} browser',
        client_type=Application.CLIENT_PUBLIC, authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        algorithm=Application.RS256_ALGORITHM, redirect_uris=origin + callback, allowed_origins=origin)
    browser.full_clean()
    browser.save()
    resource = Application(service=installation.service, name=f'{installation.package.name} {environment.name} resource API',
        client_type=Application.CLIENT_CONFIDENTIAL, authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        algorithm=Application.RS256_ALGORITHM, redirect_uris=origin + '/oauth/resource-callback')
    resource_secret = resource.client_secret
    resource.full_clean()
    resource.save()
    parent = Deployment.objects.filter(installation__organization=installation.organization,
        installation__package__slug='core-platform', target__in=['embedded', 'kubernetes-controller']).first()
    deployment = Deployment(installation=installation, environment=environment, parent=parent, name=installation.package.slug,
        target='external', endpoint=origin, application=browser, resource_application=resource,
        encrypted_bundle=encrypt_bundle({'introspection_client_secret': resource_secret}),
        observed_revision=1, observed_state='registered', message='Registered identity; AWS hosting and health are managed externally.')
    deployment.full_clean()
    deployment.save()
    AuditEvent.objects.create(organization=installation.organization, actor=actor,
        action='deployment.external_registered', target=str(deployment.pk))
    return deployment


def external_public_configuration(deployment):
    origin = settings.ISSUER
    return {
        'version': 1, 'issuer': origin, 'organization_id': str(deployment.installation.organization_id),
        'service_id': str(deployment.installation.service_id), 'audience': deployment.installation.service.audience,
        'installation_id': str(deployment.installation_id), 'deployment_id': str(deployment.pk),
        'environment': deployment.environment.name, 'browser_client_id': deployment.application.client_id,
        'redirect_uri': deployment.application.redirect_uris, 'application_origin': deployment.endpoint,
        'authorization_endpoint': origin + '/oauth/authorize/', 'token_endpoint': origin + '/oauth/token/',
        'revocation_endpoint': origin + '/oauth/revoke/', 'introspection_endpoint': origin + '/oauth/introspect/',
        'permissions': [permission['codename'] for permission in deployment.installation.manifest.get('permissions', [])],
    }


def external_resource_credentials(deployment):
    """Private output for a server-side secret store; never add it to public configuration."""
    return {'client_id': deployment.resource_application.client_id,
        'client_secret': decrypt_bundle(deployment.encrypted_bundle)['introspection_client_secret']}
