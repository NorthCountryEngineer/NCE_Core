from django.core.management.base import BaseCommand, CommandError
from identity.models import Organization
from platform_services.edge import register_application


class Command(BaseCommand):
    help = 'Register NCE Robotics and its public PKCE iOS client for an organization. Does not grant roles or enroll devices.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True, help='Exact existing organization slug')

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(slug=options['organization'], active=True)
        except Organization.DoesNotExist:
            raise CommandError('No active organization has that slug.') from None
        installation, application = register_application(organization)
        self.stdout.write(self.style.SUCCESS('NCE Robotics registered.'))
        self.stdout.write('Installation: ' + str(installation.pk))
        self.stdout.write('Public client ID: ' + application.client_id)
        self.stdout.write('Redirect URI: ' + application.redirect_uris)
