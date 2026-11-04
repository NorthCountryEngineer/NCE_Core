"""Pure offline customer chart derivation verification; no transport, keys or execution.

Kept byte-identical with Proposal Studio's migration_chart module. Canonical
signed metadata follows nce-app-migration/v1; publisher bytes use ECDSA P-256.
"""
import base64
from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import stat
import tarfile
from urllib.parse import urlsplit
import zlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
import yaml

PROTOCOL = 'nce-app-migration/v1'
DOMAIN = b'NCE-APP-MIGRATION-SIGNED-V1\n'


def canonical_json(value):
    def check(item, depth=0):
        if depth > 24:
            raise ValueError('Metadata nesting exceeds its bound.')
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ValueError('Only typed JSON metadata without floats is accepted.')
    check(value)
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    if len(data) > 32768:
        raise ValueError('Metadata exceeds 32 KiB.')
    return data


def decode64(value, size):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]*', value):
        raise ValueError('Use canonical unpadded base64url.')
    decoded = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if len(decoded) != size or base64.urlsafe_b64encode(decoded).decode().rstrip('=') != value:
        raise ValueError('Incorrect key or signature length.')
    return decoded


def verify_document(envelope, public_key, purpose):
    if not isinstance(envelope, dict) or set(envelope) != {'protocol', 'purpose', 'payload', 'signer_key_id', 'signature'}:
        raise ValueError('Invalid signature envelope.')
    public = decode64(public_key, 32)
    if (envelope['protocol'] != PROTOCOL or envelope['purpose'] != purpose or
            envelope['signer_key_id'] != 'sha256:' + hashlib.sha256(public).hexdigest()):
        raise ValueError('Incorrect pinned signer, protocol or purpose.')
    body = {name: value for name, value in envelope.items() if name != 'signature'}
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public).verify(decode64(envelope['signature'], 64), DOMAIN + canonical_json(body))
    except InvalidSignature:
        raise ValueError('Invalid customer signature.') from None
    return envelope['payload']


MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?')


class RelocationError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise RelocationError('Duplicate metadata keys are prohibited.')
            value[key] = item
        return value
    try:
        result = json.loads(data, object_pairs_hook=pairs)
        canonical_json(result)
        return result
    except (ValueError, TypeError, RecursionError):
        raise RelocationError('Invalid bounded release metadata.') from None


def _origin(value):
    if not isinstance(value, str) or len(value) > 250 or '\\' in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise RelocationError('Use an exact HTTPS target origin.')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or
                parsed.query or parsed.fragment or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
    except ValueError:
        raise RelocationError('Use an exact HTTPS target origin.') from None
    return value


def _image(value):
    if not isinstance(value, str) or len(value) > 512 or value.count('@') != 1:
        raise RelocationError('Use an exact immutable customer image reference.')
    repository, digest = value.split('@')
    if '/' not in repository or not DIGEST.fullmatch(digest):
        raise RelocationError('Use an exact immutable customer image reference.')
    registry, path = repository.split('/', 1)
    _origin('https://' + registry)
    if not re.fullmatch(r'[a-z0-9]+(?:[._-]*[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]*[a-z0-9]+)*)*', path):
        raise RelocationError('Use a canonical OCI repository path without a tag.')
    return repository, digest


def verified_release(release_bytes, signature, publisher_key_pem, expected_publisher_key_sha256, expected_release_hash):
    if len(release_bytes) > 32768 or len(signature) > 16384 or len(publisher_key_pem) > 16384:
        raise RelocationError('Publisher metadata exceeds its bound.')
    try:
        public = serialization.load_pem_public_key(publisher_key_pem)
        if not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != 'secp256r1':
            raise ValueError
        fingerprint = sha(public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        if (not HEX.fullmatch(expected_publisher_key_sha256) or fingerprint != expected_publisher_key_sha256 or
                sha(release_bytes) != expected_release_hash):
            raise ValueError
        public.verify(base64.b64decode(signature.strip(), validate=True), release_bytes, ec.ECDSA(hashes.SHA256()))
        release = _json(release_bytes)
        if (release['status'] != 'published' or not isinstance(release['images'], dict) or
                not 1 <= len(release['images']) <= 128 or not DIGEST.fullmatch(release['chart']['package_digest']) or
                not DIGEST.fullmatch(release['chart']['oci_digest'])):
            raise ValueError
        for name, reference in release['images'].items():
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name):
                raise ValueError
            _image(reference)
        return release, fingerprint
    except (ValueError, TypeError, KeyError, InvalidSignature):
        raise RelocationError('Publisher signature, public-key pin, or reviewed release hash is invalid.') from None


def _archive(data):
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE:
        raise RelocationError('Chart package exceeds its bound.')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            expanded = stream.read(MAX_EXPANDED + 1)
        if len(expanded) > MAX_EXPANDED:
            raise RelocationError('Expanded chart exceeds its bound.')
        files, root = {}, None
        with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
            for member in archive:
                name = member.name.rstrip('/')
                path = PurePosixPath(name)
                if (len(files) >= 256 or not name or path.is_absolute() or any(part in {'.', '..'} for part in path.parts)
                        or path.as_posix() != name or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or member.issym() or member.islnk() or not (member.isfile() or member.isdir())
                        or member.size < 0 or member.size > 8 * 1024 * 1024 or name in files):
                    raise RelocationError('Unsafe or duplicate chart member.')
                root = root or path.parts[0]
                if path.parts[0] != root or (member.isfile() and len(path.parts) < 2):
                    raise RelocationError('Chart must have one package root.')
                content = archive.extractfile(member).read() if member.isfile() else None
                files[name] = (content, stat.S_IMODE(member.mode), member.isdir())
        if not root or any(root + '/' + name not in files for name in ('Chart.yaml', 'values.yaml', 'values.schema.json')):
            raise RelocationError('The reviewed generated Core chart files are required.')
        return root, files
    except (OSError, EOFError, tarfile.TarError, ValueError, zlib.error):
        raise RelocationError('Unsafe or invalid chart archive.') from None


