import hashlib
import json
import re
from datetime import timezone as utc
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from identity.access import Principal, effective_tags, record_decision, require_access
from identity.models import Membership, Organization
from identity.policy import authorization_for
from platform_services.agent_identity import require_capabilities
from platform_services.models import Installation, WorkloadCredential
from platform_services.services import credential_allows, feature_enabled
from .models import FabricContract, FabricPermit
from .transport import FabricUnavailable, request_peer, validate_origin, validate_peer


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def terms(contract):
    local = {'issuer': settings.ISSUER, 'organization_id': str(contract.organization_id)}
    remote = {'issuer': contract.peer.issuer, 'organization_id': str(contract.peer.remote_organization_id)}
    source, recipient = (local, remote) if contract.direction == 'export' else (remote, local)
    source = {**source, 'dataset_id': str(contract.dataset_id if contract.direction == 'export' else contract.remote_dataset_id),
        'data_class': contract.data_class}
    return {'version': 1, 'agreement_id': str(contract.agreement_id), 'source': source, 'recipient': recipient,
        'purpose_code': contract.purpose_code, 'fields': sorted(contract.fields) if isinstance(contract.fields, list) else contract.fields,
        'filters': contract.filters, 'allow_read': contract.allow_read, 'allow_write': contract.allow_write,
        'expires_at': contract.expires_at.astimezone(utc.utc).isoformat() if contract.expires_at else None}


def validate_terms(value):
    try:
        if (not isinstance(value, dict) or set(value) != {'version', 'agreement_id', 'source', 'recipient', 'purpose_code',
                'fields', 'filters', 'allow_read', 'allow_write', 'expires_at'} or type(value['version']) is not int or value['version'] != 1):
            raise ValueError
        UUID(value['agreement_id'])
        for side, keys in [('source', {'issuer', 'organization_id', 'dataset_id', 'data_class'}), ('recipient', {'issuer', 'organization_id'})]:
            if not isinstance(value[side], dict) or set(value[side]) != keys:
                raise ValueError
            validate_origin(value[side]['issuer'])
            UUID(value[side]['organization_id'])
        UUID(value['source']['dataset_id'])
        if value['source']['data_class'] not in {'synthetic', 'operational'} or value['source']['issuer'] == value['recipient']['issuer']:
            raise ValueError
        if (not isinstance(value['purpose_code'], str) or not re.fullmatch(r'[-A-Za-z0-9_]{1,80}', value['purpose_code']) or
                type(value['allow_read']) is not bool or type(value['allow_write']) is not bool or not (value['allow_read'] or value['allow_write'])):
            raise ValueError
        fields, filters = value['fields'], value['filters']
        if (not isinstance(fields, list) or not 1 <= len(fields) <= 40 or len(set(fields)) != len(fields) or
                not isinstance(filters, dict) or len(filters) > 20):
            raise ValueError
        for field in [*fields, *filters]:
            if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', field):
                raise ValueError
        if any(type(v) not in {str, int, float, bool, type(None)} for v in filters.values()) or len(canonical(value).encode()) > 16384:
            raise ValueError
        if value['expires_at'] is not None:
            from datetime import datetime
            if not datetime.fromisoformat(value['expires_at']).tzinfo:
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValidationError('Use bounded agreement terms with explicit fields, scalar row filters, matching identities, and read/write scope.')


def fingerprint(contract):
    return digest(terms(contract))


def capability(principal, organization, name):
    if not feature_enabled(organization, 'core-fabric'):
        raise PermissionDenied('Core Data Fabric is unavailable.')
    service = Installation.objects.select_related('service').get(organization=organization, package__slug='core-fabric')
    require_access(principal, service, 'fabric.service_access')
    if principal.agent_binding:
        if principal.agent_binding.organization_id != organization.pk:
            raise PermissionDenied
        require_capabilities(principal, [name])
    elif principal.credential:
        credential = WorkloadCredential.objects.select_related('installation__organization', 'installation__package', 'installation__service').get(pk=principal.credential.pk)
        if credential.installation.organization_id != organization.pk or not credential_allows(credential, [name]):
            raise PermissionDenied
        require_access(principal, credential, 'fabric.application_access')
    else:
        member = Membership.objects.filter(organization=organization, user=principal.user, active=True, user__is_active=True).first()
        if not member or name.replace('.', '-') not in authorization_for(member, service.service, principal)['permissions']:
            raise PermissionDenied
    require_access(principal, service.service.permissions.get(codename=name.replace('.', '-')), 'fabric.permission')


