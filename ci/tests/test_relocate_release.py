"""Offline customer chart derivation; neither a registry nor a cluster is contacted."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519

from ci import oci_release
from ci import relocate_release as r
from platform_services.migration_protocol import DOMAIN, PROTOCOL, canonical_json


class RelocateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        config = oci_release.load_config(oci_release.ROOT / 'ci/applications/proposal-studio.example.json')
        self.images = {name: item['repository'] + '@sha256:' + hashlib.sha256(name.encode()).hexdigest()
            for name, item in config['images'].items()}
        chart, version = oci_release.generate_chart(config, self.images, 'a' * 40, 'b' * 40, self.root)
        self.files = {chart.name + '/' + path.relative_to(chart).as_posix(): (path.read_bytes(), 0o644, False)
            for path in chart.rglob('*') if path.is_file()}
        self.chart = r._pack(self.files)
        self.release = {'status': 'published', 'images': self.images, 'chart': {'version': version,
            'oci_digest': 'sha256:' + 'd' * 64, 'package_digest': 'sha256:' + r.sha(self.chart)}}
        self.publisher = ec.generate_private_key(ec.SECP256R1())
        self.publisher_pem = self.publisher.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.publisher_pin = r.sha(self.publisher.public_key().public_bytes(serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo))
        self.customer = ed25519.Ed25519PrivateKey.generate()
        self.customer_public = base64.urlsafe_b64encode(self.customer.public_key().public_bytes_raw()).decode().rstrip('=')
        self.key_file = self.root / 'identity.json'
        descriptor = os.open(self.key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(canonical_json({'protocol': PROTOCOL,
                'signing_private_key': base64.urlsafe_b64encode(self.customer.private_bytes_raw()).decode().rstrip('='),
                'exchange_private_key': base64.urlsafe_b64encode(x25519.X25519PrivateKey.generate().private_bytes_raw()).decode().rstrip('=')}))
        self.mapping = {name: 'registry.customer.example/team/' + name + '@' + reference.split('@')[1]
            for name, reference in self.images.items()}
        self.options = {'expected_publisher_key_sha256': self.publisher_pin,
            'original_chart_bytes': self.chart, 'mirrored_images': self.mapping, 'customer_registry': 'registry.customer.example',
            'target_origin': 'https://proposal.customer.example:8443', 'derived_version': '0.1.0-customer.1',
            'customer_key_file': self.key_file, 'expected_customer_public_key': self.customer_public}

    def signed_release(self, release=None):
        raw = json.dumps(release or self.release, sort_keys=True, separators=(',', ':')).encode()
        signature = base64.b64encode(self.publisher.sign(raw, ec.ECDSA(hashes.SHA256())))
        return raw, signature

    def derive(self, **overrides):
        raw, signature = self.signed_release()
        options = {**self.options, 'expected_release_hash': r.sha(raw), **overrides}
        return r.relocate_release(raw, signature, self.publisher_pem, **options)

    def verify(self, derived, envelope, **overrides):
        raw, signature = self.signed_release()
        return r.verify_derivation(raw, signature, self.publisher_pem,
            expected_publisher_key_sha256=self.publisher_pin, expected_release_hash=r.sha(raw),
            original_chart_bytes=self.chart, derived_chart_bytes=derived, envelope=envelope,
            expected_customer_public_key=overrides.get('expected_customer_public_key', self.customer_public),
            expected_target_origin=overrides.get('expected_target_origin', self.options['target_origin']),
            expected_mirrored_images=overrides.get('expected_mirrored_images', self.mapping),
            expected_customer_registry='registry.customer.example')

    def resign_customer(self, envelope):
        result = {name: value for name, value in envelope.items() if name != 'signature'}
        result['signature'] = base64.urlsafe_b64encode(self.customer.sign(DOMAIN + canonical_json(result))).decode().rstrip('=')
        return result

    def test_relocates_only_repository_constants_and_version_preserving_security_bytes(self):
        derived, envelope = self.derive()
        payload = self.verify(derived, envelope)
        root, files = r._archive(derived)
        self.assertEqual(payload['mirrored_images'], self.mapping)
        self.assertEqual(payload['original_chart_package_digest'], self.release['chart']['package_digest'])
        self.assertEqual(payload['derived_chart_package_digest'], 'sha256:' + r.sha(derived))
        self.assertEqual(payload['changed_files'], sorted(root + '/' + name for name in ['Chart.yaml', 'values.yaml', 'values.schema.json']))
        for name, original in self.files.items():
            if name not in payload['changed_files']:
                self.assertEqual(files[name], original, name)
        original_values = json.loads(self.files[root + '/values.yaml'][0])
        values = json.loads(files[root + '/values.yaml'][0])
        self.assertEqual(values['images'], self.mapping)
        original_values['images'] = self.mapping
        self.assertEqual(values, original_values)
        schema = json.loads(files[root + '/values.schema.json'][0])
        for name, reference in self.mapping.items():
            self.assertEqual(schema['properties']['images']['properties'][name]['const'], reference)
        # Session/database wiring, hooks and default-deny networking are preserved.
        self.assertEqual(files[root + '/templates/network-policy.yaml'], self.files[root + '/templates/network-policy.yaml'])
        self.assertEqual(files[root + '/templates/migration.yaml'], self.files[root + '/templates/migration.yaml'])
        self.assertEqual(self.derive(), (derived, envelope))

    def test_rejects_changed_digest_incomplete_map_other_registry_and_source_repository(self):
        for change in ['digest', 'missing', 'registry', 'source']:
            mapping = dict(self.mapping)
            component = next(iter(mapping))
            if change == 'digest':
                mapping[component] = mapping[component].split('@')[0] + '@sha256:' + '0' * 64
            elif change == 'missing':
                mapping.pop(component)
            elif change == 'registry':
                mapping[component] = mapping[component].replace('registry.customer.example', 'unreviewed.example')
            else:
                mapping[component] = self.images[component]
            with self.subTest(change=change), self.assertRaises(r.RelocationError):
                self.derive(mirrored_images=mapping)

    def test_rejects_wrong_publisher_signature_key_pin_release_hash_and_package(self):
        for changed in [{'expected_publisher_key_sha256': '0' * 64}, {'expected_release_hash': '0' * 64},
                {'original_chart_bytes': self.chart + b'changed'}]:
            with self.subTest(changed=list(changed)), self.assertRaises(r.RelocationError):
                self.derive(**changed)
        raw, signature = self.signed_release()
        with self.assertRaises(r.RelocationError):
            r.verified_release(raw, base64.b64encode(b'bad'), self.publisher_pem, self.publisher_pin, r.sha(raw))

    def test_customer_private_key_is_pinned_owned_and_not_world_readable(self):
        wrong = base64.urlsafe_b64encode(ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode().rstrip('=')
        with self.assertRaises(r.RelocationError):
            self.derive(expected_customer_public_key=wrong)
        self.key_file.chmod(0o644)
        with self.assertRaises(r.RelocationError):
            self.derive()

    def test_even_customer_signed_other_file_edits_are_rejected(self):
        derived, envelope = self.derive()
        root, files = r._archive(derived)
        name = root + '/templates/network-policy.yaml'
        files[name] = (files[name][0].replace(b'policyTypes:', b'policyTypes: # changed'), files[name][1], False)
        changed = r._pack(files)
        tampered = deepcopy(envelope)
        tampered['payload']['derived_chart_package_digest'] = 'sha256:' + r.sha(changed)
        with self.assertRaises(r.RelocationError):
            self.verify(changed, self.resign_customer(tampered))

    def test_rejects_wrong_customer_public_pin_target_and_unsigned_receipt(self):
        derived, envelope = self.derive()
        with self.assertRaises(r.RelocationError):
            self.verify(derived, envelope, expected_target_origin='https://different.example')
        with self.assertRaises(r.RelocationError):
            self.verify(derived, envelope, expected_customer_public_key=base64.urlsafe_b64encode(b'x' * 32).decode().rstrip('='))
        envelope['payload']['target_origin'] = 'https://different.example'
        with self.assertRaises(r.RelocationError):
            self.verify(derived, envelope)

    def test_tar_traversal_symlink_duplicates_devices_and_expansion_are_rejected(self):
        for change in ['traversal', 'symlink', 'duplicate', 'device']:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                for name, (content, _, _) in self.files.items():
                    item = tarfile.TarInfo(name); item.size = len(content)
                    archive.addfile(item, io.BytesIO(content))
                item = tarfile.TarInfo('../escape' if change == 'traversal' else next(iter(self.files)) if change == 'duplicate' else 'chart/unsafe')
                if change == 'symlink':
                    item.type, item.linkname = tarfile.SYMTYPE, '/etc/passwd'
                elif change == 'device':
                    item.type = tarfile.CHRTYPE
                archive.addfile(item)
            package = raw.getvalue()
            release = deepcopy(self.release)
            release['chart']['package_digest'] = 'sha256:' + r.sha(package)
            signed, signature = self.signed_release(release)
            with self.subTest(change=change), self.assertRaises(r.RelocationError):
                r.relocate_release(signed, signature, self.publisher_pem, **{**self.options,
                    'original_chart_bytes': package, 'expected_release_hash': r.sha(signed)})
        with patch('ci.migration_chart.MAX_EXPANDED', 100):
            with self.assertRaises(r.RelocationError):
                self.derive()

    def test_customer_signed_mapping_requires_separately_approved_exact_repositories(self):
        different = {name: reference.replace('/team/', '/unreviewed/') for name, reference in self.mapping.items()}
        derived, receipt = self.derive(mirrored_images=different)
        with self.assertRaises(r.RelocationError):
            self.verify(derived, receipt)
        self.assertEqual(self.verify(derived, receipt, expected_mirrored_images=different)['mirrored_images'], different)

    def test_customer_signature_does_not_permit_boolean_in_place_of_integer_metadata(self):
        derived, receipt = self.derive()
        receipt['payload']['schema_version'] = True
        with self.assertRaises(r.RelocationError):
            self.verify(derived, self.resign_customer(receipt))


if __name__ == '__main__':
    unittest.main()
