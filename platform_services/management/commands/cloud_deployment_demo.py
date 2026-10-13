"""Create a disposable, loopback-only deployment sandbox without cloud access."""
from urllib.parse import urlsplit
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from identity.access import Principal
from identity.models import Membership, Organization, User
from platform_services.cloud_models import CloudPlatform
from platform_services.cloud_deployments import verify_platform
from platform_services.environments import create_environment
from platform_services.services import install_core


class Command(BaseCommand):
    help = 'Seed a local deployment simulation. Makes no AWS calls; never changes an existing account password.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', default='deployment-demo')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or urlsplit(settings.ISSUER).hostname not in {'localhost', '127.0.0.1'}:
            raise CommandError('This demo is available only in explicit loopback local mode.')
        slug = options['organization']
        if not slug.startswith('deployment-demo'):
            raise CommandError('Use a disposable organization identifier starting with deployment-demo.')
        org, created = Organization.objects.get_or_create(slug=slug, defaults={'name': 'Deployment demo'})
        email = f'{slug}@example.test'
        user = User.objects.filter(email=email).first()
        if user and not Membership.objects.filter(organization=org, user=user, kind='administrator', active=True).exists():
            raise CommandError('An existing account uses the demo address. Choose a different demo organization.')
        if not created and org.name != 'Deployment demo':
            raise CommandError('That organization is not a deployment demo. Choose another identifier.')
        if not user:
            user = User.objects.create_user(email, 'Local-demo-only-2026!')
        Membership.objects.get_or_create(organization=org, user=user, defaults={'kind': 'administrator'})
        install_core(org, actor=user)
        env = org.environments.filter(name='development').first() or create_environment(org, name='development',
            namespace='deployment-demo-dev', application_origin='http://localhost:8000')
        platform, _ = CloudPlatform.objects.get_or_create(organization=org, name='AWS preview · simulation', defaults={
            'mode': 'demo', 'allowed_repositories': ['NorthCountryEngineer/Axolotl_News'],
            'config': {'environment_ids': [str(env.pk)], 'archetypes': ['amplify-static', 'amplify-serverless']}})
        if platform.mode != 'demo':
            raise CommandError('The demo platform was changed. Choose another demo organization.')
        verify_platform(platform, Principal(user=user))
        self.stdout.write(f'Demo ready: {settings.ISSUER}/organizations/{slug}/platform/cloud/apps/')
        self.stdout.write(f'Local demo email: {email}\nInitial password: Local-demo-only-2026!')
        self.stdout.write('Existing passwords are preserved. This platform simulates deployment and makes no AWS calls.')
