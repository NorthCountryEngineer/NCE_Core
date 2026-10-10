import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from platform_services.models import Deployment
from platform_services.workflows import process_one
from platform_services.training import process_one as process_training


def heartbeat_embedded():
    for deployment in Deployment.objects.select_related("installation__organization", "installation__service", "installation__package").filter(target="embedded"):
        installation = deployment.installation
        active = installation.enabled and installation.organization.active and installation.service.active and installation.package.active
        Deployment.objects.filter(pk=deployment.pk).update(observed_state="running" if active else "stopped",
            healthy=active, observed_revision=deployment.revision, last_seen=timezone.now())


class Command(BaseCommand):
    help = "Run core workflow and training workers and heartbeat embedded core services."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=5)

    def handle(self, *args, **options):
        try:
            while True:
                close_old_connections()
                heartbeat_embedded()
                for _ in range(20):
                    workflow_work = process_one()
                    training_work = process_training()
                    if not workflow_work and not training_work:
                        break
                if options["once"]:
                    return
                time.sleep(max(0.2, min(options["interval"], 30)))
        except KeyboardInterrupt:
            return
