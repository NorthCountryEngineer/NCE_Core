"""Portable organization shell: preferences never confer application access."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse

from identity.access import decision, effective_tags, require_access, record_decision
from identity.models import Membership
from identity.policy import authorization_for
from .models import ApplicationPin, Deployment
from .validation import validate_endpoint


def member(principal, organization):
    if not principal.user or principal.credential or principal.agent_binding:
        raise PermissionDenied('User tools require a signed-in human account.')
    require_access(principal, organization, 'header.user_access')
    membership = Membership.objects.filter(organization=organization, user=principal.user, active=True, user__is_active=True).first()
    if not membership:
        raise PermissionDenied('An active organization membership is required.')
    require_access(principal, membership, 'header.membership_access')
    return membership


def enabled(organization, installation=None):
    return organization.unified_header_enabled if installation is None or installation.header_mode == 'inherit' else installation.header_mode == 'enabled'


def applications(principal, organization, membership):
    rows = []
    pins = set(membership.application_pins.values_list('deployment_id', flat=True))
    # No network requests or health claims: stopped/disabled instances are omitted;
    # health and environment are labels, not permission grants.
    deployments = Deployment.objects.filter(installation__organization=organization, installation__enabled=True,
        installation__service__active=True, installation__package__active=True,
        installation__package__kind='application', desired_state='running').select_related(
        'installation__package', 'installation__service', 'environment', 'node').order_by('installation__package__name', 'name', 'pk')
    for deployment in deployments:
        if deployment.environment_id and not deployment.environment.active or deployment.node_id and not deployment.node.active:
            continue
        try:
            validate_endpoint(deployment.endpoint)
        except ValidationError:
            continue
        if not decision(principal, deployment)[0] or not authorization_for(membership, deployment.installation.service, principal)['permissions']:
            continue
        rows.append({'id': str(deployment.pk), 'name': deployment.installation.package.name,
            'instance': deployment.name, 'url': deployment.endpoint, 'environment': deployment.environment.name if deployment.environment_id else 'unassigned',
            'status': deployment.status, 'pinned': deployment.pk in pins})
        if len(rows) == 100:
            break
    return rows


def context(principal, organization, source=None):
    membership = member(principal, organization)
    if source:
        if not source.credential or source.agent_binding:
            raise PermissionDenied('A registered application backend is required.')
        installation = source.credential.installation
        require_access(source, source.credential, 'header.application_access')
        # A valid OAuth token alone is not an application usage grant.
        if not authorization_for(membership, installation.service, principal)['permissions']:
            raise PermissionDenied('Application access has been revoked.')
    return membership


def state(principal, organization, source=None):
    membership = context(principal, organization, source)
    installation = source.credential.installation if source else None
    result = {'version': 1, 'organization_id': str(organization.pk), 'enabled': enabled(organization, installation),
        'brand': organization.brand, 'organization_name': organization.name,
        'user': {'id': str(principal.user.pk), 'name': principal.user.get_full_name() or principal.user.email},
        'applications': [], 'pins': [], 'tools': [], 'chat': {'enabled': False, 'rooms': [], 'unread': 0, 'notifications': 0}}
    if not result['enabled']:
        return result
    rows = applications(principal, organization, membership)
    from .header_configuration import presentation
    result['appearance'], result['shortcuts'], result['links'] = presentation(organization, principal, rows, source)
    result['environment'] = source.credential.environment.name if source and source.credential.environment_id else 'Core'
    result['applications'] = rows
    visible = {r['id'] for r in rows}
    result['pins'] = [str(pk) for pk in membership.application_pins.values_list('deployment_id', flat=True) if str(pk) in visible]
    result['tools'] = [{'name': 'Organizations', 'url': settings.ISSUER + reverse('home')},
        {'name': 'Password', 'url': settings.ISSUER + reverse('password_change')}]
    from .documentation import require_documentation_capabilities
    try:
        require_documentation_capabilities(principal, organization, ['docs.read'])
        result['tools'].append({'name': 'Documentation', 'url': settings.ISSUER + reverse('documentation', args=[organization.slug])})
    except PermissionDenied:
        pass
    if membership.kind == 'administrator':
        result['tools'].append({'name': 'Administration', 'url': settings.ISSUER + reverse('platform_dashboard', args=[organization.slug])})
    from core_chat.services import capability
    from core_chat.workspace import workspace_state
    try:
        if 'chat' not in result['appearance']['widgets']:
            return result
        capability(principal, organization, 'chat.read')
        if source:
            capability(source, organization, 'chat.read')
        result['chat'] = {'enabled': True, 'url': settings.ISSUER + reverse('chat', args=[organization.slug]),
            **workspace_state(principal, organization, source=source)}
    except PermissionDenied:
        pass
    return result


@transaction.atomic
def pin(principal, organization, values):
    if set(values) != {'deployment_id', 'pinned'} or type(values['pinned']) is not bool:
        raise ValidationError('Choose a deployment and a boolean pinned preference.')
    from uuid import UUID
    try:
        selected = UUID(str(values['deployment_id']))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Choose a deployment ID.')
    membership = member(principal, organization)
    Membership.objects.select_for_update().get(pk=membership.pk)
    if values['pinned']:
        if str(selected) not in {r['id'] for r in applications(principal, organization, membership)}:
            raise PermissionDenied('Application is unavailable.')
        if not membership.application_pins.filter(deployment_id=selected).exists() and membership.application_pins.count() >= 12:
            raise ValidationError('Pin at most twelve applications.')
        ApplicationPin.objects.get_or_create(membership=membership, deployment_id=selected)
    else:
        # Removing an obsolete private bookmark must remain possible after revocation.
        membership.application_pins.filter(deployment_id=selected).delete()
    record_decision(principal, organization, 'header.pin_updated', True, 'private_preference', [])


def chat_room(principal, organization, pk, operation='read', source=None):
    from django.shortcuts import get_object_or_404
    from core_chat.models import Conversation
    from core_chat.services import authorize
    member(principal, organization)
    conversation = get_object_or_404(Conversation, organization=organization, pk=pk)
    if source:
        authorize(source, conversation, operation)
    participant = authorize(principal, conversation, operation)
    return conversation, participant


def chat_messages(principal, organization, pk, after=None, source=None):
    from core_chat.services import message_page
    conversation, _ = chat_room(principal, organization, pk, source=source)
    if after is None:
        last = list(conversation.messages.order_by('-pk').values_list('pk', flat=True)[:100]) if not conversation.connection_id else []
        after = min(last) - 1 if last else 0
    else:
        try:
            after = int(after)
        except (ValueError, TypeError):
            raise ValidationError('Use a nonnegative integer cursor.')
        if not 0 <= after <= 2**63 - 1:
            raise ValidationError('Use a bounded read cursor.')
    page = message_page(principal, conversation, after=after, limit=100)
    if source and not conversation.connection_id:
        permitted = {str(row.public_id) for row in conversation.messages.filter(public_id__in=[m['id'] for m in page['messages']]) if decision(source, row)[0]}
        page['messages'] = [m for m in page['messages'] if m['id'] in permitted]
        for message in page['messages']:
            if message.get('reply_to') and message['reply_to'] not in permitted:
                parent = conversation.messages.filter(public_id=message['reply_to']).first()
                if parent and not decision(source, parent)[0]:
                    message.pop('reply_preview', None)
                    message.pop('reply_to', None)
    from core_chat.services import authorize
    try:
        authorize(principal, conversation, 'send')
        if source:
            authorize(source, conversation, 'send')
        page['can_send'] = True
    except PermissionDenied:
        page['can_send'] = False
    return page


def chat_send(principal, organization, pk, values, source=None):
    from core_chat.services import send_message
    if set(values) - {'text', 'request_id', 'reply_to'} or not {'text', 'request_id'} <= values.keys():
        raise ValidationError('Supply message text and an idempotency key.')
    conversation, _ = chat_room(principal, organization, pk, 'send', source)
    if source and values.get('reply_to') and not conversation.connection_id:
        chat_room(principal, organization, pk, 'read', source)
        from uuid import UUID
        try:
            parent = conversation.messages.get(public_id=UUID(str(values['reply_to'])))
        except (ValueError, conversation.messages.model.DoesNotExist):
            raise ValidationError('Choose a visible reply in this conversation.')
        require_access(source, parent, 'header.reply_access')
    return send_message(principal, conversation, values['request_id'], values['text'], reply_to=values.get('reply_to'),
        via_installation_id=source.credential.installation_id if source else None)


@transaction.atomic
def chat_preferences(principal, organization, pk, values, source=None):
    conversation, participant = chat_room(principal, organization, pk, source=source)
    if set(values) - {'cursor', 'notifications'} or not values:
        raise ValidationError('Choose a read cursor or notification preference.')
    if 'cursor' in values:
        cursor = values['cursor']
        if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
            raise ValidationError('Use a nonnegative integer read cursor.')
        type(participant).objects.filter(pk=participant.pk, last_read_cursor__lt=cursor).update(last_read_cursor=cursor)
    if 'notifications' in values:
        if not isinstance(values['notifications'], str) or values['notifications'] not in {'all', 'mentions', 'muted'}:
            raise ValidationError('Choose a notification mode.')
        type(participant).objects.filter(pk=participant.pk).update(notifications=values['notifications'])
    record_decision(principal, conversation, 'header.chat_preferences_updated', True, 'private_preference', effective_tags(conversation))
    return {'organization_id': str(organization.pk), 'conversation_id': str(pk), 'saved': True}
