"""Unified retrieval uses the requesting human's live authority, never a service account."""
from dataclasses import replace
import json
from time import monotonic
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from core_fabric.services import authorize_consumer, canonical, consume, digest, fingerprint, matches
from core_fabric.transport import FabricUnavailable
from identity.access import decision, effective_tags, record_decision, require_access
from identity.models import Application, Membership, ProtectedResource, User
from identity.policy import authorization_for
from platform_services import ai_rules
from platform_services.documentation import require_service_permissions
from platform_services.models import DeploymentEnvironment, WorkloadCredential
from platform_services.training import require_capability
from .models import QuerySource
from .providers import status

MAX_SOURCES = 10
MAX_RECORD_BYTES = 32768


def fresh(principal):
    """Refresh mutable authority after network calls; retain verified auth evidence."""
    return replace(principal,
        user=User.objects.get(pk=principal.user.pk) if principal.user else None,
        credential=WorkloadCredential.objects.select_related('installation').get(pk=principal.credential.pk) if principal.credential else None,
        application=Application.objects.select_related('service').get(pk=principal.application.pk) if principal.application else None)


def principals(reader, caller=None):
    return [reader, caller] if caller and caller is not reader else [reader]


def entry(reader, organization, caller=None):
    if not reader.user or reader.credential or reader.agent_binding:
        raise PermissionDenied('A signed-in user or backend-delegated user token is required.')
    for principal in principals(reader, caller):
        require_capability(principal, organization, ['ai.query'])
        require_access(principal, organization, 'ai.query_access')
    if reader.application and (not reader.application.active or not reader.application.service.active or
            reader.application.service.organization_id != organization.pk):
        raise PermissionDenied('The calling application is unavailable.')
    if reader.application:
        membership = Membership.objects.filter(organization=organization, user=reader.user, active=True).first()
        permissions = [name for name in authorization_for(membership, reader.application.service, reader)['permissions']
            if name != 'admin'] if membership else []
        if not permissions:
            raise PermissionDenied('Calling application access has been revoked.')
        require_service_permissions(reader, reader.application.service, permissions)


def authorize(reader, source, caller=None):
    entry(reader, source.organization, caller)
    source.full_clean()
    if (not source.active or not source.environment.active or not source.installation.enabled or
            not source.installation.service.active or not source.installation.package.active or
            source.dataset_id and not source.dataset.active):
        raise PermissionDenied('Source unavailable.')
    membership = Membership.objects.filter(organization=source.organization, user=reader.user, active=True).first()
    if not membership:
        raise PermissionDenied('Membership required.')
    if membership.kind != 'administrator' and not reader.user.is_superuser:
        roles = membership.roles.filter(organization=source.organization, pk__in=source.allowed_roles.values('pk'))
        if not any(decision(reader, role)[0] for role in roles):
            raise PermissionDenied('An approved query role is required.')
    require_service_permissions(reader, source.installation.service, source.required_permissions)
    for principal in principals(reader, caller):
        require_access(principal, source, 'ai.source_read')
        require_capability(principal, source.organization, ['fabric.read' if source.contract_id else 'data.read'])
        if source.contract_id:
            authorize_consumer(principal, source.contract, 'read')
    if caller:
        if not caller.credential or not source.allowed_consumers.filter(pk=caller.credential.installation_id,
                organization=source.organization, enabled=True).exists():
            raise PermissionDenied('The consuming application must be explicitly approved.')


def descriptor(source):
    return {'id': str(source.pk), 'name': source.name, 'application': source.installation.service.name,
        'application_id': str(source.installation.pk), 'environment': source.environment.name,
        'kind': 'federated_dataset' if source.contract_id else 'dataset', 'fields': source.fields,
        'purpose_code': source.contract.purpose_code if source.contract_id else '',
        'data_class': source.contract.data_class if source.contract_id else source.dataset.data_class}


def scope_digest(source):
    return digest({'installation': str(source.installation_id), 'environment': str(source.environment_id),
        'dataset': str(source.dataset_id), 'contract': fingerprint(source.contract) if source.contract_id else None,
        'fields': source.fields, 'filters': source.filters, 'permissions': source.required_permissions,
        'record_resource_type': source.record_resource_type})


def catalog(reader, organization, caller=None):
    entry(reader, organization, caller)
    result = []
    for source in QuerySource.objects.filter(organization=organization, active=True).select_related(
            'installation__service', 'installation__package', 'environment', 'dataset', 'contract__peer').order_by('name', 'pk'):
        try:
            authorize(reader, source, caller)
        except (PermissionDenied, ValidationError):
            continue
        result.append(descriptor(source))
        if len(result) == 100:
            break
    return {'version': 1, 'organization_id': str(organization.pk), 'sources': result, 'provider': status()}


