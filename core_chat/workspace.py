"""User-specific unread state, checked against live authorization on every request."""
import re

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from identity.access import decision, record_decision
from .federation import FederationUnavailable
from .services import authorize, message_page


def mentioned(text, email):
    return bool(re.search(r'(?<![\w@])@' + re.escape(email) + r'(?!(?:[\w+-]|\.[\w]))', text, re.IGNORECASE))


def workspace_state(principal, organization):
    rooms = []
    # Bound expensive peer calls and policy evaluations. No unread/message cache can
    # outlive a permission, tag, membership, or federation approval change.
    for room in organization.chat_conversations.filter(active=True, participants__active=True,
            participants__user=principal.user).select_related('organization', 'connection').order_by('kind', 'title')[:100]:
        try:
            participant = authorize(principal, room)
            if room.connection_id:
                messages = message_page(principal, room, after=participant.last_read_cursor, limit=200)['messages']
            else:
                # Counts disclose no message bodies. Evaluate every row's tags,
                # but avoid serializing names/previews and writing a read audit
                # for every old message on every five-second inbox poll.
                rows = room.messages.filter(pk__gt=participant.last_read_cursor).select_related('conversation')[:200]
                messages = [{'actor_id': row.actor_id, 'origin_issuer': row.origin_issuer, 'text': row.text}
                            for row in rows if decision(principal, row)[0]]
                record_decision(principal, room, 'chat.activity_read', True, 'participant_authorized', [])
        except PermissionDenied:
            continue
        except FederationUnavailable:
            rooms.append({'id': str(room.pk), 'title': room.title, 'kind': room.kind, 'topic': room.topic,
                          'url': reverse('chat_room', args=[organization.slug, room.pk]),
                          'unread': 0, 'mentions': 0, 'notifications': 0, 'unavailable': True})
            continue
        unread = [m for m in messages if not (
            m['actor_id'] == str(principal.user.pk) and m['origin_issuer'] == settings.ISSUER)]
        mentions = sum(mentioned(m['text'], principal.user.email) for m in unread)
        count = len(unread) if participant.notifications == 'all' else mentions if participant.notifications == 'mentions' else 0
        rooms.append({'id': str(room.pk), 'title': room.title, 'kind': room.kind, 'topic': room.topic,
                      'url': reverse('chat_room', args=[organization.slug, room.pk]), 'unread': len(unread),
                      'mentions': mentions, 'notifications': count, 'unavailable': False,
                      'notification_mode': participant.notifications})
    return {'rooms': rooms, 'unread': sum(r['unread'] for r in rooms),
            'notifications': sum(r['notifications'] for r in rooms)}
