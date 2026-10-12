"""Register source metadata and explicitly export credentials without provisioning hosting."""
import json
import os
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from identity.models import Membership, Organization, User
from platform_services.external_apps import external_public_configuration, external_resource_credentials, register_external_deployment
from platform_services.models import MarketplacePackage
from platform_services.services import install_package


def exclusive_json(path, value, private=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, indent=2)
            output.write('\n')
    except Exception:
        path.unlink(missing_ok=True)
        raise


class Command(BaseCommand):
    help = 'Register an external AWS Amplify marketplace application. Export credentials to a new private file or explicitly transfer them to the configured SSM parameter; never provision hosting.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True, help='Existing organization slug')
        parser.add_argument('--environment', required=True, choices=['sandbox', 'development', 'staging', 'uat', 'production'])
        parser.add_argument('--package', required=True, help='Reviewed local marketplace package JSON')
        parser.add_argument('--origin', required=True, help='Exact HTTPS application origin')
        parser.add_argument('--actor-email', required=True, help='Existing organization administrator authorizing registration')
        parser.add_argument('--public-config', help='Optional new output JSON file; public configuration is always printed to stdout')
        destination = parser.add_mutually_exclusive_group(required=True)
        destination.add_argument('--credentials-file', help='New private file outside Git or under Core .local/')
        destination.add_argument('--credentials-parameter', '--private-output-ssm-parameter', dest='credentials_parameter',
            help='Explicit one-off ECS transfer to its configured SSM SecureString parameter; existing parameters are never overwritten')

    def handle(self, **options):
        public_path = Path(options['public_config']).expanduser().resolve() if options.get('public_config') else None
        secret_path = Path(options['credentials_file']).expanduser().resolve() if options.get('credentials_file') else None
        if (public_path and public_path == secret_path) or any(path.exists() for path in [public_path, secret_path] if path):
            raise CommandError('Choose two distinct new output files. Existing files are never overwritten.')
        root = Path(settings.BASE_DIR).resolve()
        if secret_path and secret_path.is_relative_to(root) and not secret_path.is_relative_to(root / '.local'):
            raise CommandError('Private credentials inside this checkout must be under ignored .local/.')
        if any(not path.parent.is_dir() for path in [public_path, secret_path] if path):
            raise CommandError('Create the output directories first; private directories should be accessible only to their owner.')
        try:
            package_path = Path(options['package'])
            if package_path.stat().st_size > 65536:
                raise CommandError('Marketplace package JSON must be under 64 KB.')
            value = json.loads(package_path.read_text())
            if not isinstance(value, dict) or set(value) != {'slug', 'name', 'version', 'description', 'kind', 'manifest'} or value['kind'] != 'application':
                raise CommandError('Use an application package with slug, name, version, description, kind, and manifest.')
            if not isinstance(value['manifest'], dict) or value['manifest'].get('external_runtime') != 'aws-amplify':
                raise CommandError('This command requires an explicit AWS Amplify external-runtime package.')
            organization = Organization.objects.get(slug=options['organization'], active=True)
            actor = User.objects.get(email=options['actor_email'].strip().lower(), is_active=True)
            if not (actor.is_superuser or Membership.objects.filter(user=actor, organization=organization, active=True, kind='administrator').exists()):
                raise CommandError('The actor must be this organization’s active administrator.')
            environment = organization.environments.get(name=options['environment'], active=True)
            written = []
            try:
                with transaction.atomic():
                    package = MarketplacePackage.objects.filter(slug=value['slug']).first()
                    if package:
                        if package.organization_id not in {None, organization.pk} or any(getattr(package, key) != value[key] for key in ['name', 'version', 'description', 'kind', 'manifest']):
                            raise CommandError('An existing package differs or belongs to another organization. Publish/review a separate release first.')
                    else:
                        package = MarketplacePackage(organization=organization, **value)
                        package.full_clean()
                        package.save()
                    installation = install_package(organization, package, actor=actor)
                    if installation.manifest != package.manifest or installation.version != package.version:
                        raise CommandError('Review and upgrade the installation before registering this release.')
                    deployment = register_external_deployment(installation, environment, options['origin'], actor)
                    public = external_public_configuration(deployment)
                    credentials = external_resource_credentials(deployment)
                    if public_path:
                        exclusive_json(public_path, public)
                        written.append(public_path)
                    if secret_path:
                        exclusive_json(secret_path, credentials, private=True)
                        written.append(secret_path)
                    else:
                        from deployment.aws_ssm import put_secure_parameter
                        put_secure_parameter(options['credentials_parameter'], json.dumps(credentials))
            except Exception:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
        except (OSError, ValueError, RuntimeError, ValidationError, ObjectDoesNotExist) as error:
            raise CommandError('Registration failed. Check the reviewed package, existing organization/environment/admin, and new output paths.') from error
        self.stdout.write(json.dumps(public, indent=2))
        if secret_path:
            self.stderr.write(f'Private resource credentials written to {secret_path}. Keep this file out of the browser and Git.')
        else:
            self.stderr.write('Private resource credentials transferred to the configured SSM parameter. No private credential file was written.')
