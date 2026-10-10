"""Composable constraints and serializable, shared admission/usage accounting.

No provider is called here. Consumer reports are claims, not provider invoices.
Every accounting mutation locks the organization before touching its meters.
"""
from copy import deepcopy
from datetime import timedelta, timezone as utc
import hashlib
import json
import re
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, connection
from django.utils import timezone

from identity.access import require_access, record_decision
from identity.models import Membership, Organization
from .documentation import require_service_permissions
from .models import AIRulePolicy, AIRuleRevision, AIUsageMeter, AIUsageReservation, Installation, DeploymentEnvironment
from .training import require_capability

METRICS = ('requests', 'tokens', 'cost_micros', 'query_rows')
OPERATIONS = ('ai', 'query', 'workflow')
ZERO = dict.fromkeys(METRICS, 0)
MAXIMUM = 10 ** 12
DEFAULTS = {
    'user_daily': {'requests': 1000, 'tokens': 1000000, 'cost_micros': 0, 'query_rows': 100000},
    'organization_daily': {'requests': 10000, 'tokens': 10000000, 'cost_micros': 0, 'query_rows': 1000000},
    'query': {'max_rows': 200, 'timeout_ms': 10000, 'require_pagination': True, 'require_indexed_filters': True},
    'paid_calls_allowed': False, 'allowed_operations': list(OPERATIONS),
}


class RuleConflict(Exception):
    pass


class RuleDenied(Exception):
    def __init__(self, reason, metric=None):
        self.reason, self.metric = reason, metric


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def today():
    return timezone.now().astimezone(utc.utc).date()


