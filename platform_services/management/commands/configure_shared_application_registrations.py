"""Operator-reviewed, metadata-only registration allowlist for one hosted app."""
import json
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from identity.access import Principal, authentication_valid, require_access
from identity.models import AuditEvent, AuthenticationEvidence, Membership, User
from platform_services.cloud_deployments import authorized
from platform_services.cloud_models import CloudApplication
from platform_services.cloud_registrations import REGISTRY_KEY, registration_bindings, validate_registration_export
from platform_services.models import Deployment


class Command(BaseCommand):
    help = 'Replace one hosted application’s explicit additional Core registration allowlist; no hosting, secret export or cloud calls.'

    def add_arguments(self, parser):
        parser.add_argument('--application', required=True, help='Existing hosting CloudApplication UUID')
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument('--deployment', action='append', help='Exact additional external deployment UUID; repeat for multiple organizations')
        selection.add_argument('--clear', action='store_true', help='Remove every additional registration from this hosting application')
        parser.add_argument('--actor-email', required=True, help='Active human administrator of hosting/provider and every selected organization')
        parser.add_argument('--authentication-evidence', help='Exact existing login-evidence UUID belonging to this actor; current authentication policies still apply')
        parser.add_argument('--purpose', default='', help='Explicit current policy purpose, if required; does not grant a purpose or permission')

    def handle(self, **options):
        try:
            with transaction.atomic():
                actor = User.objects.get(email=options['actor_email'].strip().lower(), is_active=True)
                evidence = None
                if options.get('authentication_evidence'):
                    identifier = options['authentication_evidence']
                    if str(UUID(identifier)) != identifier:
                        raise ValueError()
                    evidence = AuthenticationEvidence.objects.select_for_update().select_related('identity__provider').get(
                        pk=identifier, user=actor, revoked=False)
                principal = Principal(user=actor, evidence=evidence, purpose=options.get('purpose', ''))
                app = CloudApplication.objects.select_for_update().select_related('platform', 'environment').get(pk=options['application'])
                for organization in (app.organization, app.platform.organization):
                    if not authentication_valid(actor, organization, evidence):
                        raise PermissionDenied('Current actor authentication is required.')
                authorized(principal, app, 'cloud.application_configure')
                if app.releases.filter(status__in=['queued', 'running']).exists():
                    raise ValidationError('Wait for the current release to finish or cancel it first.')
                previous = app.config.get(REGISTRY_KEY, [])
                values = options.get('deployment') or []
                app.config = {REGISTRY_KEY: values} if values else {}
                app.full_clean()
                bindings = registration_bindings(app)
                for deployment in Deployment.objects.select_related('installation__organization').filter(pk__in=values):
                    if not authentication_valid(actor, deployment.installation.organization, evidence):
                        raise PermissionDenied('Current target organization authentication is required.')
                    if not (actor.is_superuser or Membership.objects.filter(user=actor,
                            organization=deployment.installation.organization, active=True, kind='administrator').exists()):
                        raise PermissionDenied('An active target organization administrator is required.')
                    require_access(principal, deployment, 'cloud.application_registration_configure')
                validate_registration_export(app)
                app.save(update_fields=['config', 'updated'])
                AuditEvent.objects.create(organization=app.organization, actor=actor,
                    action='cloud.application_registrations_configured', target=str(app.pk))
                if evidence is not None:
                    AuditEvent.objects.create(organization=app.organization, actor=actor,
                        action='cloud.application_registration_evidence', target=f'{app.pk}:{evidence.pk}',
                        reason_code='verified_existing_authentication', purpose_code=principal.purpose)
                for identifier in sorted(set(previous) | set(values)):
                    AuditEvent.objects.create(organization=app.organization, actor=actor,
                        action='cloud.application_registration_retained' if identifier in values else 'cloud.application_registration_removed',
                        target=f'{app.pk}:{identifier}')
        except (ObjectDoesNotExist, ValueError, TypeError, ValidationError, PermissionDenied) as error:
            raise CommandError('Registration configuration failed. Check exact application/deployment IDs, live administrator access and compatible registration bindings.') from error
        self.stdout.write(json.dumps({'application_id': str(app.pk), 'registrations': bindings}, indent=2))