def validate_query(values):
    if not isinstance(values, dict) or set(values) - {'sources', 'text', 'cursors', 'limit'}:
        raise ValidationError('Use sources, text, cursors, and limit only.')
    selected = values.get('sources', [])
    if not isinstance(selected, list) or len(selected) > MAX_SOURCES:
        raise ValidationError('Choose at most ten sources.')
    try:
        if any(not isinstance(value, str) for value in selected):
            raise ValueError
        selected = [str(uuid.UUID(value)) for value in selected]
    except (ValueError, AttributeError, TypeError):
        raise ValidationError('Use source UUIDs.')
    if len(set(selected)) != len(selected):
        raise ValidationError('Choose distinct sources.')
    text, cursors, limit = values.get('text', ''), values.get('cursors', {}), values.get('limit', 100)
    if not isinstance(text, str) or len(text) > 200 or any(ord(char) < 32 for char in text):
        raise ValidationError('Use at most 200 characters of literal search text.')
    if (not isinstance(cursors, dict) or len(cursors) > MAX_SOURCES or any(key not in selected or
            type(value) is not int or not 0 <= value <= 10**12 for key, value in cursors.items())):
        raise ValidationError('Cursors must be bounded integers for selected sources.')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValidationError('Use an inspection budget from 1 to 200.')
    return selected, text.strip().casefold(), cursors, limit


def record_access(reader, source, record_id, caller=None):
    if not source.record_resource_type:
        return True
    resource = ProtectedResource.objects.filter(service=source.installation.service,
        resource_type=source.record_resource_type, external_id=str(record_id), active=True).first()
    if resource is None:
        return False
    placements = set(DeploymentEnvironment.objects.filter(organization=source.organization,
        boundary_tag_id__in=[tag.pk for tag in effective_tags(resource)]).values_list('pk', flat=True))
    return placements == {source.environment_id} and all(decision(principal, resource)[0] for principal in principals(reader, caller))


def evidence(source, row):
    authority = str(source.contract.peer.remote_organization_id) if source.contract_id else str(source.organization_id)
    return {'citation_id': f'{source.pk}:{row["id"]}', 'source_id': str(source.pk), 'source': source.name,
        'application': source.installation.service.name, 'application_id': str(source.installation.pk),
        'environment': source.environment.name, 'authority_organization_id': authority,
        'dataset_id': str(source.contract.remote_dataset_id if source.contract_id else source.dataset_id),
        'record_id': row['id'], 'synthetic': row['synthetic'], 'created': row.get('created'),
        'data': {field: row['data'][field] for field in source.fields if field in row['data']},
        'agreement_id': str(source.contract.agreement_id) if source.contract_id else None}


def fetch(source, reader, after, limit, remaining_ms):
    if source.contract_id:
        value = consume(reader, source.contract, 'read', {'after': after, 'limit': min(limit, 100)}, timeout_ms=remaining_ms)
        return value['records'], value['next_cursor'], len(value['records']), value['next_cursor'] > after
    # The dataset/id index bounds retrieval. Text and policy filters only inspect
    # this page; there is no unbounded database substring scan or generated SQL.
    with transaction.atomic():
        connection.ensure_connection()
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(remaining_ms)])
        if connection.vendor == 'sqlite':
            deadline = monotonic() + remaining_ms / 1000
            connection.connection.set_progress_handler(lambda: int(monotonic() > deadline), 100)
        try:
            rows = list(source.dataset.records.filter(pk__gt=after).order_by('pk')[:limit])
        finally:
            if connection.vendor == 'sqlite':
                connection.connection.set_progress_handler(None, 0)
    return [{'id': row.pk, 'data': row.data, 'synthetic': row.synthetic, 'created': row.created.isoformat()} for row in rows], (
        rows[-1].pk if rows else after), len(rows), len(rows) == limit


