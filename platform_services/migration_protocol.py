"""Portable, domain-separated Ed25519 metadata envelopes; no transport or data access."""
import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL = 'nce-app-migration/v1'
DOMAIN = b'NCE-APP-MIGRATION-SIGNED-V1\n'


def canonical_json(value):
    def check(item, depth=0):
        if depth > 24:
            raise ValueError('Metadata is too deeply nested.')
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ValueError('Only bounded JSON metadata without floats is supported.')
    check(value)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(raw) > 32768:
        raise ValueError('Metadata exceeds 32 KiB.')
    return raw


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def decode64(value, size):
    if not isinstance(value, str) or '=' in value:
        raise ValueError('Use canonical unpadded base64url.')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, TypeError):
        raise ValueError('Invalid public key or signature encoding.') from None
    if len(raw) != size or base64.urlsafe_b64encode(raw).decode().rstrip('=') != value:
        raise ValueError('Invalid public key or signature encoding.')
    return raw


def verify_document(envelope, public_key, expected_purpose):
    if not isinstance(envelope, dict) or set(envelope) != {'protocol', 'purpose', 'signer_key_id', 'payload', 'signature'}:
        raise ValueError('Invalid signed envelope.')
    key = decode64(public_key, 32)
    if (envelope['protocol'] != PROTOCOL or envelope['purpose'] != expected_purpose or
            envelope['signer_key_id'] != 'sha256:' + hashlib.sha256(key).hexdigest()):
        raise ValueError('Wrong protocol, purpose, or pinned signer.')
    signed = {name: envelope[name] for name in ('protocol', 'purpose', 'signer_key_id', 'payload')}
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(decode64(envelope['signature'], 64), DOMAIN + canonical_json(signed))
    except InvalidSignature:
        raise ValueError('Invalid signature.') from None
    return envelope['payload']
