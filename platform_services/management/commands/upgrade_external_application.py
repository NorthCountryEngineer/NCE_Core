"""Apply a reviewed identity-package upgrade without deploying infrastructure."""
import json
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from identity.models import Membership, Organization, User
from platform_services.models import Installation, MarketplacePackage
from platform_services.services import upgrade_installation


class Command(BaseCommand):
    help = 'Upgrade one existing external application identity package. Creates no users, cloud resources, or credentials.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True)
        parser.add_argument('--service-id', required=True)
        parser.add_argument('--package', required=True)
        parser.add_argument('--expected-version', required=True)
        parser.add_argument('--actor-email', required=True)

    @transaction.atomic
    def handle(self, **options):
        try:
            path = Path(options['package'])
            if path.stat().st_size > 65536: raise CommandError('Use a reviewed package under 64 KB.')
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or set(value) != {'slug', 'name', 'version', 'description', 'kind', 'manifest'} or value['kind'] != 'application':
                raise CommandError('Use a complete reviewed application package.')
            organization = Organization.objects.select_for_update().get(slug=options['organization'], active=True)
            actor = User.objects.get(email__iexact=options['actor_email'].strip(), is_active=True)
            if not (actor.is_superuser or Membership.objects.filter(user=actor, organization=organization, active=True, kind='administrator').exists()):
                raise CommandError('The actor must be this organization’s active administrator.')
            installation = Installation.objects.select_related('package', 'service').get(organization=organization,
                service_id=options['service_id'], package__slug=value['slug'], enabled=True)
            package = MarketplacePackage.objects.select_for_update().get(pk=installation.package_id)
            if (package.organization_id not in {None, organization.pk} or installation.manifest.get('external_runtime') != 'aws-amplify' or
                    value['manifest'].get('external_runtime') != 'aws-amplify'):
                raise CommandError('Choose this organization’s existing external application.')
            changed = installation.version != value['version'] or installation.manifest != value['manifest']
            if changed:
                if installation.version != options['expected_version']:
                    raise CommandError('The installed version changed. Review it before retrying.')
                previous_permissions = {row['codename'] for row in installation.manifest.get('permissions', [])}
                next_permissions = {row['codename'] for row in value['manifest'].get('permissions', [])}
                if not previous_permissions <= next_permissions:
                    raise CommandError('This additive identity upgrade cannot remove existing permissions.')
                for key in ['name', 'version', 'description', 'kind', 'manifest']: setattr(package, key, value[key])
                package.full_clean(); package.save(update_fields=['name', 'version', 'description', 'kind', 'manifest'])
                installation.package = package
                upgrade_installation(installation, installation.granted_capabilities, actor)
            self.stdout.write(json.dumps({'organization_id': str(organization.pk), 'service_id': str(installation.service_id),
                'version': installation.version, 'changed': changed, 'cloud_resources_created': False, 'accounts_created': 0}))
        except (OSError, ValueError, ObjectDoesNotExist, ValidationError) as error:
            raise CommandError('Package upgrade failed. Check the reviewed file, expected version, organization, service, and administrator.') from error
