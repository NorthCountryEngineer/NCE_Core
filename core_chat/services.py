import re

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from identity.access import decision, effective_tags, record_decision, require_access
from identity.models import Membership
from identity.policy import authorization_for
from platform_services.data import IdempotencyConflict
from platform_services.models import Installation
from platform_services.services import credential_allows, feature_enabled
from .models import Conversation, Message, Participant


def capability(principal, organization, name):
    if not feature_enabled(organization, 'core-chat'):
        raise PermissionDenied('Core Chat is unavailable.')
    installation = Installation.objects.select_related('service').get(organization=organization, package__slug='core-chat')
    require_access(principal, installation, 'chat.service_access')
    if principal.agent_binding:
        from platform_services.agent_identity import require_permission
        if principal.agent_binding.organization_id != organization.pk:
            raise PermissionDenied('The agent belongs to another organization.')
        require_permission(principal, installation.service, name.replace('.', '-'))
    elif principal.credential:
        # Reload on every operation, including queued agent execution.
        from platform_services.models import WorkloadCredential
        credential = WorkloadCredential.objects.select_related('installation__organization', 'installation__service', 'installation__package').get(pk=principal.credential.pk)
        if credential.installation.organization_id != organization.pk or not credential_allows(credential, [name]):
            raise PermissionDenied('Chat capability has not been granted.')
        require_access(principal, credential, 'chat.application_access')
    else:
        membership = Membership.objects.filter(organization=organization, user=principal.user, active=True, user__is_active=True).first()
        if not membership or name.replace('.', '-') not in authorization_for(membership, installation.service, principal)['permissions']:
            raise PermissionDenied('Chat permission has not been granted.')


def authorize(principal, conversation, operation='read', ai=False):
    capability(principal, conversation.organization, 'chat.' + operation)
    require_access(principal, conversation, 'chat.' + operation)
    if not conversation.active:
        raise PermissionDenied('Conversation is disabled.')
    identity = participant_identity(principal)
    participant = conversation.participants.filter(active=True, **identity).first()
    if not participant or operation == 'send' and participant.role == 'reader':
        record_decision(principal, conversation, 'chat.' + operation, False, 'conversation_membership', [])
        raise PermissionDenied('Conversation membership does not permit this operation.')
    require_access(principal, participant, 'chat.participant_access')
    if ai:
        capability(principal, conversation.organization, 'chat.ai')
        if not conversation.ai_enabled or not participant.allow_ai:
            raise PermissionDenied('AI messaging requires conversation and participant approval.')
    return participant


def can_manage(principal, conversation):
    capability(principal, conversation.organization, 'chat.manage')
    require_access(principal, conversation, 'chat.manage')
    if principal.user and Membership.objects.filter(organization=conversation.organization, user=principal.user,
                                                   active=True, kind='administrator').exists():
        return
    participant = authorize(principal, conversation, 'manage')
    if participant.role != 'owner':
        raise PermissionDenied('Only an owner can change participants.')


@transaction.atomic
def create_conversation(principal, organization, title, ai_enabled=False, connection=None, remote_id=None, *, kind='channel', topic=''):
    capability(principal, organization, 'chat.manage')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160 or type(ai_enabled) is not bool:
        raise ValidationError('Use a title of 1–160 characters and a boolean AI setting.')
    if not isinstance(kind, str) or kind not in {'channel', 'direct', 'group'} or not isinstance(topic, str) or len(topic) > 500:
        raise ValidationError('Choose a conversation type and a topic up to 500 characters.')
    if connection:
        require_access(principal, connection, 'chat.federation_connect')
        if not connection.active:
            raise ValidationError('The federation connection is disabled.')
    conversation = Conversation(organization=organization, title=title.strip(), ai_enabled=ai_enabled,
                                connection=connection, remote_conversation_id=remote_id, kind=kind, topic=topic)
    conversation.full_clean()
    conversation.save()
    from platform_services.environments import principal_environment
    environment = principal_environment(principal)
    if environment:
        conversation.permission_tags.add(environment.boundary_tag)
    identity = participant_identity(principal)
    participant = Participant(conversation=conversation, role='owner', **identity)
    participant.full_clean()
    participant.save()
    record_decision(principal, conversation, 'chat.created', True, 'explicit_creation', [])
    return conversation


def serialize(message, principal=None):
    name = message.agent_name or message.actor_kind.replace('_', ' ').title()
    if message.actor_kind == 'user':
        from identity.models import User
        # Reuse names within a single authorized operation, never across requests.
        names = getattr(principal, '_chat_sender_names', {}) if principal else {}
        key = (message.conversation.organization_id, message.actor_id)
        if key not in names:
            user = User.objects.filter(pk=message.actor_id, memberships__organization_id=key[0]).first()
            names[key] = user.get_full_name() or user.email if user else name
        name = names[key]
        if principal:
            principal._chat_sender_names = names
    parent = message.reply_to if message.reply_to_id else None
    if parent and (not principal or not decision(principal, parent)[0]):
        parent = None
    return {'id': str(message.public_id), 'cursor': message.pk, 'text': message.text,
            'actor_kind': message.actor_kind, 'actor_id': message.actor_id, 'origin_issuer': message.origin_issuer,
            'via_installation_id': str(message.via_installation_id) if message.via_installation_id else None,
            'agent_name': message.agent_name, 'sender_name': name, 'created': message.created.isoformat(),
            'reply_to': str(parent.public_id) if parent else None,
            'reply_preview': parent.text[:240] if parent else ''}


def participant_identity(principal):
    if principal.agent_binding:
        return {'agent_id': principal.agent_binding.account_id}
    return {'installation': principal.credential.installation} if principal.credential else {'user': principal.user}