def _pack(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, (data, mode, directory) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.mode, info.uid, info.gid, info.mtime = mode, 0, 0, 0
            info.uname, info.gname = '', ''
            info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
            info.size = 0 if directory else len(data)
            archive.addfile(info, None if directory else io.BytesIO(data))
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as stream:
        stream.write(raw.getvalue())
    return output.getvalue()


def _derive(release, chart_bytes, mirrored_images, customer_registry, target_origin, derived_version, fingerprint, release_hash):
    _origin(target_origin)
    _origin('https://' + customer_registry)
    if not isinstance(derived_version, str) or not VERSION.fullmatch(derived_version) or derived_version == release['chart']['version']:
        raise RelocationError('Review a distinct valid derived chart version.')
    if not isinstance(mirrored_images, dict) or set(mirrored_images) != set(release['images']):
        raise RelocationError('Map exactly every published image.')
    for name, mapped in mirrored_images.items():
        repository, digest = _image(mapped)
        if (digest != _image(release['images'][name])[1] or not repository.startswith(customer_registry + '/')
                or repository == _image(release['images'][name])[0]):
            raise RelocationError('Customer mirrors must preserve every exact image digest in the approved registry.')
    if 'sha256:' + sha(chart_bytes) != release['chart']['package_digest']:
        raise RelocationError('Original chart package does not match the publisher release.')
    root, original = _archive(chart_bytes)
    files = deepcopy(original)
    chart_path, values_path, schema_path = (root + '/' + name for name in ('Chart.yaml', 'values.yaml', 'values.schema.json'))
    try:
        # The reviewed Core producer emits JSON values and schema. Other chart
        # dialects require a separate adapter rather than a generic YAML rewrite.
        values = _json(original[values_path][0])
        schema = _json(original[schema_path][0])
        chart = original[chart_path][0].decode('utf-8')
        metadata = yaml.safe_load(chart)
        if metadata['name'] != root or str(metadata['version']) != release['chart']['version'] or values['images'] != release['images']:
            raise ValueError
        if len(re.findall(r'(?m)^version:[^\n]*$', chart)) != 1:
            raise ValueError
        for name, reference in release['images'].items():
            if schema['properties']['images']['properties'][name]['const'] != reference:
                raise ValueError
            schema['properties']['images']['properties'][name]['const'] = mirrored_images[name]
        values['images'] = dict(mirrored_images)
        changes = {chart_path: re.sub(r'(?m)^version:[^\n]*$', 'version: ' + derived_version, chart).encode(),
            values_path: json.dumps(values, sort_keys=True, indent=2).encode() + b'\n',
            schema_path: json.dumps(schema, sort_keys=True, indent=2).encode() + b'\n'}
        release_path = root + '/release.json'
        if release_path in files:
            embedded = _json(files[release_path][0])
            if embedded['images'] != release['images']:
                raise ValueError
            embedded['images'] = dict(mirrored_images)
            changes[release_path] = json.dumps(embedded, sort_keys=True, indent=2).encode() + b'\n'
    except (ValueError, TypeError, KeyError, UnicodeError, yaml.YAMLError):
        raise RelocationError('Chart identity or image constraints do not match the published Core contract.') from None
    for name, content in changes.items():
        files[name] = (content, original[name][1], False)
    derived = _pack(files)
    unchanged = {name: {'sha256': sha(data), 'mode': mode} for name, (data, mode, directory) in original.items()
        if not directory and name not in changes}
    payload = {'schema_version': 1, 'operation': 'relocate-core-chart', 'original_release_sha256': release_hash,
        'publisher_spki_sha256': fingerprint, 'original_chart_oci_digest': release['chart']['oci_digest'],
        'original_chart_package_digest': release['chart']['package_digest'],
        'derived_chart_package_digest': 'sha256:' + sha(derived), 'original_chart_version': release['chart']['version'],
        'derived_chart_version': derived_version, 'target_origin': target_origin, 'customer_registry': customer_registry,
        'original_images': release['images'], 'mirrored_images': mirrored_images, 'changed_files': sorted(changes),
        'unchanged_files_sha256': sha(canonical_json(unchanged))}
    canonical_json(payload)
    return derived, payload


def verify_derivation(release_bytes, signature, publisher_key_pem, *, expected_publisher_key_sha256,
        expected_release_hash, original_chart_bytes, derived_chart_bytes, envelope, expected_customer_public_key,
        expected_target_origin, expected_mirrored_images, expected_customer_registry):
    try:
        payload = verify_document(envelope, expected_customer_public_key, 'chart-relocation')
        release, fingerprint = verified_release(release_bytes, signature, publisher_key_pem,
            expected_publisher_key_sha256, expected_release_hash)
        if (payload['target_origin'] != expected_target_origin or payload['mirrored_images'] != expected_mirrored_images
                or payload['customer_registry'] != expected_customer_registry):
            raise ValueError
        expected_bytes, expected_payload = _derive(release, original_chart_bytes, payload['mirrored_images'],
            payload['customer_registry'], payload['target_origin'], payload['derived_chart_version'], fingerprint, expected_release_hash)
        if expected_bytes != derived_chart_bytes or canonical_json(payload) != canonical_json(expected_payload):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError):
        raise RelocationError('Customer derivation signature, approved identity, or exact chart changes are invalid.') from None
