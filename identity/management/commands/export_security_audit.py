import json
from django.core.management.base import BaseCommand
from identity.models import AuditEvent


class Command(BaseCommand):
    help = "Operator-only JSONL audit export; route to access-controlled archival storage. No automatic external transmission."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization slug")
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=10000)

    def handle(self, *args, **options):
        rows = AuditEvent.objects.filter(organization__slug=options["organization"], pk__gt=options["after_id"]).order_by("pk")
        for row in rows[:max(1, min(options["limit"], 10000))].iterator():
            value = {"id": row.pk, "organization_id": str(row.organization_id), "actor_subject": str(row.actor_subject) if row.actor_subject else None,
                "principal_id": str(row.principal_id) if row.principal_id else None,
                "principal_kind": row.principal_kind, "request_id": str(row.request_id) if row.request_id else None,
                "action": row.action, "target": row.target, "outcome": row.outcome, "reason_code": row.reason_code,
                "purpose_code": row.purpose_code, "tags": row.tags, "created": row.created.isoformat()}
            self.stdout.write(json.dumps(value))
