"""Provider-neutral rules and accounting APIs with live delegated user proof."""
from functools import wraps
from django.core.exceptions import ValidationError
from django.db import OperationalError
from .api import body, response, rule_denial
from .integration_api import integration_api
from .ai_rules import document, reserve, transition, reservation_document, RuleConflict, RuleDenied
from .models import AIRulePolicy, AIUsageReservation


def rules_api(methods=('GET',)):
    def decorate(view):
        @integration_api(methods, namespace='ai_rules')
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            request.integration_audit_resource = AIRulePolicy.objects.filter(organization=request.organization).first() or request.organization
            try:
                return view(request, *args, **kwargs)
            except RuleConflict:
                return response({'error': 'revision_or_idempotency_conflict'}, 409)
            except RuleDenied as error:
                from identity.access import record_decision
                for principal in [request.integration_source, request.access_principal] if request.integration_source is not request.access_principal else [request.access_principal]:
                    record_decision(principal, request.integration_audit_resource, 'ai_rules.operation_denied', False, error.reason, [])
                return rule_denial(error)
            except OperationalError:
                # SQLite is a development backend; contention fails closed.
                return response({'error': 'accounting_temporarily_unavailable'}, 503)
        return wrapped
    return decorate


@rules_api()
def rules(request):
    if request.GET:
        raise ValidationError('Rule scope is derived from the authenticated consumer, never supplied by the caller.')
    return response(document(request.organization, request.access_principal, source=request.integration_source))


@rules_api(('POST',))
def reservations(request):
    row = reserve(request.organization, request.access_principal, body(request), source=request.integration_source)
    return response(reservation_document(row), 201)


@rules_api(('POST',))
def update(request, pk):
    request.integration_audit_resource = AIUsageReservation.objects.filter(pk=pk, organization=request.organization).first() or request.integration_audit_resource
    values = body(request)
    if set(values) - {'action', 'actual'} or 'action' not in values or (values['action'] == 'settle') != ('actual' in values):
        raise ValidationError('Supply action, and actual usage only when settling.')
    row = transition(request.organization, request.access_principal, pk, values['action'], values.get('actual'), source=request.integration_source)
    return response(reservation_document(row))
