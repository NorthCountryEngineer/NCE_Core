import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError

from identity.access import record_decision, require_access


class FederationUnavailable(Exception):
    pass


def validate_issuer(issuer):
    if not isinstance(issuer, str):
        raise ValidationError('Use an approved Core issuer origin.')
    try:
        url = urlparse(issuer)
        url.port
    except ValueError:
        raise ValidationError('Use a valid Core origin.')
    local = settings.DEBUG and url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}
    if (not url.netloc or url.username is not None or url.path or url.query or url.fragment or
            not (local or url.scheme == 'https' and issuer in settings.CORE_CHAT_FEDERATION_ORIGINS)):
        raise ValidationError('Use an HTTPS Core origin explicitly approved by the operator.')
    return issuer


def credential_path(connection):
    root = Path(settings.CORE_CHAT_SECRET_ROOT).resolve() / str(connection.organization_id)
    path = Path(connection.credential_file)
    if not path.is_absolute() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValidationError('Mount the credential within CORE_CHAT_SECRET_ROOT/<organization-uuid>/.')
    return path.resolve()


def validate_connection(connection):
    validate_issuer(connection.issuer)
    if connection.issuer == settings.ISSUER:
        raise ValidationError('A federation connection must target a different Core issuer.')
    credential_path(connection)


def relay(principal, conversation, method, participant, **values):
    connection = conversation.connection
    require_access(principal, connection, 'chat.federation_access')
    if not connection.active:
        raise PermissionDenied('Federation connection is disabled.')
    validate_connection(connection)
    suffix = '/send/' if method == 'POST' else '/messages/'
    url = connection.issuer + '/api/platform/v1/chat/conversations/' + str(conversation.remote_conversation_id) + suffix
    try:
        token = credential_path(connection).read_text().strip()
        if not token or len(token) > 256 or any(c.isspace() for c in token):
            raise ValueError
        headers = {'Authorization': 'Bearer ' + token, 'X-Core-Chat-Federation': settings.ISSUER}
        if principal.purpose:
            headers['X-Core-Purpose'] = principal.purpose
        if method == 'POST':
            actor_id = str(principal.agent_binding.account_id if principal.agent_binding else principal.user.pk if principal.user else principal.credential.installation_id)
            kind = 'ai' if values['ai'] else 'user' if principal.user else 'application'
            values['origin'] = {'issuer': settings.ISSUER, 'actor_id': actor_id, 'actor_kind': kind}
            values['request_id'] = hashlib.sha256(f"{conversation.pk}:{kind}:{actor_id}:{values['request_id']}".encode()).hexdigest()
        with requests.request(method, url, headers=headers, params=values if method == 'GET' else None,
                              json=values if method == 'POST' else None, timeout=(5, 10), allow_redirects=False, stream=True) as response:
            if response.status_code in {401, 403, 404}:
                raise PermissionDenied('The authoritative instance denied access.')
            if response.status_code == 409:
                from platform_services.data import IdempotencyConflict
                raise IdempotencyConflict('Remote message request ID conflict.')
            if response.status_code not in {200, 201}:
                raise ValueError
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > 4 * 1024 * 1024:
                    raise ValueError
                chunks.append(chunk)
            result = json.loads(b''.join(chunks))
            if (not isinstance(result, dict) or result.get('organization_id') != str(connection.remote_organization_id) or
                    result.get('conversation_id') != str(conversation.remote_conversation_id) or
                    result.get('authority_issuer') != connection.issuer):
                raise ValueError
            if method == 'GET':
                if (not isinstance(result.get('messages'), list) or len(result['messages']) > values['limit'] or
                        type(result.get('next_cursor')) is not int or result['next_cursor'] < values['after']):
                    raise ValueError
                messages = result['messages']
            else:
                if type(result.get('created')) is not bool:
                    raise ValueError
                messages = [result.get('message')]
            from uuid import UUID
            for message in messages:
                if (not isinstance(message, dict) or not isinstance(message.get('text'), str) or len(message['text'].encode()) > 16384 or
                        type(message.get('cursor')) is not int or message['cursor'] < 1 or
                        message.get('actor_kind') not in {'user', 'application', 'ai', 'federated_user', 'federated_application', 'federated_ai'} or
                        not all(isinstance(message.get(k), str) for k in ['actor_id', 'origin_issuer', 'agent_name', 'created'])):
                    raise ValueError
                UUID(message['id'])
        record_decision(principal, conversation, 'chat.federated_' + ('read' if method == 'GET' else 'sent'), True, 'authority_verified', [])
        result['conversation_id'] = str(conversation.pk)
        result['organization_id'] = str(conversation.organization_id)
        result['remote_conversation_id'] = str(conversation.remote_conversation_id)
        return result
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
        raise FederationUnavailable('The authoritative chat instance is unavailable or returned an invalid response.') from error
