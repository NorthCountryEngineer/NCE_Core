from functools import wraps
import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from oauth2_provider.models import get_access_token_model

from identity.access import Principal, evidence_for, require_access
from identity.views import token_claims
from platform_services.api import bearer, body, response
from platform_services.data import IdempotencyConflict
from platform_services.models import Installation, WorkloadCredential
from platform_services.services import credential_allows
from .federation import FederationUnavailable
from .models import Conversation, Participant
from .services import authorize, can_manage, capability, create_conversation, message_page, send_message


def chat_api(operation, methods=('GET',)):
    def decorate(view):
        @csrf_exempt
        @require_http_methods(methods)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            raw = bearer(request)
            credential = WorkloadCredential.objects.select_related('installation__organization', 'installation__service', 'installation__package').filter(
                token_hash=WorkloadCredential.digest(raw), active=True).first() if raw else None
            purpose = request.headers.get('X-Core-Purpose', '')
            if credential:
                if not credential_allows(credential, ['chat.' + operation]):
                    return response({'error': 'insufficient_permission'}, 403)
                organization = credential.installation.organization
                principal = Principal(credential=credential, purpose=purpose)
                delegated = request.headers.get('X-Core-User-Token')
                if delegated:
                    token = get_access_token_model().objects.select_related('application__service__organization', 'user').filter(token_checksum=hashlib.sha256(delegated.encode()).hexdigest()).first()
                    if (not token or not token.application or token.application.service_id != credential.installation.service_id or
                            'authorization' not in token.scopes or not (token_claims(delegated, token.application, purpose) or {}).get('active')):
                        return response({'error': 'invalid_user_token'}, 401)
                    principal = Principal(user=token.user, evidence=evidence_for(delegated), purpose=purpose)
            else:
                token = get_access_token_model().objects.select_related('application__service__organization', 'user').filter(token_checksum=hashlib.sha256(raw.encode()).hexdigest()).first() if raw else None
                if (not token or not token.application or token.application.service.slug != 'core-chat' or
                        'authorization' not in token.scopes or not (token_claims(raw, token.application, purpose) or {}).get('active')):
                    return response({'error': 'invalid_credential'}, 401)
                organization = token.application.service.organization
                principal = Principal(user=token.user, evidence=evidence_for(raw), purpose=purpose)
            request.organization = organization
            request.access_principal = principal
            principal.request_id = getattr(request, 'security_request_id', None)
            request.chat_credential = credential
            try:
                if credential:
                    # Delegation retains the calling application's independent policy checks.
                    require_access(Principal(credential=credential, purpose=purpose), credential, 'chat.api_call')
                capability(principal, organization, 'chat.' + operation)
                return view(request, *args, **kwargs)
            except ValidationError as error:
                return response({'error': 'invalid_request', 'detail': error.messages[0]}, 400)
            except PermissionDenied:
                return response({'error': 'insufficient_permission'}, 403)
            except IdempotencyConflict:
                return response({'error': 'idempotency_conflict'}, 409)
            except FederationUnavailable:
                return response({'error': 'chat_authority_unavailable'}, 503)
        return wrapped
    return decorate


def room(request, pk, operation, ai=False):
    conversation = get_object_or_404(Conversation, organization=request.organization, pk=pk)
    if request.chat_credential and request.access_principal.user:
        source = Principal(credential=request.chat_credential, purpose=request.access_principal.purpose,
                           request_id=request.access_principal.request_id)
        authorize(source, conversation, operation, ai=ai)
        from identity.access import record_decision, effective_tags
        record_decision(source, conversation, 'chat.delegated_call', True, 'user_and_application_authorized', effective_tags(conversation))
    return conversation


