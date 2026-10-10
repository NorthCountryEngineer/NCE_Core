from django.core.management.base import BaseCommand
from platform_services.documentation_catalog import sync_bundled_documentation


class Command(BaseCommand):
    help = 'Publish changed bundled platform documentation; inherit new pages without replacing organization customizations or pinned revisions.'

    def handle(self, *args, **options):
        sync_bundled_documentation()
        self.stdout.write('Platform documentation synchronized; existing organization revision choices and customizations preserved.')
