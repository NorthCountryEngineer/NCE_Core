"""Bounded, tenant-local robot people registry. Never logs biometric payloads."""
import base64
from copy import deepcopy
from datetime import timedelta, timezone as datetime_timezone
import hashlib
import json
import math
import re
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from identity.models import Organization
from . import edge
from .edge_models import EdgeDevice
from .models import DataRecord, Dataset

SLUG = 'nce-robotics-people'
MARKER = 'robotics-people-v1'
MAX_PEOPLE = 500  # Includes tombstones: deletion cannot create unbounded history.
MAX_SAMPLES = 3
MAX_JPEG = 32 * 1024
MAX_SIDE = 320
MAX_UPDATES = 5
PAGE_SIZE = 50
MAX_REQUEST = 1024 * 1024
SEEN_INTERVAL = timedelta(hours=1)


class RegistryFull(edge.Conflict):
    pass


def request_body(request, limit=MAX_REQUEST):
    if request.content_type != 'application/json':
        raise ValidationError('Use an application/json body.')
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > limit:
            raise ValidationError('Request exceeds the registry size limit.')
        raw = request.read(limit + 1)
        if len(raw) > limit:
            raise ValidationError('Request exceeds the registry size limit.')
        def pairs(rows):
            value = {}
            for key, item in rows:
                if key in value:
                    raise ValueError('Duplicate key')
                value[key] = item
            return value
        def constant(_):
            raise ValueError('Non-finite JSON')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError('Use a valid JSON object.') from None
    if not isinstance(value, dict):
        raise ValidationError('Use a JSON object.')
    return value


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValidationError('Unexpected or missing registry fields.')


def identifier(value):
    if not isinstance(value, str):
        raise ValidationError('Use a UUID identifier.')
    try:
        return str(UUID(value))
    except ValueError:
        raise ValidationError('Use a UUID identifier.') from None


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValidationError('Use an ISO timestamp with a timezone.')
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError('Use an ISO timestamp with a timezone.')
    if parsed > timezone.now() + timedelta(minutes=5):
        raise ValidationError('Observation timestamp is too far in the future.')
    return parsed.astimezone(datetime_timezone.utc).isoformat()


def name_value(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValidationError('Use a name of 1–80 characters without control characters.')
    return value.strip()


def cursor_value(value):
    if value in (None, ''):
        return 0
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,19}', str(value)):
        raise ValidationError('Use a nonnegative registry cursor.')
    result = int(value)
    if result > 2**63 - 1:
        raise ValidationError('Registry cursor exceeds the supported range.')
    return result


def jpeg_dimensions(data):
    """Read bounded JPEG framing without decoding pixels or accepting other formats."""
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValidationError('Sample must be a complete JPEG.')
    offset, dimensions = 2, None
    while offset < len(data) - 2:
        if data[offset] != 255:
            raise ValidationError('Invalid JPEG framing.')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            raise ValidationError('Invalid JPEG marker.')
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset:offset+2], 'big')
        if size < 2 or offset + size > len(data):
            raise ValidationError('Invalid JPEG segment size.')
        if marker in (0xc0, 0xc1, 0xc2):
            segment = data[offset+2:offset+size]
            if len(segment) < 6 or segment[0] != 8 or segment[5] not in (1, 3):
                raise ValidationError('Unsupported JPEG sample.')
            if len(segment) != 6 + 3 * segment[5] or dimensions is not None:
                raise ValidationError('Invalid JPEG dimensions.')
            height, width = int.from_bytes(segment[1:3], 'big'), int.from_bytes(segment[3:5], 'big')
            if not 1 <= width <= MAX_SIDE or not 1 <= height <= MAX_SIDE:
                raise ValidationError('Sample dimensions exceed 320 pixels.')
            dimensions = (width, height)
        if marker == 0xda:
            if dimensions is None or offset + size >= len(data) - 2:
                raise ValidationError('JPEG sample has no scan or dimensions.')
            return dimensions
        offset += size
    raise ValidationError('JPEG sample has no complete image header.')