def validate_clause(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValidationError('Use the fixed AI rule schema; executable expressions and unknown fields are excluded.')
    for name, items in value.items():
        if name in ('user_daily', 'organization_daily', 'query'):
            if not isinstance(items, dict) or set(items) - set(DEFAULTS[name]):
                raise ValidationError('Unknown quota or query constraint.')
            for key, limit in items.items():
                if key.startswith('require_'):
                    valid = type(limit) is bool
                else:
                    valid = type(limit) is int and 0 <= limit <= MAXIMUM
                    if key == 'max_rows':
                        valid = valid and 1 <= limit <= 200
                    if key == 'timeout_ms':
                        valid = valid and 1 <= limit <= 10000
                if not valid:
                    raise ValidationError('Use bounded integer limits and boolean query requirements.')
        elif name == 'paid_calls_allowed':
            if type(items) is not bool:
                raise ValidationError('Paid calls require an explicit boolean policy.')
        elif not isinstance(items, list) or len(items) != len(set(str(item) for item in items)) or any(item not in OPERATIONS for item in items):
            raise ValidationError('Choose distinct operations from ai, query, workflow.')


def require_rules(principal, organization, capability):
    require_capability(principal, organization, [capability])
    if not principal.credential:
        service = Installation.objects.get(organization=organization, package__slug='core-platform').service
        require_service_permissions(principal, service, [capability.replace('.', '-')])


def context(principal):
    if principal.agent_binding:
        binding = principal.agent_binding
        return binding.installation, DeploymentEnvironment.objects.filter(organization=binding.organization, name=binding.environment, active=True).first()
    if principal.credential:
        return principal.credential.installation, principal.credential.environment
    application = principal.application
    if application:
        installation = Installation.objects.filter(service_id=application.service_id).first()
        deployment = getattr(application, 'deployment', None)
        return installation, deployment.environment if deployment else None
    return None, None


def subject(principal):
    if principal.agent_binding:
        return 'agent:' + str(principal.agent_binding.account_id)
    if principal.user:
        return 'user:' + str(principal.user.pk)
    if principal.credential:
        # Rotating a workload key never resets an application's aggregate quota.
        return 'application:' + str(principal.credential.installation_id)
    raise PermissionDenied('An accountable user, agent, or application is required.')


def consumer(principal):
    installation, _ = context(principal)
    return 'application:' + str(installation.pk) if installation else subject(principal)


def baseline(policy):
    validate_clause(policy.baseline)
    result = deepcopy(DEFAULTS)
    for name, value in policy.baseline.items():
        if isinstance(value, dict):
            result[name].update(value)
        else:
            result[name] = deepcopy(value)
    return result


def resolved(policy, source):
    installation, environment = context(source)
    result = baseline(policy)
    for override in policy.overrides:
        if (override['installation_id'] and (not installation or override['installation_id'] != str(installation.pk)) or
                override['environment'] and (not environment or override['environment'] != environment.name)):
            continue
        clause = override['rules']
        validate_clause(clause)
        for name, value in clause.items():
            if isinstance(value, dict):
                for key, limit in value.items():
                    old = result[name][key]
                    result[name][key] = old or limit if key.startswith('require_') else min(old, limit)
            elif name == 'paid_calls_allowed':
                result[name] = result[name] and value
            else:
                result[name] = [item for item in result[name] if item in value]
    return result


def locked_policy(organization):
    Organization.objects.select_for_update().get(pk=organization.pk)
    return AIRulePolicy.objects.get_or_create(organization=organization, defaults={'baseline': deepcopy(DEFAULTS)})[0]


def authorize(policy, reader, source=None, capability='rules.read', *, internal=False):
    from .services import feature_enabled
    if not feature_enabled(policy.organization, 'core-platform'):
        raise PermissionDenied('The central rules service is unavailable.')
    for principal in [source, reader] if source and source is not reader else [reader]:
        if not internal:
            require_rules(principal, policy.organization, capability)
        require_access(principal, policy, 'ai_rules.' + capability)


@transaction.atomic
def save_policy(organization, principal, values):
    policy = locked_policy(organization)
    authorize(policy, principal, capability='rules.manage')
    if (not principal.user or principal.credential or principal.agent_binding or principal.application or
            not principal.user.is_active or not principal.user.is_superuser and not Membership.objects.filter(
                organization=organization, user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('A native organization administrator must compose platform rules.')
    if not isinstance(values, dict) or set(values) != {'revision', 'baseline', 'overrides'} or type(values['revision']) is not int:
        raise ValidationError('Supply revision, baseline, and overrides.')
    if values['revision'] != policy.revision:
        raise RuleConflict
    validate_clause(values['baseline'])
    if not isinstance(values['overrides'], list) or len(values['overrides']) > 100:
        raise ValidationError('Use at most 100 application/environment rule sets.')
    scopes = set()
    for row in values['overrides']:
        if not isinstance(row, dict) or set(row) != {'installation_id', 'environment', 'rules'}:
            raise ValidationError('Each override requires installation_id, environment, and rules.')
        key = (row['installation_id'], row['environment'])
        if any(not isinstance(item, str) for item in key):
            raise ValidationError('Rule scope identifiers are strings.')
        if key in scopes or not any(key):
            raise ValidationError('Use unique application/environment scopes; empty strings mean all.')
        scopes.add(key)
        for model, field, value in [(Installation, 'id', key[0]), (DeploymentEnvironment, 'name', key[1])]:
            if value:
                try:
                    item = model.objects.get(organization=organization, **{field: value})
                except (model.DoesNotExist, ValueError, ValidationError):
                    raise ValidationError('Rule scopes must belong to this organization.')
                require_access(principal, item, 'ai_rules.configure_scope')
        validate_clause(row['rules'])
    policy.baseline, policy.overrides = values['baseline'], values['overrides']
    policy.revision += 1
    policy.save(update_fields=['baseline', 'overrides', 'revision', 'updated'])
    AIRuleRevision.objects.create(policy=policy, revision=policy.revision, snapshot={**values, 'revision': policy.revision}, actor_id=principal.user.pk)
    record_decision(principal, policy, 'ai_rules.published', True, 'administrator_revision', [])
    return policy


def amounts(value, *, requests=1):
    if not isinstance(value, dict) or set(value) - set(METRICS) or any(type(item) is not int or not 0 <= item <= MAXIMUM for item in value.values()):
        raise ValidationError('Usage is bounded integer requests, tokens, cost_micros, and query_rows; currency is USD.')
    result = {**ZERO, **value}
    if result['requests'] != requests:
        raise ValidationError('Every attempted operation counts as one request.')
    return result


def get_meter(organization, key, day):
    return AIUsageMeter.objects.get_or_create(organization=organization, subject=key, day=day,
        defaults={'used': dict(ZERO), 'held': dict(ZERO)})[0]


def change(meters, field, value, factor=1):
    for meter in meters:
        totals = getattr(meter, field)
        for metric in METRICS:
            totals[metric] = totals.get(metric, 0) + factor * value[metric]
            if totals[metric] < 0:
                raise RuleConflict
        meter.save(update_fields=[field])


def reservation_document(row):
    return {'id': str(row.pk), 'organization_id': str(row.organization_id), 'state': row.state, 'operation': row.operation,
        'revision': row.revision, 'currency': 'USD', 'estimated': row.estimated, 'actual': row.actual,
        'expires': row.expires.isoformat(), 'report_source': 'consumer_report_not_provider_invoice'}


@transaction.atomic
def reserve(organization, reader, values, *, source=None, internal=False):
    source = source or reader
    policy = locked_policy(organization)
    authorize(policy, reader, source, 'usage.reserve', internal=internal)
    if not isinstance(values, dict) or set(values) != {'request_key', 'operation', 'estimated', 'query'}:
        raise ValidationError('Supply request_key, operation, estimated, and query.')
    key, operation = values['request_key'], values['operation']
    if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', key) or operation not in OPERATIONS:
        raise ValidationError('Use an opaque idempotency key and supported operation.')
    estimated = amounts(values['estimated'])
    identity, caller = subject(reader), consumer(source)
    fingerprint = digest(values)
    existing = AIUsageReservation.objects.filter(organization=organization, subject=identity, consumer=caller, request_key=key).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise RuleConflict
        require_access(reader, existing, 'ai_rules.idempotent_reservation')
        require_access(source, existing, 'ai_rules.idempotent_reservation')
        return existing
    limits = resolved(policy, source)
    if operation not in limits['allowed_operations']:
        raise RuleDenied('operation_disabled')
    if estimated['cost_micros'] and not limits['paid_calls_allowed']:
        raise RuleDenied('paid_calls_disabled', 'cost_micros')
    if AIUsageReservation.objects.filter(organization=organization, state='overrun', overrun_acknowledged=False).exists():
        raise RuleDenied('usage_overrun_requires_review')
    query = values['query']
    if operation == 'query':
        if (not isinstance(query, dict) or set(query) != {'rows', 'timeout_ms', 'paginated', 'indexed_filters'} or
                type(query['rows']) is not int or not 1 <= query['rows'] <= limits['query']['max_rows'] or
                estimated['query_rows'] != query['rows'] or type(query['timeout_ms']) is not int or
                not 1 <= query['timeout_ms'] <= limits['query']['timeout_ms'] or
                any(type(query[name]) is not bool for name in ('paginated', 'indexed_filters'))):
            raise ValidationError('Query rows and timeout must fit the resolved plan and reservation.')
        if any(limits['query']['require_' + name] and not query[flag] for name, flag in
                [('pagination', 'paginated'), ('indexed_filters', 'indexed_filters')]):
            raise RuleDenied('query_plan_rejected')
    elif query is not None or estimated['query_rows']:
        raise ValidationError('Query metadata and row budgets are only for query operations.')
    day = today()
    meters = [get_meter(organization, identity, day), get_meter(organization, '*', day)]
    for meter, budget in zip(meters, [limits['user_daily'], limits['organization_daily']]):
        for metric in METRICS:
            if meter.used.get(metric, 0) + meter.held.get(metric, 0) + estimated[metric] > budget[metric]:
                raise RuleDenied('quota_exceeded', metric)
    change(meters, 'held', estimated)
    installation, environment = context(source)
    row = AIUsageReservation.objects.create(organization=organization, policy=policy, installation=installation, environment=environment,
        subject=identity, consumer=caller, request_key=key, fingerprint=fingerprint, operation=operation, revision=policy.revision,
        day=day, estimated=estimated, expires=timezone.now() + timedelta(minutes=5))
    record_decision(reader, row, 'ai_rules.reserved', True, 'central_budget_admission', [])
    return row


@transaction.atomic
def transition(organization, reader, pk, action, actual=None, *, source=None, internal=False):
    source = source or reader
    policy = locked_policy(organization)
    authorize(policy, reader, source, 'usage.report', internal=internal)
    row = AIUsageReservation.objects.filter(pk=pk, organization=organization, subject=subject(reader), consumer=consumer(source)).first()
    if not row:
        raise PermissionDenied('Reservation belongs to a different principal or consumer.')
    for principal in [reader, source] if reader is not source else [reader]:
        require_access(principal, row, 'ai_rules.' + action)
    if action == 'start':
        if row.state == 'running':
            raise RuleDenied('operation_already_started')
        if row.state != 'reserved' or row.expires <= timezone.now():
            raise RuleDenied('reservation_unavailable')
        current = resolved(policy, source)
        if row.revision != policy.revision or row.operation not in current['allowed_operations']:
            raise RuleDenied('policy_changed_reserve_again')
        # Revoked permissions and tightened limits are rechecked before effects.
        row.state = 'running'
    elif action == 'cancel':
        if row.state == 'cancelled':
            return row
        if row.state != 'reserved':
            raise RuleDenied('started_usage_requires_settlement')
        change([get_meter(organization, row.subject, row.day), get_meter(organization, '*', row.day)], 'held', row.estimated, -1)
        row.state = 'cancelled'
    elif action == 'settle':
        actual = amounts(actual)
        if row.operation != 'query' and actual['query_rows']:
            raise ValidationError('Only query operations report query rows.')
        if row.state in ('settled', 'overrun'):
            if row.actual != actual:
                raise RuleConflict
            return row
        if row.state != 'running':
            raise RuleDenied('start_required_before_settlement')
        meters = [get_meter(organization, row.subject, row.day), get_meter(organization, '*', row.day)]
        change(meters, 'held', row.estimated, -1)
        change(meters, 'used', actual)
        row.actual = actual
        row.state = 'overrun' if any(actual[name] > row.estimated[name] for name in METRICS) else 'settled'
    else:
        raise ValidationError('Choose start, cancel, or settle.')
    row.save(update_fields=['state', 'actual', 'updated'])
    record_decision(reader, row, 'ai_rules.' + action, True, row.state, [])
    return row


@transaction.atomic
def acknowledge(organization, principal, pk):
    policy = locked_policy(organization)
    authorize(policy, principal, capability='rules.manage')
    if not principal.user or principal.agent_binding or principal.credential or principal.application or not (
            principal.user.is_superuser or Membership.objects.filter(organization=organization, user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('An administrator must acknowledge usage overruns.')
    row = AIUsageReservation.objects.filter(pk=pk, organization=organization, state='overrun').first()
    if not row:
        raise PermissionDenied('Only an overrun in this organization can be acknowledged.')
    require_access(principal, row, 'ai_rules.reconcile')
    row.overrun_acknowledged = True
    row.save(update_fields=['overrun_acknowledged', 'updated'])
    record_decision(principal, row, 'ai_rules.overrun_acknowledged', True, 'usage_preserved', [])


@transaction.atomic
def document(organization, reader, *, source=None):
    policy = locked_policy(organization)
    authorize(policy, reader, source)
    installation, environment = context(source or reader)
    limits = resolved(policy, source or reader)
    meter = get_meter(organization, subject(reader), today())
    aggregate = get_meter(organization, '*', today())
    reset = timezone.datetime.combine(today() + timedelta(days=1), timezone.datetime.min.time(), tzinfo=utc.utc)
    remaining = {name: max(0, limits['user_daily'][name] - meter.used.get(name, 0) - meter.held.get(name, 0)) for name in METRICS}
    organization_remaining = {name: max(0, limits['organization_daily'][name] - aggregate.used.get(name, 0) - aggregate.held.get(name, 0)) for name in METRICS}
    value = {'schema_version': 1, 'organization_id': str(organization.pk), 'installation_id': str(installation.pk) if installation else None,
        'environment': environment.name if environment else None, 'revision': policy.revision, 'currency': 'USD',
        'cost_unit': 'one_millionth_USD', 'rules': limits, 'subject': subject(reader),
        'usage': {'used': meter.used, 'reserved': meter.held,
            'remaining': {name: min(remaining[name], organization_remaining[name]) for name in METRICS},
            'organization_remaining': organization_remaining, 'reset_at': reset.isoformat(),
            'warning_metrics': [name for name in METRICS if limits['user_daily'][name] and
                (meter.used.get(name, 0) + meter.held.get(name, 0)) * 100 >= limits['user_daily'][name] * 80],
            'review_required': AIUsageReservation.objects.filter(organization=organization, state='overrun', overrun_acknowledged=False).exists()},
        'enforcement': 'central_reservation_required_before_operation', 'provider_billing': 'consumer_reports_only',
        'instructions': ['Fetch rules; reserve upper bounds before each operation.', 'Start before side effects; enforce query plan and provider output bounds.',
            'Settle actual usage once; unknown outcomes retain their reservation.', 'Quota denials use the same reason and UTC reset across applications.',
            'Rules do not grant service permissions or financial approval.']}
    value['digest'] = digest(value)
    return value


@transaction.atomic
def core_query(organization, principal, requested_rows, callback):
    """Core's own indexed, cursor-based data reads; measured rows, no provider."""
    policy = locked_policy(organization)
    authorize(policy, principal, internal=True)
    plan = resolved(policy, principal)['query']
    limit = min(requested_rows, plan['max_rows'])
    row = reserve(organization, principal, {'request_key': uuid.uuid4().hex, 'operation': 'query',
        'estimated': {'requests': 1, 'query_rows': limit}, 'query': {'rows': limit, 'timeout_ms': plan['timeout_ms'],
        'paginated': True, 'indexed_filters': True}}, internal=True)
    transition(organization, principal, row.pk, 'start', internal=True)
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(plan['timeout_ms'])])
    import time
    connection.ensure_connection()
    if connection.vendor == 'sqlite':
        deadline = time.monotonic() + plan['timeout_ms'] / 1000
        connection.connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
    try:
        result = callback(limit)
    finally:
        if connection.vendor == 'sqlite':
            connection.connection.set_progress_handler(None, 0)
    transition(organization, principal, row.pk, 'settle', {'requests': 1, 'query_rows': len(result)}, internal=True)
    return result, limit


@transaction.atomic
def settle_core_query(reservation, inspected_rows):
    """Trusted retrieval accounting after I/O, even when access has been revoked.

    This has no HTTP entry point and grants no data access. Immutable reservation
    subject/consumer keys determine the meters; model/provider spend stays zero.
    """
    Organization.objects.select_for_update().get(pk=reservation.organization_id)
    row = AIUsageReservation.objects.select_for_update().get(pk=reservation.pk, organization_id=reservation.organization_id)
    actual = amounts({'requests': 1, 'query_rows': inspected_rows})
    if row.operation != 'query' or row.estimated['tokens'] or row.estimated['cost_micros'] or inspected_rows > row.estimated['query_rows']:
        raise RuleConflict
    if row.state == 'settled' and row.actual == actual:
        return row
    if row.state != 'running':
        raise RuleConflict
    meters = [get_meter(row.organization, row.subject, row.day), get_meter(row.organization, '*', row.day)]
    change(meters, 'held', row.estimated, -1)
    change(meters, 'used', actual)
    row.state, row.actual = 'settled', actual
    row.save(update_fields=['state', 'actual', 'updated'])
    return row


@transaction.atomic
def core_step(organization, principal, key, callback):
    """Transactional, deterministic Core tools have no paid/model side effects."""
    row = reserve(organization, principal, {'request_key': digest(key), 'operation': 'workflow',
        'estimated': {'requests': 1}, 'query': None}, internal=True)
    if row.state != 'reserved':
        raise RuleDenied('operation_already_started')
    transition(organization, principal, row.pk, 'start', internal=True)
    result = callback()
    transition(organization, principal, row.pk, 'settle', {'requests': 1}, internal=True)
    return result