@chat_api('read')
def conversations(request):
    principal = request.access_principal
    rows = []
    for conversation in Conversation.objects.filter(organization=request.organization, active=True).order_by('created'):
        try:
            if request.chat_credential and principal.user:
                authorize(Principal(credential=request.chat_credential, purpose=principal.purpose), conversation)
            authorize(principal, conversation)
        except PermissionDenied:
            continue
        rows.append({'id': str(conversation.pk), 'title': conversation.title, 'ai_enabled': conversation.ai_enabled,
                     'shared': bool(conversation.connection_id)})
        if len(rows) == 200:
            break
    return response({'organization_id': str(request.organization.pk), 'conversations': rows})


@chat_api('manage', ('POST',))
def create(request):
    values = body(request)
    if set(values) - {'title', 'ai_enabled'}:
        raise ValidationError('Unsupported conversation fields.')
    conversation = create_conversation(request.access_principal, request.organization, values.get('title'), values.get('ai_enabled', False))
    if request.chat_credential and request.access_principal.user:
        Participant.objects.create(conversation=conversation, installation=request.chat_credential.installation)
    return response({'organization_id': str(request.organization.pk), 'conversation_id': str(conversation.pk)}, 201)


@chat_api('read')
def messages(request, pk):
    conversation = room(request, pk, 'read')
    try:
        after, limit = int(request.GET.get('after', 0)), int(request.GET.get('limit', 100))
    except ValueError:
        raise ValidationError('Invalid message cursor or limit.')
    return response(message_page(request.access_principal, conversation, after, limit,
                                 federation_issuer=request.headers.get('X-Core-Chat-Federation', '')))


@chat_api('send', ('POST',))
def send(request, pk):
    values = body(request)
    if set(values) - {'request_id', 'text', 'ai', 'agent_name', 'origin'}:
        raise ValidationError('Unsupported message fields; sender identity comes from authentication.')
    ai = values.get('ai', False)
    conversation = room(request, pk, 'send', ai=ai)
    if ai and request.chat_credential and not credential_allows(request.chat_credential, ['chat.ai']):
        raise PermissionDenied
    origin = values.get('origin')
    issuer = request.headers.get('X-Core-Chat-Federation', '')
    if (origin is not None and not isinstance(origin, dict) or bool(origin) != bool(issuer) or
            origin and origin.get('issuer') != issuer):
        raise ValidationError('Federation header and sender provenance must agree.')
    result = send_message(request.access_principal, conversation, values.get('request_id'), values.get('text'),
                          ai=ai, agent_name=values.get('agent_name', ''), origin=origin,
                          via_installation_id=request.chat_credential.installation_id if request.chat_credential else None)
    return response(result, 201 if result['created'] else 200)


@chat_api('manage', ('POST',))
def participant(request, pk):
    conversation = room(request, pk, 'manage')
    can_manage(request.access_principal, conversation)
    values = body(request)
    if set(values) - {'user_id', 'installation_id', 'role', 'allow_ai', 'active', 'federated_issuer'}:
        raise ValidationError('Unsupported participant fields.')
    if bool(values.get('user_id')) == bool(values.get('installation_id')):
        raise ValidationError('Choose exactly one user or installation.')
    identity = {'user_id': values['user_id']} if values.get('user_id') else {'installation_id': values['installation_id']}
    try:
        existing = conversation.participants.filter(**identity).first()
        item = existing or Participant(conversation=conversation, **identity)
        for field in ['role', 'allow_ai', 'active', 'federated_issuer']:
            if field in values:
                setattr(item, field, values[field])
        if type(item.allow_ai) is not bool or type(item.active) is not bool:
            raise ValidationError('Participant flags must be boolean.')
        if existing:
            require_access(request.access_principal, existing, 'chat.participant_manage')
        item.full_clean()
        item.save()
    except (ValueError, TypeError, Installation.DoesNotExist):
        raise ValidationError('Invalid participant identity.')
    from identity.access import record_decision
    record_decision(request.access_principal, item, 'chat.participant_updated', True, 'owner_approved', [])
    return response({'participant_id': item.pk}, 200 if existing else 201)
