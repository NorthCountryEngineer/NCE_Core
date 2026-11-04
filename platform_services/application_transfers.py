"""Human-controlled, metadata-only application handover.

This module deliberately has no HTTP/cloud/database-export adapter. Signed local
application proofs are supplied by operators; Core login is not source-data authority.
"""
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.access import require_access
from identity.models import Application, AuditEvent, Membership
from identity.policy_evaluation import policy_fact
from .migration_protocol import canonical_json, decode64, digest, verify_document
from .models import (ApplicationPlacementReference, ApplicationTransfer, CloudApplication,
    CloudRelease, Deployment, Installation, TransferReceipt, WorkloadCredential)

CAPABILITIES = ['fenced-cutover', 'identity-mapping', 'inert-import', 'offline-release-verification',
    'paused-workers', 'tenant-closure']
STAGES = ['snapshot', 'staged', 'fenced', 'verified', 'activated']
HASH = re.compile(r'[a-f0-9]{64}')
IDENTIFIER = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}')


def independent(installation):
    return policy_fact(('independent_installation', str(installation.pk)), lambda:
        ApplicationPlacementReference.objects.filter(installation_id=installation.pk, reference_only=True).exists())


def require_managed(installation):
    if independent(installation):
        raise ValidationError('This application was handed over. Core retains a reference only and has no runtime or data authority.')


def require_change_allowed(installation):
    require_managed(installation)
    if ApplicationTransfer.objects.filter(installation_id=installation.pk, approved_at__isnull=False).exclude(
            state__in=['cancelled', 'handed_over']).exists():
        raise ValidationError('An approved transfer freezes Core deployment changes. Cancel before fencing, or use a reviewed recovery plan.')


def controls_frozen(installation):
    try:
        require_change_allowed(installation)
        return False
    except ValidationError:
        return True


def authorize(principal, installation):
    user = principal.user
    if (not user or not user.is_authenticated or not user.is_active or principal.credential or principal.runtime or
            principal.agent_binding or principal.agent_credential or not (user.is_superuser or
            Membership.objects.filter(organization=installation.organization, user=user, active=True,
                kind='administrator').exists())):
        raise PermissionDenied('An active human source organization administrator is required.')
    require_access(principal, installation, 'application.transfer')
    if installation.package.kind != 'application':
        raise ValidationError('Only applications may be transferred; Core control-plane services cannot be handed over here.')


def _audit(transfer, user, action):
    AuditEvent.objects.create(organization=transfer.installation.organization, actor=user,
        action='application.transfer.' + action, target=str(transfer.pk))


def source_identity(installation):
    return {'organization_id': str(installation.organization_id), 'installation_id': str(installation.pk),
        'service_id': str(installation.service_id),
        'deployment_ids': sorted(str(pk) for pk in installation.deployments.values_list('pk', flat=True))}