def current_administrator(user, organization):
    return bool(user and user.is_active and (user.is_superuser or Membership.objects.filter(
        organization=organization, user=user, active=True, kind='administrator').exists()))


def approved(contract):
    validate_peer(contract.peer)
    validate_terms(terms(contract))
    if (not contract.active or not contract.peer.active or not feature_enabled(contract.organization, 'core-fabric') or
            not contract.approved_at or not current_administrator(contract.approved_by, contract.organization) or
            contract.approved_hash != fingerprint(contract) or contract.expires_at and contract.expires_at <= timezone.now()):
        raise PermissionDenied('The current agreement requires an active administrator approval.')
    if contract.direction == 'export' and (not contract.dataset.active or not contract.dataset.organization.active or
            contract.dataset.organization_id != contract.organization_id or contract.dataset.data_class != contract.data_class):
        raise PermissionDenied('The authoritative dataset is unavailable or reclassified.')


def administer(principal, contract):
    if not current_administrator(principal.user, contract.organization) or principal.agent_binding or principal.credential:
        raise PermissionDenied('Only a current organization administrator can approve federation.')
    capability(principal, contract.organization, 'fabric.manage')
    require_access(principal, contract, 'fabric.administration')


@transaction.atomic
def approve(contract, principal, expected_hash):
    contract = FabricContract.objects.select_for_update().get(pk=contract.pk)
    administer(principal, contract)
    contract.full_clean()
    if expected_hash != fingerprint(contract) or not contract.active or not contract.peer.active:
        raise ValidationError('Review the current terms before approving.')
    contract.approved_by, contract.approved_at, contract.approved_hash = principal.user, timezone.now(), expected_hash
    contract.save()
    record_decision(principal, contract, 'fabric.approved', True, 'administrator_reviewed_terms', effective_tags(contract))
    return contract


@transaction.atomic
def revoke(contract, principal):
    contract = FabricContract.objects.select_for_update().get(pk=contract.pk)
    administer(principal, contract)
    contract.approved_by, contract.approved_at, contract.approved_hash = None, None, ''
    contract.save()
    record_decision(principal, contract, 'fabric.revoked', True, 'administrator_revocation', effective_tags(contract))


def authorize_consumer(principal, contract, operation):
    approved(contract)
    if contract.direction != 'import' or operation not in {'read', 'write'} or not getattr(contract, 'allow_' + operation):
        raise PermissionDenied('Operation is outside the approved agreement.')
    capability(principal, contract.organization, 'fabric.' + operation)
    if principal.purpose != contract.purpose_code:
        raise PermissionDenied('Use the approved purpose code.')
    require_access(principal, contract, 'fabric.' + operation)
    if principal.agent_binding:
        allowed = contract.allowed_bindings.filter(pk=principal.agent_binding.pk, account__organization=contract.organization).exists()
    elif principal.credential:
        allowed = contract.allowed_installations.filter(pk=principal.credential.installation_id, organization=contract.organization).exists()
    else:
        allowed = current_administrator(principal.user, contract.organization)
        if not allowed and operation == 'read':
            membership = contract.allowed_memberships.filter(organization=contract.organization,
                user=principal.user, active=True, user__is_active=True).first()
            allowed = bool(membership)
            if membership:
                require_access(principal, membership, 'fabric.member_read')
    if not allowed:
        raise PermissionDenied('This application or agent is not approved to consume the agreement.')


