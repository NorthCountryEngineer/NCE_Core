#!/usr/bin/env python3
"""Create a NEW private runtime secret locally; does not contact AWS."""
import argparse
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
value = {
    "django_key": secrets.token_urlsafe(64),
    "bootstrap_password": secrets.token_urlsafe(32),
    "oidc_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode(),
}
descriptor = os.open(args.output.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as handle:
    json.dump(value, handle, indent=2)
    handle.write("\n")
print(f"Created private file {args.output}; no AWS calls were made. Keep the file out of Git.")