def message_page(principal, conversation, after=0, limit=100, federation_issuer=''):
    participant = authorize(principal, conversation)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValidationError('Use a nonnegative cursor and limit of 1–200.')
    if federation_issuer:
        validate_peer(principal, conversation, participant, federation_issuer)
    if conversation.connection_id:
        if federation_issuer:
            raise PermissionDenied('Federation targets must hold authoritative conversations.')
        from .federation import relay
        return relay(principal, conversation, 'GET', participant, after=after, limit=limit)
    rows = list(conversation.messages.select_related('conversation', 'reply_to__conversation').filter(pk__gt=after)[:limit])
    visible = []
    for row in rows:
        if decision(principal, row)[0]:
            require_access(principal, row, 'chat.message_read')
            visible.append(serialize(row, principal))
    record_decision(principal, conversation, 'chat.read', True, 'participant_authorized', effective_tags(conversation))
    return {'organization_id': str(conversation.organization_id), 'conversation_id': str(conversation.pk),
            'authority_issuer': settings.ISSUER, 'messages': visible, 'next_cursor': rows[-1].pk if rows else after}


def validate_peer(principal, conversation, participant, issuer):
    capability(principal, conversation.organization, 'chat.federate')
    if not principal.credential or not participant.federated_issuer or participant.federated_issuer != issuer:
        raise PermissionDenied('Federated origin is not approved for this participant.')


def send_message(principal, conversation, request_id, text, *, ai=False, agent_name='', origin=None, via_installation_id=None, reply_to=None):
    if (not isinstance(request_id, str) or not re.fullmatch(r'[-a-zA-Z0-9_:./]{1,160}', request_id) or
            not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384 or '\x00' in text or
            type(ai) is not bool or not isinstance(agent_name, str) or len(agent_name) > 160 or
            (ai and not agent_name.strip()) or (not ai and agent_name)):
        raise ValidationError('Use a request ID, nonempty text up to 16 KiB, and an agent name for AI messages.')
    participant = authorize(principal, conversation, 'send', ai=ai)
    parent = None
    if reply_to is not None:
        from uuid import UUID
        try:
            reply_to = str(UUID(str(reply_to)))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError('Choose a message in this conversation to reply to.')
        authorize(principal, conversation)
        if not conversation.connection_id:
            parent = conversation.messages.filter(public_id=reply_to).first()
            if not parent:
                raise ValidationError('Choose a message in this conversation to reply to.')
            require_access(principal, parent, 'chat.reply')
    user = principal.user
    actor_id = str(principal.agent_binding.account_id if principal.agent_binding else user.pk if user else principal.credential.installation_id)
    kind = 'ai' if ai else 'user' if user else 'application'
    origin_issuer = settings.ISSUER
    sender_key = ('agent:' if principal.agent_binding else 'user:' if user else 'installation:') + actor_id
    via = principal.agent_binding.installation_id if principal.agent_binding else principal.credential.installation_id if principal.credential else via_installation_id
    if principal.agent_binding and not ai:
        raise PermissionDenied('Agent messages must be identified as AI messages.')
    if origin is not None:
        if (not isinstance(origin, dict) or set(origin) != {'issuer', 'actor_id', 'actor_kind'} or
                not isinstance(origin['actor_id'], str) or not re.fullmatch(r'[-a-zA-Z0-9_:./]{1,160}', origin['actor_id']) or
                not isinstance(origin['issuer'], str) or not isinstance(origin['actor_kind'], str) or
                origin['actor_kind'] not in {'user', 'application', 'ai'} or (origin['actor_kind'] == 'ai') != ai):
            raise ValidationError('Invalid federated sender provenance.')
        validate_peer(principal, conversation, participant, origin['issuer'])
        if conversation.connection_id:
            raise PermissionDenied('Federation targets must hold authoritative conversations.')
        actor_id, origin_issuer = origin['actor_id'], origin['issuer']
        kind = 'federated_' + origin['actor_kind']
        # All writers through a peer are isolated from local idempotency identities.
        import hashlib
        sender_key = 'peer:' + hashlib.sha256(f'{via}:{origin_issuer}:{kind}:{actor_id}'.encode()).hexdigest()
    if conversation.connection_id:
        from .federation import relay
        values = {'request_id': request_id, 'text': text, 'ai': ai, 'agent_name': agent_name}
        if reply_to:
            values['reply_to'] = reply_to
        return relay(principal, conversation, 'POST', participant, **values)
    with transaction.atomic():
        # Serialize room writes so cursor order follows committed visibility.
        Conversation.objects.select_for_update().get(pk=conversation.pk)
        message, created = Message.objects.get_or_create(conversation=conversation, sender_key=sender_key, request_id=request_id,
            defaults={'text': text, 'actor_kind': kind, 'actor_id': actor_id, 'origin_issuer': origin_issuer,
                      'via_installation_id': via, 'agent_name': agent_name, 'reply_to': parent})
        if (message.text, message.actor_kind, message.actor_id, message.origin_issuer, message.agent_name, message.via_installation_id) != (text, kind, actor_id, origin_issuer, agent_name, via):
            raise IdempotencyConflict('Message request ID already used with different content.')
        if message.reply_to_id != (parent.pk if parent else None):
            raise IdempotencyConflict('Message request ID already used with a different reply.')
        record_decision(principal, conversation, 'chat.ai_sent' if ai else 'chat.sent', True, 'participant_authorized', effective_tags(conversation))
    return {'organization_id': str(conversation.organization_id), 'conversation_id': str(conversation.pk),
            'authority_issuer': settings.ISSUER, 'message': serialize(message, principal), 'created': created}
