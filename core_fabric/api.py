from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone

from identity.access import decision, effective_tags, record_decision, require_access
from platform_services.api import body, response, workload_api
from platform_services.data import publish
from platform_services.models import Installation, WorkloadCredential
from platform_services.services import credential_allows
from .models import FabricContract, FabricPermit
from .services import (approved, authorize_consumer, canonical, capability, consume, digest, fingerprint, identity,
    matches, permit_principal, terms, validate_operation)
from .transport import FabricUnavailable, request_peer


def document(contract):
    return {'id': str(contract.pk), 'name': contract.name, 'direction': contract.direction,
        'terms': terms(contract), 'terms_hash': fingerprint(contract), 'locally_approved': bool(contract.approved_hash == fingerprint(contract) and contract.approved_at)}


@workload_api(['fabric.read'])
def contracts(request):
    rows = []
    for item in FabricContract.objects.filter(organization=request.organization, direction='import', active=True).select_related('peer'):
        try:
            authorize_consumer(request.access_principal, item, 'read')
        except PermissionDenied:
            continue
        rows.append(document(item))
        if len(rows) == 100:
            break
    return response({'organization_id': str(request.organization.pk), 'contracts': rows})


@workload_api(['fabric.read'])
def records(request, pk):
    contract = get_object_or_404(FabricContract, organization=request.organization, pk=pk)
    try:
        values = {'after': int(request.GET.get('after', 0)), 'limit': int(request.GET.get('limit', 100))}
    except ValueError:
        raise ValidationError('Use integer pagination.')
    try:
        return response(consume(request.access_principal, contract, 'read', values))
    except FabricUnavailable:
        return response({'error': 'federation_unavailable'}, 503)


@workload_api(['fabric.write'], methods=('POST',))
def write(request, pk):
    contract = get_object_or_404(FabricContract, organization=request.organization, pk=pk)
    try:
        result = consume(request.access_principal, contract, 'write', body(request))
        return response(result, 201 if result['created'] else 200)
    except FabricUnavailable:
        return response({'error': 'federation_unavailable'}, 503)


def bridge(request, contract):
    principal = request.access_principal
    if (not request.credential or principal.agent_binding or
            request.credential.installation_id != contract.peer.inbound_installation_id or
            request.headers.get('X-Core-Fabric-Issuer') != contract.peer.issuer or principal.purpose != contract.purpose_code):
        raise PermissionDenied('The peer application and issuer must match the agreement.')
    approved(contract)
    capability(principal, request.organization, 'fabric.peer')
    require_access(principal, contract, 'fabric.peer_access')


@workload_api(['fabric.peer'], methods=('POST',))
def verify_permit(request, pk):
    permit = get_object_or_404(FabricPermit.objects.select_related('contract'), pk=pk, contract__organization=request.organization)
    contract = permit.contract
    bridge(request, contract)
    values = body(request)
    if (set(values) != {'agreement_id', 'terms_hash', 'operation', 'request_hash', 'purpose_code'} or
            contract.direction != 'import' or str(contract.agreement_id) != values['agreement_id'] or
            permit.terms_hash != values['terms_hash'] or permit.operation != values['operation'] or
            permit.request_hash != values['request_hash'] or values['purpose_code'] != contract.purpose_code or
            permit.expires_at <= timezone.now() or permit.terms_hash != fingerprint(contract)):
        raise PermissionDenied('The recipient must authorize this exact current operation.')
    principal = permit_principal(permit)
    authorize_consumer(principal, contract, permit.operation)
    record_decision(principal, contract, 'fabric.permit_verified', True, 'live_recipient_authority', effective_tags(contract))
    return response({'issuer': settings.ISSUER, 'organization_id': str(request.organization.pk), 'permit_id': str(permit.pk),
        'agreement_id': str(contract.agreement_id), 'terms_hash': permit.terms_hash, 'request_hash': permit.request_hash,
        'operation': permit.operation, 'actor': identity(principal)})


