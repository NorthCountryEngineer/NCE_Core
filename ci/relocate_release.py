"""Offline deterministic customer Helm derivation; no registry copy or deployment.

See ci.migration_chart for the shared pure verifier. This wrapper reads the
existing owner-only local receiver key and writes a customer-signed derivation.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import stat

from cryptography.hazmat.primitives.asymmetric import ed25519

from .migration_chart import (DOMAIN, PROTOCOL, RelocationError, canonical_json, decode64, sha,
    verified_release, verify_derivation, _derive, _json, _archive, _pack)


def _customer_key(path, expected_public):
    target = Path(path).absolute()
    if target != target.resolve() or any((parent / '.git').exists() for parent in target.parents):
        raise RelocationError('Use the existing private customer receiver key outside Git.')
    directory = target.parent.stat()
    if directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
        raise RelocationError('Customer key directory must be owned and not writable by another account.')
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise RelocationError('Customer key must be an owner-only regular 0600 file.')
        data = stream.read(4097)
        if len(data) > 4096:
            raise RelocationError('Customer key file exceeds its bound.')
        document = _json(data)
    if set(document) != {'protocol', 'signing_private_key', 'exchange_private_key'} or document['protocol'] != PROTOCOL:
        raise RelocationError('Use the customer receiver identity key file.')
    private = ed25519.Ed25519PrivateKey.from_private_bytes(decode64(document['signing_private_key'], 32))
    if private.public_key().public_bytes_raw() != decode64(expected_public, 32):
        raise RelocationError('Customer receiver public-key pin does not match this private key.')
    return private


def relocate_release(release_bytes, signature, publisher_key_pem, *, expected_publisher_key_sha256,
        expected_release_hash, original_chart_bytes, mirrored_images, customer_registry, target_origin,
        derived_version, customer_key_file, expected_customer_public_key):
    release, fingerprint = verified_release(release_bytes, signature, publisher_key_pem,
        expected_publisher_key_sha256, expected_release_hash)
    derived, payload = _derive(release, original_chart_bytes, mirrored_images, customer_registry, target_origin,
        derived_version, fingerprint, expected_release_hash)
    key = _customer_key(customer_key_file, expected_customer_public_key)
    envelope = {'protocol': PROTOCOL, 'purpose': 'chart-relocation',
        'signer_key_id': 'sha256:' + sha(key.public_key().public_bytes_raw()), 'payload': payload}
    envelope['signature'] = base64.urlsafe_b64encode(key.sign(DOMAIN + canonical_json(envelope))).decode().rstrip('=')
    return derived, envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['release', 'signature', 'publisher-key', 'chart', 'image-map', 'customer-key-file', 'output-chart', 'output-receipt']:
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ['publisher-spki-sha256', 'release-sha256', 'customer-public-key', 'customer-registry', 'target-origin', 'derived-version']:
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        derived, receipt = relocate_release(args.release.read_bytes(), args.signature.read_bytes(), args.publisher_key.read_bytes(),
            expected_publisher_key_sha256=args.publisher_spki_sha256, expected_release_hash=args.release_sha256,
            original_chart_bytes=args.chart.read_bytes(), mirrored_images=_json(args.image_map.read_bytes()),
            customer_registry=args.customer_registry, target_origin=args.target_origin, derived_version=args.derived_version,
            customer_key_file=args.customer_key_file, expected_customer_public_key=args.customer_public_key)
        for path, data in [(args.output_chart, derived), (args.output_receipt, canonical_json(receipt) + b'\n')]:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        print(json.dumps({'derived_chart_package_digest': receipt['payload']['derived_chart_package_digest'],
            'derivation_receipt_sha256': sha(canonical_json(receipt)), 'published': False, 'deployed': False}))
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, 'Chart relocation refused; review publisher/customer pins, unchanged source package, target and exact mirror mapping.\n')


if __name__ == '__main__':
    main()
