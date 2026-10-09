import base64
import hashlib
import json

from cryptography.fernet import Fernet
from django.conf import settings


def cipher():
    key = hashlib.sha256(("core-platform-integration-v1:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_bundle(bundle):
    return cipher().encrypt(json.dumps(bundle).encode()).decode()


def decrypt_bundle(value):
    return json.loads(cipher().decrypt(value.encode()))
