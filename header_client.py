"""Allowlisted same-origin backend adapter for the <core-header> component.

The host authenticates its session, enforces CSRF/Origin on POST, and supplies
the session's server-held Core OAuth token. Never accept that token from JSON.
"""
import re
from uuid import UUID


class HeaderAdapter:
    def __init__(self, core):
        self.core = core

    def handle(self, method, path, *, user_access_token, payload=None, query=None):
        query = query or {}
        if not user_access_token or set(query) - {'after'}:
            raise ValueError('Use an authenticated session and a read cursor.')
        if method == 'GET' and path == '' and not query:
            return self.core.header_state(user_access_token=user_access_token)
        if method == 'POST' and path == 'pins/' and not query and isinstance(payload, dict) and set(payload) == {'deployment_id', 'pinned'}:
            return self.core.header_pin(payload['deployment_id'], payload['pinned'], user_access_token=user_access_token)
        match = re.fullmatch(r'chat/([a-fA-F0-9-]{36})/(messages|send|preferences)/', path)
        if not match:
            raise ValueError('Unsupported header route.')
        room, operation = str(UUID(match[1])), match[2]
        if method == 'GET' and operation == 'messages':
            return self.core.header_messages(room, user_access_token=user_access_token, after=query.get('after'))
        if method != 'POST' or query or not isinstance(payload, dict):
            raise ValueError('Unsupported header request.')
        if operation == 'send' and {'request_id', 'text'} <= payload.keys() and not set(payload) - {'request_id', 'text', 'reply_to'}:
            return self.core.header_send(room, payload['request_id'], payload['text'], user_access_token=user_access_token,
                reply_to=payload.get('reply_to'))
        if operation == 'preferences' and payload and not set(payload) - {'cursor', 'notifications'}:
            return self.core.header_preferences(room, payload, user_access_token=user_access_token)
        raise ValueError('Unsupported header fields.')