def sample_value(value):
    fields(value, {'id', 'jpeg_base64', 'sha256', 'width', 'height'},
           {'id', 'jpeg_base64', 'sha256', 'width', 'height'})
    sample_id = identifier(value['id'])
    encoded, digest = value['jpeg_base64'], value['sha256']
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_JPEG + 2) // 3):
        raise ValidationError('JPEG sample exceeds 32 KiB.')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValidationError('Use a lowercase SHA-256 digest.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValidationError('Invalid JPEG encoding.') from None
    if not 1 <= len(data) <= MAX_JPEG or hashlib.sha256(data).hexdigest() != digest:
        raise ValidationError('JPEG size or digest did not match.')
    dimensions = jpeg_dimensions(data)
    if any(type(value[key]) is not int for key in ('width', 'height')) or dimensions != (value['width'], value['height']):
        raise ValidationError('JPEG dimensions did not match.')
    return {'id': sample_id, 'sha256': digest, 'width': dimensions[0], 'height': dimensions[1],
            'jpeg_base64': base64.b64encode(data).decode()}


def person_update(value):
    fields(value, {'id', 'first_seen', 'last_seen', 'samples', 'embeddings', 'embedding_model', 'legacy_name'},
           {'id', 'first_seen', 'last_seen', 'embeddings', 'embedding_model'})
    person_id = identifier(value['id'])
    first, last = timestamp(value['first_seen']), timestamp(value['last_seen'])
    if parse_datetime(first) > parse_datetime(last):
        raise ValidationError('First seen cannot follow last seen.')
    if value['embedding_model'] != 'sface-v1':
        raise ValidationError('Unsupported embedding model.')
    samples = value.get('samples', [])
    vectors = value['embeddings']
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise ValidationError('Use at most three samples per person.')
    samples = [sample_value(sample) for sample in samples]
    if len({sample['id'] for sample in samples}) != len(samples):
        raise ValidationError('Duplicate sample identifier.')
    if not isinstance(vectors, list) or len(vectors) > MAX_SAMPLES:
        raise ValidationError('Use at most three embeddings per person.')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != 128
                or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 1e6 for x in vector)):
            raise ValidationError('Use finite 128-value embeddings.')
        if not .98 <= sum(x*x for x in vector) <= 1.02:
            raise ValidationError('Embeddings must have nonzero unit length.')
    result = {'id': person_id, 'first_seen': first, 'last_seen': last, 'samples': samples,
              'embeddings': vectors, 'embedding_model': 'sface-v1'}
    if 'legacy_name' in value:
        result['legacy_name'] = name_value(value['legacy_name'])
    return result


def registry(installation, *, create=False, lock=False):
    if create:
        # Serialize first creation and the organization-wide 500-record cap.
        Organization.objects.select_for_update().get(pk=installation.organization_id)
    rows = Dataset.objects.filter(organization_id=installation.organization_id, slug=SLUG)
    if lock:
        rows = rows.select_for_update()
    dataset = rows.first()
    schema = {'type': 'object', 'x-nce-managed': MARKER, 'x-nce-installation-id': str(installation.pk)}
    if dataset is None and create:
        dataset = Dataset.objects.create(organization_id=installation.organization_id,
            slug=SLUG, name='NCE Robotics people (private managed state)', schema=schema)
    if dataset is not None and (not dataset.active or dataset.data_class != 'operational' or dataset.schema != schema):
        raise edge.Conflict('The reserved people registry has incompatible ownership or configuration.')
    return dataset


def person_values(data, *, agent=False):
    value = {key: data[key] for key in ('id', 'name', 'revision', 'first_seen', 'last_seen',
                                      'last_synced_at', 'source_device_id', 'named', 'deleted')}
    value['samples'] = [{key: sample[key] for key in ('id', 'sha256', 'width', 'height')}
                        for sample in data['samples']]
    value['face_ready'] = bool(data['embeddings'])
    if agent:
        value.update(embeddings=data['embeddings'], embedding_model=data['embedding_model'])
    return value


def authorize_record(installation, principal, record, permission='device-read'):
    if record.data.get('installation_id') != str(installation.pk):
        raise PermissionDenied
    source = EdgeDevice.objects.filter(pk=record.data['source_device_id'], installation=installation).first()
    if source is None:
        raise PermissionDenied
    edge.authorize(principal, installation, permission, resource=source)


def list_people(installation, principal, after=None, limit=PAGE_SIZE):
    edge.authorize(principal, installation)
    after = cursor_value(after)
    if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValidationError('Use a page limit from 1 to 50.')
    dataset = registry(installation)
    if dataset is None:
        return {'people': [], 'next_cursor': None, 'limit': limit, 'max_limit': PAGE_SIZE}
    rows = list(dataset.records.filter(pk__gt=after).order_by('pk')[:limit+1])
    people = []
    for row in rows[:limit]:
        try:
            authorize_record(installation, principal, row)
        except PermissionDenied:
            continue
        if not row.data['deleted']:
            people.append(person_values(row.data))
    return {'people': people, 'next_cursor': rows[limit-1].pk if len(rows) > limit else None,
            'limit': limit, 'max_limit': PAGE_SIZE}


def record_for(installation, person_id, *, lock=False):
    dataset = registry(installation, lock=lock)
    rows = DataRecord.objects.filter(dataset=dataset, event_id=str(person_id)) if dataset else DataRecord.objects.none()
    if lock:
        rows = rows.select_for_update()
    record = rows.first()
    if record is None:
        raise Http404
    return record


def sample_bytes(installation, principal, person_id, sample_id):
    record = record_for(installation, person_id)
    authorize_record(installation, principal, record)
    if record.data['deleted']:
        raise Http404
    sample = next((item for item in record.data['samples'] if item['id'] == str(sample_id)), None)
    if sample is None:
        raise Http404
    return base64.b64decode(sample['jpeg_base64']), sample['sha256']


