"""First administrator setup for a one-off ECS task; password is secret-injected."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import django
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management import call_command
from django.db import transaction

parser = argparse.ArgumentParser()
parser.add_argument("--slug", required=True)
parser.add_argument("--name", required=True)
parser.add_argument("--admin-email", required=True)
args = parser.parse_args()
password = os.environ.pop("CORE_BOOTSTRAP_PASSWORD", "")
if not password:
    raise SystemExit("Missing secret-injected CORE_BOOTSTRAP_PASSWORD")
django.setup()
with transaction.atomic():
    call_command("bootstrap_organization", slug=args.slug, name=args.name, admin_email=args.admin_email)
    user = get_user_model().objects.get(email=args.admin_email.strip().lower())
    if user.has_usable_password():
        raise SystemExit("Administrator already has a password; use the normal password change flow.")
    validate_password(password, user=user)
    user.set_password(password)
    user.save(update_fields=["password"])
print("First administrator password configured from Secrets Manager; no password was logged.")
