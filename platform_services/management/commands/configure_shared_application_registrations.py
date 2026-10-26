"""Operator-reviewed, metadata-only registration allowlist for one hosted app."""
import json

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from identity.access import Principal, require_access
from identity.models import AuditEvent, Membership, User
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

    def handle(self, **options):
        try:
            with transaction.atomic():
                actor = User.objects.get(email=options['actor_email'].strip().lower(), is_active=True)
                principal = Principal(user=actor)
                app = CloudApplication.objects.select_for_update().select_related('platform', 'environment').get(pk=options['application'])
                authorized(principal, app, 'cloud.application_configure')
                if app.releases.filter(status__in=['queued', 'running']).exists():
                    raise ValidationError('Wait for the current release to finish or cancel it first.')
                previous = app.config.get(REGISTRY_KEY, [])
                values = options.get('deployment') or []
                app.config = {REGISTRY_KEY: values} if values else {}
                app.full_clean()
                bindings = registration_bindings(app)
                for deployment in Deployment.objects.select_related('installation__organization').filter(pk__in=values):
                    if not (actor.is_superuser or Membership.objects.filter(user=actor,
                            organization=deployment.installation.organization, active=True, kind='administrator').exists()):
                        raise PermissionDenied('An active target organization administrator is required.')
                    require_access(principal, deployment, 'cloud.application_registration_configure')
                validate_registration_export(app)
                app.save(update_fields=['config', 'updated'])
                AuditEvent.objects.create(organization=app.organization, actor=actor,
                    action='cloud.application_registrations_configured', target=str(app.pk))
                for identifier in sorted(set(previous) | set(values)):
                    AuditEvent.objects.create(organization=app.organization, actor=actor,
                        action='cloud.application_registration_retained' if identifier in values else 'cloud.application_registration_removed',
                        target=f'{app.pk}:{identifier}')
        except (ObjectDoesNotExist, ValueError, TypeError, ValidationError, PermissionDenied) as error:
            raise CommandError('Registration configuration failed. Check exact application/deployment IDs, live administrator access and compatible registration bindings.') from error
        self.stdout.write(json.dumps({'application_id': str(app.pk), 'registrations': bindings}, indent=2))