def validate_operation(contract, operation, values):
    if operation == 'read':
        if set(values) != {'after', 'limit'} or type(values['after']) is not int or values['after'] < 0 or type(values['limit']) is not int or not 1 <= values['limit'] <= 100:
            raise ValidationError('Use a nonnegative cursor and limit of 1–100.')
    elif operation == 'write':
        if (set(values) != {'event_id', 'data'} or not isinstance(values['event_id'], str) or not 1 <= len(values['event_id']) <= 160 or
                not isinstance(values['data'], dict) or set(values['data']) - set(contract.fields) or not matches(contract, values['data'])):
            raise ValidationError('Writes require an event ID and only approved fields matching the agreement row scope.')
    else:
        raise ValidationError('Use read or write.')
    try:
        if len(canonical(values).encode()) > 65536:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise ValidationError('Use finite JSON data bounded to 64 KiB.')


def matches(contract, data):
    return all(key in data and canonical(data[key]) == canonical(value) for key, value in contract.filters.items())


def permit_principal(permit):
    return Principal(user=permit.actor, credential=permit.credential, agent_binding=permit.agent_binding,
        agent_credential=permit.agent_credential, evidence=permit.evidence, application=permit.application, purpose=permit.contract.purpose_code)


def identity(principal):
    if principal.agent_binding:
        return {'kind': 'agent', 'id': str(principal.agent_binding.account_id)}
    return {'kind': 'application' if principal.credential else 'user',
        'id': str(principal.credential.installation_id if principal.credential else principal.user.pk)}


def consume(principal, contract, operation, values, *, timeout_ms=None):
    authorize_consumer(principal, contract, operation)
    validate_operation(contract, operation, values)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=contract.organization_id)
        FabricPermit.objects.filter(contract__organization=contract.organization, expires_at__lte=timezone.now()).delete()
        if FabricPermit.objects.filter(contract__organization=contract.organization).count() >= 100:
            raise ValidationError('Too many concurrent federation operations; retry shortly.')
        permit = FabricPermit.objects.create(contract=contract, operation=operation, request_hash=digest(values), terms_hash=fingerprint(contract),
            actor=principal.user, application=principal.application, credential=principal.credential, agent_binding=principal.agent_binding,
            agent_credential=principal.agent_credential, evidence=principal.evidence)
    try:
        options = {'timeout_ms': timeout_ms} if timeout_ms is not None else {}
        result = request_peer(contract.peer, f'exports/{contract.agreement_id}/{operation}/', {
            'permit_id': str(permit.pk), 'terms_hash': permit.terms_hash, 'purpose_code': contract.purpose_code, 'request': values}, **options)
        # Recheck local approvals after I/O too. Never return a revoked or mismatched response.
        contract = FabricContract.objects.get(pk=contract.pk)
        authorize_consumer(principal, contract, operation)
        if (fingerprint(contract) != permit.terms_hash or result.get('agreement_id') != str(contract.agreement_id) or
                result.get('terms_hash') != permit.terms_hash or result.get('dataset_id') != str(contract.remote_dataset_id)):
            raise FabricUnavailable('Peer response did not match the approved agreement.')
        if operation == 'read':
            rows, cursor = result.get('records'), result.get('next_cursor')
            if not isinstance(rows, list) or len(rows) > values['limit'] or type(cursor) is not int or not values['after'] <= cursor <= values['after'] + 10**12:
                raise FabricUnavailable('Invalid federated page.')
            previous = values['after']
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('id')) is not int or not previous < row['id'] <= cursor or
                        not isinstance(row.get('data'), dict) or set(row['data']) - set(contract.fields) or
                        type(row.get('synthetic')) is not bool or row['synthetic'] != (contract.data_class == 'synthetic')):
                    raise FabricUnavailable('Invalid federated record.')
                previous = row['id']
        elif (type(result.get('record_id')) is not int or type(result.get('created')) is not bool or
                type(result.get('synthetic')) is not bool or result['synthetic'] != (contract.data_class == 'synthetic')):
            raise FabricUnavailable('Invalid federated write receipt.')
        record_decision(principal, contract, 'fabric.federated_' + operation, True, 'bilateral_live_approval', effective_tags(contract))
        return {**result, 'organization_id': str(contract.organization_id), 'contract_id': str(contract.pk),
            'authority_organization_id': str(contract.peer.remote_organization_id)}
    finally:
        FabricPermit.objects.filter(pk=permit.pk).delete()