def export_operation(request, agreement_id, operation):
    contract = get_object_or_404(FabricContract, organization=request.organization, agreement_id=agreement_id, direction='export')
    bridge(request, contract)
    envelope = body(request)
    if set(envelope) != {'permit_id', 'terms_hash', 'purpose_code', 'request'} or envelope['purpose_code'] != contract.purpose_code:
        raise ValidationError('Use an operation-bound recipient permit.')
    from uuid import UUID
    try:
        permit_id = str(UUID(envelope['permit_id']))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Use a valid recipient permit UUID.')
    values = envelope['request']
    if not isinstance(values, dict):
        raise ValidationError('Use an operation request object.')
    validate_operation(contract, operation, values)
    if not getattr(contract, 'allow_' + operation) or envelope['terms_hash'] != fingerprint(contract):
        raise PermissionDenied('Both administrators must approve identical current terms.')
    verified = request_peer(contract.peer, f'permits/{permit_id}/verify/', {'agreement_id': str(contract.agreement_id),
        'terms_hash': envelope['terms_hash'], 'operation': operation, 'request_hash': digest(values), 'purpose_code': contract.purpose_code})
    if (verified.get('permit_id') != permit_id or verified.get('agreement_id') != str(contract.agreement_id) or
            verified.get('terms_hash') != envelope['terms_hash'] or verified.get('request_hash') != digest(values) or
            verified.get('operation') != operation or not isinstance(verified.get('actor'), dict)):
        raise FabricUnavailable('Recipient returned an invalid authorization proof.')
    actor = verified['actor']
    if set(actor) != {'kind', 'id'} or actor['kind'] not in {'application', 'agent', 'user'}:
        raise FabricUnavailable('Recipient returned an invalid actor.')
    try:
        UUID(actor['id'])
    except (ValueError, TypeError, AttributeError):
        raise FabricUnavailable('Recipient returned an invalid actor.')
    # Recheck source authority after the recipient callback and before any read/write.
    contract = FabricContract.objects.get(pk=contract.pk)
    bridge(request, contract)
    if fingerprint(contract) != envelope['terms_hash']:
        raise PermissionDenied('Agreement terms changed during federation.')
    current = WorkloadCredential.objects.select_related('installation__organization', 'installation__package', 'installation__service').get(pk=request.credential.pk)
    if not credential_allows(current, ['fabric.peer', 'data.' + operation]):
        raise PermissionDenied('Source data authority changed during federation.')
    require_access(request.access_principal, current, 'fabric.source_credential')
    data_service = Installation.objects.select_related('service').get(organization=request.organization, package__slug='core-data')
    require_access(request.access_principal, data_service.service.permissions.get(codename='data-' + operation), 'fabric.source_data_permission')
    provenance = {'agreement_id': str(contract.agreement_id), 'terms_hash': fingerprint(contract),
        'issuer': contract.peer.issuer, 'organization_id': str(contract.peer.remote_organization_id), 'actor': actor,
        'purpose_code': contract.purpose_code}
    common = {'issuer': settings.ISSUER, 'organization_id': str(request.organization.pk),
        'agreement_id': str(contract.agreement_id), 'terms_hash': fingerprint(contract), 'dataset_id': str(contract.dataset_id)}
    if operation == 'write':
        record, created = publish(contract.dataset, 'fabric:' + digest({'agreement_id': str(contract.agreement_id),
            'actor': actor, 'event_id': values['event_id']}), values['data'], federation=provenance)
        result = {**common, 'record_id': record.pk, 'created': created, 'synthetic': record.synthetic}
    else:
        rows, cursor, size = [], values['after'], 0
        for record in contract.dataset.records.filter(pk__gt=values['after'])[:1000]:
            if matches(contract, record.data):
                row = {'id': record.pk, 'data': {k: v for k, v in record.data.items() if k in contract.fields},
                    'synthetic': record.synthetic, 'training_run_id': str(record.training_run_id) if record.training_run_id else None,
                    'federation': record.federation, 'created': record.created.isoformat()}
                length = len(canonical(row).encode())
                if size + length > 3 * 1024 * 1024:
                    if not rows:
                        raise ValidationError('A source record exceeds the federation page bound.')
                    break
                rows.append(row)
                size += length
            cursor = record.pk
            if len(rows) == values['limit']:
                break
        result = {**common, 'records': rows, 'next_cursor': cursor}
    record_decision(request.access_principal, contract, 'fabric.export_' + operation, True, 'bilateral_operation_verified', effective_tags(contract))
    return response(result, 201 if operation == 'write' and result['created'] else 200)


@workload_api(['fabric.peer', 'data.read'], methods=('POST',))
def export_read(request, agreement_id):
    try:
        return export_operation(request, agreement_id, 'read')
    except FabricUnavailable:
        return response({'error': 'recipient_approval_unavailable'}, 503)


@workload_api(['fabric.peer', 'data.write'], methods=('POST',))
def export_write(request, agreement_id):
    try:
        return export_operation(request, agreement_id, 'write')
    except FabricUnavailable:
        return response({'error': 'recipient_approval_unavailable'}, 503)
