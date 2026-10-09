from django.core.management.base import BaseCommand, CommandError
from identity.models import User


class Command(BaseCommand):
    help = "Operator command: grant or revoke developer onboarding access for an existing account."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--revoke", action="store_true")

    def handle(self, *args, **options):
        changed = User.objects.filter(email=options["email"].strip().lower()).update(is_developer=not options["revoke"])
        if not changed:
            raise CommandError("Create the account first; no account exists for this email.")
        self.stdout.write("Developer designation updated.")