@transaction.atomic
def rename(installation, principal, person_id, value):
    fields(value, {'name', 'expected_revision'}, {'name', 'expected_revision'})
    name = name_value(value['name'])
    expected = value['expected_revision']
    if type(expected) is not int or expected < 1:
        raise ValidationError('Use a positive expected revision.')
    record = record_for(installation, person_id, lock=True)
    authorize_record(installation, principal, record, 'device-manage')
    data = deepcopy(record.data)
    if data['deleted']:
        raise edge.Conflict('This person has been deleted.')
    if data['named'] and data['name'] == name:
        return person_values(data)
    if data['revision'] != expected:
        raise edge.Conflict('Person revision changed; refresh before renaming.')
    data.update(name=name, named=True, revision=data['revision']+1)
    record.data = data
    record.save(update_fields=['data'])
    edge.audit(installation, 'edge.person_renamed', principal=principal, target=person_id)
    return person_values(data)


@transaction.atomic
def delete(installation, principal, person_id, value):
    fields(value, {'expected_revision'}, {'expected_revision'})
    if type(value['expected_revision']) is not int or value['expected_revision'] < 1:
        raise ValidationError('Use a positive expected revision.')
    record = record_for(installation, person_id, lock=True)
    authorize_record(installation, principal, record, 'device-manage')
    data = deepcopy(record.data)
    if data['deleted']:
        return person_values(data)
    if data['revision'] != value['expected_revision']:
        raise edge.Conflict('Person revision changed; refresh before deleting.')
    data.update(deleted=True, named=False, name='Unknown-'+data['id'], samples=[], embeddings=[],
                revision=data['revision']+1)
    record.data = data
    record.save(update_fields=['data'])
    edge.audit(installation, 'edge.person_deleted', principal=principal, target=person_id)
    return person_values(data)


def merge_update(record, update, device, now):
    if record.data['source_device_id'] != str(device.pk):
        raise edge.Conflict('Person identifier belongs to another device.')
    data = deepcopy(record.data)
    if data['deleted']:
        return data
    changed = False
    known = {sample['id']: sample for sample in data['samples']}
    for sample in update['samples']:
        if sample['id'] in known:
            if sample != known[sample['id']]:
                raise edge.Conflict('An existing sample cannot be replaced.')
        else:
            if len(data['samples']) == MAX_SAMPLES:
                raise ValidationError('Person sample capacity reached.')
            data['samples'].append(sample)
            changed = True
    for vector in update['embeddings']:
        # A device may round or renormalize the returned canonical vector.
        # Preserve its stored bytes and avoid consuming another sample slot.
        if not any(all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
                       for a, b in zip(vector, stored)) for stored in data['embeddings']):
            if len(data['embeddings']) == MAX_SAMPLES:
                raise ValidationError('Person embedding capacity reached.')
            data['embeddings'].append(vector)
            changed = True
    if now - parse_datetime(data['last_synced_at']) >= SEEN_INTERVAL:
        data['last_seen'] = max(parse_datetime(data['last_seen']), parse_datetime(update['last_seen'])).isoformat()
        data['last_synced_at'] = now.isoformat()
        changed = True
    if changed:
        data['revision'] += 1
        record.data = data
        record.save(update_fields=['data'])
    return data


@transaction.atomic
def sync(credential, value):
    fields(value, {'people', 'cursor'}, {'people'})
    updates = value['people']
    if not isinstance(updates, list) or len(updates) > MAX_UPDATES:
        raise ValidationError('Send at most five person updates.')
    updates = [person_update(item) for item in updates]
    if len({item['id'] for item in updates}) != len(updates):
        raise ValidationError('Duplicate person identifier in batch.')
    after = cursor_value(value.get('cursor'))
    device = EdgeDevice.objects.select_for_update().get(pk=credential.device_id)
    credential = edge.current_credential(credential)
    installation = credential.device.installation
    dataset = registry(installation, create=bool(updates), lock=True)
    if dataset is None:
        return {'accepted': [], 'people': [], 'next_cursor': None, 'has_more': False}
    now = timezone.now()
    accepted = []
    for update in updates:
        record = dataset.records.filter(event_id=update['id']).first()
        if record is None:
            if dataset.records.count() >= MAX_PEOPLE:
                raise RegistryFull('Organization people registry capacity reached.')
            legacy = update.get('legacy_name')
            data = {**update, 'name': legacy or 'Unknown-'+update['id'], 'named': bool(legacy),
                'revision': 1, 'source_device_id': str(device.pk), 'installation_id': str(installation.pk),
                'last_synced_at': now.isoformat(), 'deleted': False}
            data.pop('legacy_name', None)
            record = DataRecord.objects.create(dataset=dataset, event_id=update['id'], data=data)
            edge.audit(installation, 'edge.person_created', device=device, target=update['id'])
        else:
            merge_update(record, update, device, now)
        accepted.append(update['id'])
    rows = list(dataset.records.filter(pk__gt=after).order_by('pk')[:PAGE_SIZE+1])
    page = rows[:PAGE_SIZE]
    seen = {row.event_id for row in page}
    # Include accepted records immediately even when the paging cursor is later.
    page += list(dataset.records.filter(event_id__in=set(accepted)-seen).order_by('pk'))
    return {'accepted': accepted, 'people': [person_values(row.data, agent=True) for row in page],
            'next_cursor': rows[PAGE_SIZE-1].pk if len(rows) > PAGE_SIZE else None,
            'has_more': len(rows) > PAGE_SIZE}
