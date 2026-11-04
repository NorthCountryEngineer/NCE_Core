"""Operator review and signed metadata intake; never contacts a pasted origin."""
import json
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from identity.access import request_principal
from identity.views import organization_admin
from . import application_transfers as transfers
from .migration_protocol import canonical_json
from .models import ApplicationTransfer


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate metadata field.')
        result[key] = value
    return result


def _json(raw):
    if len(raw) > 32768:
        raise ValueError('Metadata exceeds 32 KiB.')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except RecursionError:
        raise ValueError('Metadata nesting exceeds its bound.') from None
    canonical_json(value)
    return value


@organization_admin
def application_transfer(request, organization, pk):
    installation = get_object_or_404(organization.installations.select_related('package'), pk=pk, package__kind='application')
    principal = request_principal(request)
    transfers.authorize(principal, installation)
    pairing_link, error = '', ''
    current = installation.transfers.order_by('-created').first()
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'offer':
                current, secret = transfers.create_offer(installation, principal)
                fragment = urlencode({'offer_id': str(current.pk), 'source_origin': settings.ISSUER,
                    'source_ids': json.dumps(current.source_identity, sort_keys=True, separators=(',', ':')), 'secret': secret})
                pairing_link = settings.ISSUER + '/application-transfer/receive/#' + fragment
            else:
                current = get_object_or_404(installation.transfers, pk=request.POST.get('transfer_id'))
                if action == 'claim':
                    # Manual copy/paste is supported without giving Core network access.
                    document = _json(request.POST.get('document', ''))
                    secret = request.POST.get('secret', '')
                    if isinstance(document, dict) and set(document) == {'secret', 'envelope'}:
                        if secret and secret != document['secret']:
                            raise ValidationError('The supplied pairing secrets differ.')
                        secret, document = document['secret'], document['envelope']
                    current = transfers.claim_offer(current.pk, secret, document)
                elif action == 'plan':
                    current = transfers.prepare_plan(current, principal, _json(request.POST.get('document', '')))
                elif action == 'approve':
                    current = transfers.approve_plan(current, principal, request.POST.get('plan_hash'))
                elif action == 'receipt':
                    current = transfers.accept_receipt(current.pk, _json(request.POST.get('document', '')))
                elif action == 'advance':
                    current = transfers.advance_transfer(current, principal, request.POST.get('receipt_hash'))
                elif action == 'handover':
                    current = transfers.handover(current, principal, request.POST.get('plan_hash'), request.POST.get('receipt_hash'))
                elif action == 'cancel':
                    current = transfers.cancel_transfer(current, principal)
                else:
                    raise ValidationError('Unknown transfer operation.')
            if not pairing_link:
                return redirect('application_transfer', slug=organization.slug, pk=installation.pk)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            error = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'Invalid bounded JSON metadata.'
    pending = current.receipts.filter(accepted_at__isnull=True).first() if current else None
    return render(request, 'platform_services/application_transfer.html', {'organization': organization,
        'installation': installation, 'transfer': current, 'pairing_link': pairing_link, 'error': error,
        'plan_json': json.dumps(current.plan, indent=2) if current else '',
        'claim_json': json.dumps(current.receiver_claim, indent=2) if current else '',
        'pending': pending, 'pending_json': json.dumps(pending.envelope, indent=2) if pending else '',
        'placement': getattr(installation, 'independent_placement', None)})


def _status(transfer):
    return {'transfer_id': str(transfer.pk), 'state': transfer.state, 'plan_hash': transfer.plan_hash,
        'accepted_sequence': transfer.accepted_sequence, 'accepted_receipt_hash': transfer.accepted_receipt_hash}


@require_GET
def pairing_help(request):
    response = HttpResponse('<!doctype html><html lang="en"><meta charset="utf-8"><title>Application pairing link</title>'
        '<script src="/application-transfer/clear-fragment.js"></script><h1>Application pairing link</h1>'
        '<p>Paste the original link into Receive application on the independent destination. '
        'This source page does not contact the destination or authorize a data export.</p>'
        '<p>The secret fragment has been removed from this page history.</p></html>')
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    return response


@require_GET
def clear_fragment(request):
    return HttpResponse('history.replaceState(null, "", location.pathname);', content_type='application/javascript')


@csrf_exempt
@require_POST
def claim(request, pk):
    try:
        value = _json(request.body)
        if not isinstance(value, dict) or set(value) != {'secret', 'envelope'}:
            raise ValueError
        transfer = transfers.claim_offer(pk, value['secret'], value['envelope'])
        response = JsonResponse(_status(transfer))
    except (ApplicationTransfer.DoesNotExist, ValidationError, ValueError, TypeError, KeyError):
        response = JsonResponse({'error': 'invalid_or_unavailable_pairing_offer'}, status=400)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def receipt(request, pk):
    try:
        value = _json(request.body)
        transfer = transfers.accept_receipt(pk, value)
        response = JsonResponse(_status(transfer))
    except (ApplicationTransfer.DoesNotExist, ValidationError, ValueError, TypeError, KeyError):
        response = JsonResponse({'error': 'invalid_or_unavailable_signed_receipt'}, status=400)
    response['Cache-Control'] = 'no-store'
    return response
