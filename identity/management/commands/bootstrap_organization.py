from django.core.management.base import BaseCommand
from django.db import transaction
from identity.models import Membership, Organization, User


class Command(BaseCommand):
    help = "Create an organization and its first administrator (operator-only command)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--admin-email", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        organization, _ = Organization.objects.get_or_create(slug=options["slug"], defaults={"name": options["name"]})
        email = options["admin_email"].strip().lower()
        user, created = User.objects.get_or_create(email=email)
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        Membership.objects.get_or_create(organization=organization, user=user,
                                         defaults={"kind": Membership.Kind.ADMINISTRATOR})
        self.stdout.write(self.style.SUCCESS("Organization and administrator membership provisioned."))
        if created:
            self.stdout.write("Set the new account's password with: python manage.py changepassword <admin-email>")