def _fingerprint(installation):
    # Hash authorization and registration state locally. No directory/user/client
    # records leave this function or enter the reviewed plan.
    from identity.models import (ApplicationAccountAccess, AuthenticationPolicy, IdentityProvider,
        PermissionTag, ProtectedResource, Role, User)
    rows = [installation, installation.organization, installation.service, installation.package]
    rows.extend({deployment.environment_id: deployment.environment for deployment in installation.deployments.select_related(
        'environment') if deployment.environment_id}.values())
    for queryset in [installation.deployments.all(), Application.objects.filter(service_id=installation.service_id),
            installation.service.permissions.all(), Membership.objects.filter(organization_id=installation.organization_id),
            Role.objects.filter(organization_id=installation.organization_id),
            PermissionTag.objects.filter(organization_id=installation.organization_id),
            AuthenticationPolicy.objects.filter(organization_id=installation.organization_id),
            IdentityProvider.objects.filter(organization_id=installation.organization_id),
            ApplicationAccountAccess.objects.filter(deployment__installation=installation),
            ProtectedResource.objects.filter(service_id=installation.service_id),
            WorkloadCredential.objects.filter(installation=installation),
            CloudApplication.objects.filter(installation=installation)]:
        rows.extend(queryset.order_by('pk'))
    hasher = hashlib.sha256()
    hasher.update(json.dumps({'source_core_origin': settings.ISSUER, 'identity_activity': [
        [str(pk), enabled] for pk, enabled in User.objects.filter(memberships__organization_id=installation.organization_id).order_by(
            'pk').values_list('pk', 'is_active')]}, sort_keys=True, separators=(',', ':')).encode())
    for row in sorted(rows, key=lambda item: (item._meta.label_lower, str(item.pk))):
        volatile = {'observed_revision', 'observed_state', 'last_seen', 'healthy', 'message'} if isinstance(row, Deployment) else set()
        record = {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields
            if field.name not in volatile and not getattr(field, 'auto_now', False)}
        record['_relations'] = {field.name: sorted(str(pk) for pk in getattr(row, field.name).values_list('pk', flat=True))
            for field in row._meta.many_to_many}
        hasher.update(json.dumps([row._meta.label_lower, str(row.pk), record], cls=DjangoJSONEncoder,
            sort_keys=True, separators=(',', ':')).encode())
        hasher.update(b'\n')
    return hasher.hexdigest()


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError('Unsupported metadata fields. Customer data and arbitrary commands are prohibited.')


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValidationError('Expected an exact SHA-256 metadata digest.')


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError('Expected a bounded opaque identifier.')