def query(reader, organization, values, caller=None, *, revalidate=None):
    selected, text, cursors, requested = validate_query(values)
    available = catalog(reader, organization, caller)['sources']
    visible_ids = {value['id'] for value in available}
    if selected and set(selected) - visible_ids:
        raise PermissionDenied('One or more requested sources is unavailable.')
    selected = selected or [value['id'] for value in available]
    if len(selected) > MAX_SOURCES:
        raise ValidationError('Select at most ten sources explicitly.')
    sources = {str(source.pk): source for source in QuerySource.objects.filter(organization=organization, pk__in=selected)}
    snapshots = {pk: scope_digest(source) for pk, source in sources.items()}
    if not selected:
        return result(organization, [], [], {}, 0, 0)
    # Admission commits before I/O. A failure never resets the attempted request's meter.
    with transaction.atomic():
        policy = ai_rules.locked_policy(organization)
        ai_rules.authorize(policy, reader, caller, internal=True)
        plan = ai_rules.resolved(policy, caller or reader)['query']
        budget = min(requested, plan['max_rows'])
        if budget < len(selected):
            raise ValidationError('The inspection budget must cover at least one row per source.')
        reservation = ai_rules.reserve(organization, reader, {'request_key': uuid.uuid4().hex, 'operation': 'query',
            'estimated': {'requests': 1, 'query_rows': budget}, 'query': {'rows': budget, 'timeout_ms': plan['timeout_ms'],
                'paginated': True, 'indexed_filters': True}}, source=caller, internal=True)
        ai_rules.transition(organization, reader, reservation.pk, 'start', source=caller, internal=True)
    deadline = monotonic() + plan['timeout_ms'] / 1000
    rows, pages, next_cursors, inspected = [], [], {}, 0
    try:
        for index, pk in enumerate(selected):
            source, after = sources[pk], cursors.get(pk, 0)
            allocation = min(100, (budget - inspected) // (len(selected) - index))
            source_status, candidates, cursor, more = 'ok', [], after, True
            remaining = int((deadline - monotonic()) * 1000)
            if remaining < 1:
                source_status = 'timeout'
            else:
                try:
                    authorize(fresh(reader), source, fresh(caller) if caller else None)
                    candidates, cursor, count, more = fetch(source, reader, after, allocation, remaining)
                    inspected += count
                    if monotonic() >= deadline:
                        candidates, cursor, source_status = [], after, 'timeout'
                except FabricUnavailable:
                    source_status = 'unavailable'
                except PermissionDenied:
                    source_status = 'access_changed'
                except ValidationError:
                    source_status = 'configuration_changed'
            source_rows = []
            for row in candidates:
                if not matches(source, row['data']) or not record_access(reader, source, row['id'], caller):
                    continue
                item = evidence(source, row)
                if text and text not in json.dumps(item['data'], ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), allow_nan=False).casefold():
                    continue
                if len(canonical(item).encode()) > MAX_RECORD_BYTES:
                    source_status, cursor, more = 'record_too_large', after, True
                    source_rows = []
                    break
                source_rows.append(item)
            rows.extend(source_rows)
            next_cursors[pk] = cursor
            pages.append({'source_id': pk, 'status': source_status, 'has_more': more,
                'search': 'literal_match_within_page'})
        # Check every earlier source again: a later remote call may revoke it.
        reader, caller = fresh(reader), fresh(caller) if caller else None
        if revalidate and not revalidate():
            raise PermissionDenied('The delegated user session is no longer valid.')
        entry(reader, organization, caller)
        for pk in selected:
            current = QuerySource.objects.get(pk=pk)
            try:
                authorize(reader, current, caller)
                if scope_digest(current) != snapshots[pk]:
                    raise PermissionDenied
            except (PermissionDenied, ValidationError):
                rows = [row for row in rows if row['source_id'] != pk]
                next_cursors[pk] = cursors.get(pk, 0)
                for page in pages:
                    if page['source_id'] == pk:
                        page.update(status='access_changed', has_more=True)
            else:
                rows = [row for row in rows if row['source_id'] != pk or record_access(reader, current, row['record_id'], caller)]
        record_decision(reader, organization, 'ai.query_completed', True, 'authorized_evidence_only', effective_tags(organization))
        return result(organization, rows, pages, next_cursors, inspected, budget)
    finally:
        # No prompts, rows, or provider keys are stored in usage or audit records.
        ai_rules.settle_core_query(reservation, inspected)


def result(organization, rows, pages, cursors, inspected, budget):
    return {'version': 1, 'organization_id': str(organization.pk), 'evidence': rows, 'sources': pages,
        'next_cursors': cursors, 'partial': any(page['status'] != 'ok' for page in pages),
        'has_more': any(page['has_more'] for page in pages),
        'usage': {'inspected_rows': inspected, 'effective_budget': budget, 'tokens': 0, 'cost_micros': 0},
        'conversation': {'mode': 'evidence_only', 'answer': None,
            'notice': 'Literal search of approved fields within each inspected page. Follow cursors for more evidence. AI generation is not configured.'},
        'provider': status()}