def _origin(value):
    if not isinstance(value, str) or len(value) > 250:
        raise ValidationError('Use an exact HTTPS destination origin.')
    parsed = urlsplit(value)
    URLValidator(schemes=['https'])(value)
    try:
        valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and (
            parsed.path == '' and not parsed.query and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if (not valid or value != value.rstrip('/') or '\\' in value or
            any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValidationError('Use an exact HTTPS origin without credentials, path, query, fragment, or control characters.')


@transaction.atomic
def create_offer(installation, principal):
    installation = Installation.objects.select_for_update().select_related('package', 'organization').get(pk=installation.pk)
    authorize(principal, installation)
    require_managed(installation)
    if installation.transfers.exclude(state__in=['cancelled', 'handed_over']).exclude(
            Q(state='offered') & Q(expires_at__lte=timezone.now())).exists():
        raise ValidationError('An active pairing offer already exists. Cancel it before creating another.')
    if installation.transfers.filter(approved_at__isnull=False).exclude(state__in=['cancelled', 'handed_over']).exists():
        raise ValidationError('An approved transfer is already active.')
    raw = secrets.token_urlsafe(32)
    transfer = ApplicationTransfer.objects.create(installation=installation, created_by=principal.user,
        source_identity=source_identity(installation), expires_at=timezone.now() + timedelta(minutes=10),
        pairing_secret_hash=hashlib.sha256(raw.encode()).hexdigest())
    _audit(transfer, principal.user, 'offer_created')
    return transfer, raw


@transaction.atomic
def claim_offer(offer_id, secret, envelope):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation').get(pk=offer_id)
    if (transfer.state != 'offered' or transfer.expires_at <= timezone.now() or not isinstance(secret, str) or
            len(secret) > 100 or not hmac.compare_digest(transfer.pairing_secret_hash, hashlib.sha256(secret.encode()).hexdigest())):
        raise ValidationError('Pairing offer is expired, unavailable, or already used.')
    try:
        payload = envelope['payload']
        _exact(payload, ['offer_id', 'source_ids', 'destination', 'challenge', 'capabilities', 'issued_at', 'expires_at'])
        destination = payload['destination']
        _exact(destination, ['origin', 'mode', 'public_key', 'adapter_id'])
        _origin(destination['origin'])
        _identifier(destination['adapter_id'])
        _identifier(payload['challenge'])
        if destination['mode'] not in ['customer-core', 'direct-sso']:
            raise ValidationError('Unsupported destination identity adapter.')
        if payload['source_ids'] != transfer.source_identity or payload['offer_id'] != str(transfer.pk):
            raise ValidationError('Receiver claim does not match the exact source identity.')
        if (not isinstance(payload['capabilities'], list) or payload['capabilities'] != sorted(set(payload['capabilities'])) or
                set(payload['capabilities']) != set(CAPABILITIES)):
            raise ValidationError('The receiver must support the complete reviewed capability contract.')
        now = int(timezone.now().timestamp())
        if (type(payload['issued_at']) is not int or type(payload['expires_at']) is not int or
                payload['issued_at'] > now + 30 or payload['issued_at'] < now - 600 or
                payload['expires_at'] <= max(now, payload['issued_at']) or payload['expires_at'] > now + 600):
            raise ValidationError('Receiver proof must be fresh and expire within ten minutes.')
        verify_document(envelope, destination['public_key'], 'receiver-descriptor')
    except (KeyError, ValueError, TypeError):
        raise ValidationError('Invalid receiver descriptor or signature.') from None
    transfer.receiver_claim, transfer.receiver_claim_hash = envelope, digest(envelope)
    transfer.state, transfer.paired_at = 'paired', timezone.now()
    transfer.pairing_secret_hash = ''
    transfer.save()
    _audit(transfer, None, 'receiver_claimed')
    return transfer


def _validate_plan(value):
    _exact(value, ['source_adapter', 'capability_contract', 'snapshot_manifest_hash', 'identity_mapping_hash',
        'policy_mapping_hash', 'release_manifest_hash', 'retention_days', 'guest_links', 'sessions', 'workers',
        'snapshot_reference', 'data_counts'])
    _exact(value['source_adapter'], ['adapter_id', 'public_key', 'authority_hash'])
    _identifier(value['source_adapter']['adapter_id'])
    _hash(value['source_adapter']['authority_hash'])
    try:
        decode64(value['source_adapter']['public_key'], 32)
    except ValueError:
        raise ValidationError('Invalid hosted-local authority public key.') from None
    contract = value['capability_contract']
    _exact(contract, ['adapter_id', 'version', 'capabilities', 'inventory_hash', 'unknown_extensions'])
    _identifier(contract['adapter_id'])
    if (contract['version'] != 1 or type(contract['version']) is not int or contract['capabilities'] != CAPABILITIES or
            type(contract['unknown_extensions']) is not int or contract['unknown_extensions'] != 0):
        raise ValidationError('Unsupported adapter/version or incomplete application inventory. Review unknown extensions first.')
    _hash(contract['inventory_hash'])
    for name in ['snapshot_manifest_hash', 'identity_mapping_hash', 'policy_mapping_hash', 'release_manifest_hash']:
        _hash(value[name])
    if type(value['retention_days']) is not int or not 1 <= value['retention_days'] <= 3650:
        raise ValidationError('Choose an explicit source retention period between 1 and 3650 days.')
    if value['guest_links'] not in ['preserve', 'rotate'] or value['sessions'] != 'invalidate' or value['workers'] != 'paused':
        raise ValidationError('Review guest links, invalidate sessions, and preserve workers paused.')
    reference = value['snapshot_reference']
    _exact(reference, ['reference_id', 'ciphertext_hash', 'key_custody'])
    _identifier(reference['reference_id'])
    _hash(reference['ciphertext_hash'])
    if reference['key_custody'] != 'destination':
        raise ValidationError('Only a destination-controlled encrypted snapshot reference is accepted; no URL or key may be submitted.')
    counts = value['data_counts']
    if not isinstance(counts, dict) or len(counts) > 100:
        raise ValidationError('Use bounded record counts, never customer records.')
    for name, count in counts.items():
        _identifier(name)
        if type(count) is not int or not 0 <= count <= 10**12:
            raise ValidationError('Invalid record count.')
    canonical_json(value)


@transaction.atomic
def prepare_plan(transfer, principal, plan):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation__package').get(pk=transfer.pk)
    authorize(principal, transfer.installation)
    require_managed(transfer.installation)
    if transfer.state not in ['paired', 'planned'] or transfer.approved_at:
        raise ValidationError('Pair the receiver first; an approved plan is immutable.')
    if transfer.source_identity != source_identity(transfer.installation):
        raise ValidationError('Source installation identities changed after pairing. Create and review a fresh pairing offer.')
    _validate_plan(plan)
    reviewed = {'transfer_id': str(transfer.pk), 'source_ids': transfer.source_identity,
        'source_core_origin': settings.ISSUER,
        'source_fingerprint': _fingerprint(transfer.installation), 'destination_claim_hash': transfer.receiver_claim_hash,
        'destination': transfer.receiver_claim['payload']['destination'], **plan}
    transfer.plan, transfer.plan_hash, transfer.state = reviewed, digest(reviewed), 'planned'
    transfer.save()
    _audit(transfer, principal.user, 'plan_prepared')
    return transfer


@transaction.atomic
def approve_plan(transfer, principal, expected_plan_hash):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation__package').get(pk=transfer.pk)
    Installation.objects.select_for_update().get(pk=transfer.installation_id)
    authorize(principal, transfer.installation)
    require_managed(transfer.installation)
    if (transfer.state != 'planned' or expected_plan_hash != transfer.plan_hash or digest(transfer.plan) != transfer.plan_hash or
            transfer.plan['source_fingerprint'] != _fingerprint(transfer.installation)):
        raise ValidationError('The exact reviewed plan or source changed. Prepare and review it again.')
    if transfer.installation.transfers.exclude(pk=transfer.pk).filter(approved_at__isnull=False).exclude(
            state__in=['cancelled', 'handed_over']).exists():
        raise ValidationError('Another approved transfer already controls this installation.')
    if CloudRelease.objects.filter(application__installation=transfer.installation, status__in=['queued', 'running']).exists():
        raise ValidationError('Finish or cancel active source releases before approving a transfer.')
    transfer.approved_by, transfer.approved_at, transfer.state = principal.user, timezone.now(), 'approved'
    transfer.save()
    _audit(transfer, principal.user, 'plan_approved')
    return transfer


def _evidence(transfer, stage, evidence):
    hashes = {
        'snapshot': ['snapshot_manifest_hash'],
        'staged': ['snapshot_manifest_hash', 'identity_mapping_hash', 'policy_mapping_hash', 'release_manifest_hash'],
        'fenced': ['snapshot_manifest_hash'],
        'verified': ['snapshot_manifest_hash', 'identity_mapping_hash', 'policy_mapping_hash', 'release_manifest_hash'],
        'activated': ['snapshot_manifest_hash'],
    }[stage]
    checks = {
        'snapshot': ['local_admin_authorized', 'tenant_closure_verified', 'source_writes_fenced',
            'workers_paused', 'ambiguous_sends_blocked'],
        'staged': ['inert_import', 'release_mirrored', 'offline_release_verified', 'independent_identity_ready',
            'independent_admin_ready', 'credentials_rewrapped', 'no_source_keys', 'workers_paused', 'guest_links_reviewed'],
        'fenced': ['source_writes_fenced', 'workers_paused', 'ambiguous_sends_blocked', 'sessions_invalidated'],
        'verified': ['data_counts_match', 'blobs_verified', 'identity_mapping_verified', 'policy_semantics_preserved',
            'local_admin_login_verified', 'source_dependency_absent', 'restart_without_nce_verified'],
        'activated': ['destination_writes_enabled', 'source_fence_confirmed', 'ambiguous_sends_blocked', 'independent_operation_verified'],
    }[stage]
    extras = ['source_authority_hash'] if stage == 'snapshot' else []
    _exact(evidence, hashes + checks + extras)
    if any(evidence[name] is not True for name in checks):
        raise ValidationError('A required acceptance gate has not passed. Unsupported policy semantics must block transfer.')
    if any(evidence[name] != transfer.plan[name] for name in hashes):
        raise ValidationError('Evidence does not match the approved manifest or identity/policy/release mapping.')
    if stage == 'snapshot' and evidence['source_authority_hash'] != transfer.plan['source_adapter']['authority_hash']:
        raise ValidationError('Source-local administrator authority does not match the reviewed adapter.')


def _review_current(transfer):
    installation = Installation.objects.select_related('organization', 'service', 'package').get(pk=transfer.installation_id)
    if (not transfer.plan_hash or digest(transfer.plan) != transfer.plan_hash or
            transfer.plan.get('source_fingerprint') != _fingerprint(installation)):
        raise ValidationError('Source authority or the exact reviewed plan changed. Further stages require a reviewed recovery plan.')
    if CloudRelease.objects.filter(application__installation=installation, status__in=['queued', 'running']).exists():
        raise ValidationError('An active source release blocks migration acceptance.')


@transaction.atomic
def accept_receipt(transfer_id, envelope):
    transfer = ApplicationTransfer.objects.select_for_update().get(pk=transfer_id)
    if not transfer.approved_at or transfer.state in ['cancelled', 'handed_over'] or transfer.accepted_sequence >= len(STAGES):
        raise ValidationError('This transfer cannot receive another stage receipt.')
    _review_current(transfer)
    try:
        payload = envelope['payload']
        _exact(payload, ['transfer_id', 'plan_hash', 'sequence', 'previous_receipt_hash', 'stage', 'fence_epoch', 'evidence'])
        stage = STAGES[transfer.accepted_sequence]
        if (payload['transfer_id'] != str(transfer.pk) or payload['plan_hash'] != transfer.plan_hash or
                type(payload['sequence']) is not int or payload['sequence'] != transfer.accepted_sequence + 1 or
                payload['previous_receipt_hash'] != transfer.accepted_receipt_hash or payload['stage'] != stage):
            raise ValidationError('Receipt is replayed, reordered, or bound to another transfer or plan.')
        epoch = payload['fence_epoch']
        if type(epoch) is not int or epoch < 1 or (stage != 'snapshot' and epoch != transfer.fence_epoch):
            raise ValidationError('Invalid or stale fence epoch.')
        key = transfer.plan['source_adapter']['public_key'] if stage in ['snapshot', 'fenced'] else transfer.plan['destination']['public_key']
        verify_document(envelope, key, 'migration-receipt')
        _evidence(transfer, stage, payload['evidence'])
        receipt_hash = digest(envelope)
    except (KeyError, ValueError, TypeError, IndexError):
        raise ValidationError('Invalid receipt metadata or signature.') from None
    existing = transfer.receipts.filter(sequence=payload['sequence']).first()
    if existing:
        if existing.digest != receipt_hash:
            raise ValidationError('A different receipt already occupies this sequence. No overwrite is allowed.')
        return transfer
    TransferReceipt.objects.create(transfer=transfer, sequence=payload['sequence'], stage=stage,
        envelope=envelope, digest=receipt_hash)
    _audit(transfer, None, 'receipt_received')
    return transfer


@transaction.atomic
def advance_transfer(transfer, principal, expected_receipt_hash):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation__package').get(pk=transfer.pk)
    authorize(principal, transfer.installation)
    if transfer.state in ['handed_over', 'cancelled']:
        raise ValidationError('This transfer is closed.')
    _review_current(transfer)
    receipt = transfer.receipts.filter(sequence=transfer.accepted_sequence + 1, accepted_at__isnull=True).first()
    if not receipt or receipt.digest != expected_receipt_hash or digest(receipt.envelope) != receipt.digest:
        raise ValidationError('Review the exact pending signed receipt before accepting this stage.')
    receipt.accepted_at, receipt.accepted_by = timezone.now(), principal.user
    receipt.save()
    transfer.accepted_sequence, transfer.accepted_receipt_hash = receipt.sequence, receipt.digest
    transfer.state = receipt.stage
    if receipt.stage == 'snapshot':
        transfer.fence_epoch = receipt.envelope['payload']['fence_epoch']
    transfer.save()
    _audit(transfer, principal.user, 'stage_accepted')
    return transfer


@transaction.atomic
def handover(transfer, principal, expected_plan_hash, expected_receipt_hash):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation__package').get(pk=transfer.pk)
    installation = Installation.objects.select_for_update().get(pk=transfer.installation_id)
    authorize(principal, installation)
    require_managed(installation)
    if (transfer.state != 'activated' or transfer.accepted_sequence != len(STAGES) or not transfer.fence_epoch or
            expected_plan_hash != transfer.plan_hash or expected_receipt_hash != transfer.accepted_receipt_hash or
            digest(transfer.plan) != transfer.plan_hash or _fingerprint(installation) != transfer.plan['source_fingerprint']):
        raise ValidationError('All exact signed acceptance stages and the unchanged reviewed source are required.')
    apps = list(CloudApplication.objects.select_for_update().filter(installation=installation))
    if CloudRelease.objects.filter(application__in=apps, status__in=['queued', 'running']).exists():
        raise ValidationError('An active Core release blocks handover.')
    client_ids = list(Application.objects.filter(service_id=installation.service_id).values_list('pk', flat=True))
    from oauth2_provider.models import (get_access_token_model, get_refresh_token_model, get_grant_model, get_id_token_model)
    get_refresh_token_model().objects.filter(application_id__in=client_ids, revoked__isnull=True).update(
        revoked=timezone.now(), access_token=None)
    for model in [get_access_token_model(), get_grant_model(), get_id_token_model()]:
        model.objects.filter(application_id__in=client_ids).delete()
    Application.objects.filter(pk__in=client_ids).update(active=False)
    from identity.models import ApplicationAccountAccess, ApplicationAccountInvitation
    ApplicationAccountAccess.objects.filter(deployment__installation=installation).update(active=False)
    ApplicationAccountInvitation.objects.filter(deployment__installation=installation, revoked_at__isnull=True).update(
        revoked_at=timezone.now())
    WorkloadCredential.objects.filter(installation=installation).update(active=False)
    from core_ai.models import QuerySource
    from core_fabric.models import FabricPeer
    from .models import AgentBinding, ApplicationIntegration
    QuerySource.objects.filter(installation=installation).update(active=False)
    FabricPeer.objects.filter(inbound_installation=installation).update(active=False)
    AgentBinding.objects.filter(installation=installation).update(active=False)
    ApplicationIntegration.objects.filter(installation=installation).update(mode='disabled', workload_access=False,
        credential_access=False)
    # Detach the source connector, do not emit a new reconciliation/deletion command.
    Deployment.objects.filter(installation=installation).update(node=None, desired_state='stopped',
        healthy=False, observed_state='handed_over', message='Independent destination; Core reference only.')
    installation.enabled = False
    installation.save(update_fields=['enabled'])
    installation.service.active = False
    installation.service.save(update_fields=['active'])
    CloudApplication.objects.filter(pk__in=[app.pk for app in apps]).update(active=False)
    destination = transfer.plan['destination']
    ApplicationPlacementReference.objects.create(installation=installation, transfer=transfer,
        destination_origin=destination['origin'], destination_mode=destination['mode'], source_identity=transfer.source_identity,
        plan_hash=transfer.plan_hash, final_receipt_hash=transfer.accepted_receipt_hash)
    transfer.state, transfer.completed_at = 'handed_over', timezone.now()
    transfer.save()
    _audit(transfer, principal.user, 'handed_over')
    return transfer


@transaction.atomic
def cancel_transfer(transfer, principal):
    transfer = ApplicationTransfer.objects.select_for_update().select_related('installation__package').get(pk=transfer.pk)
    authorize(principal, transfer.installation)
    if transfer.accepted_sequence >= 1 or transfer.state == 'handed_over':
        raise ValidationError('A fenced transfer requires an explicitly reviewed recovery plan; automatic rollback is prohibited.')
    transfer.state = 'cancelled'
    transfer.pairing_secret_hash = ''
    transfer.save()
    _audit(transfer, principal.user, 'cancelled')
    return transfer
