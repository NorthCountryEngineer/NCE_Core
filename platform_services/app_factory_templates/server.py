"""Trusted generated-app runtime. All business requests check live Core policy."""
import base64
import hashlib
from http.cookies import SimpleCookie
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

import requests
from identity_client import ResourceClient, Unauthenticated, Forbidden, IdentityUnavailable
from core_client import CoreClient, CoreServiceError
from header_client import HeaderAdapter
from documentation_client import OrganizationDocumentation
from ai_rules_client import AIRules

ROOT = Path(__file__).resolve().parent
SPEC = json.loads((ROOT / 'app-spec.json').read_text())
COLLECTIONS = {item['slug']: item for item in SPEC['resources']}
IDENTITY = None
STATES = {}
SESSIONS = {}
DATABASE = os.environ.get('APP_DATABASE', '/tmp/app.sqlite3')


def identity():
    global IDENTITY
    if IDENTITY is None:
        IDENTITY = ResourceClient.from_environment()
        if IDENTITY.organization_id != SPEC['organization_id'] or not IDENTITY.environment or not IDENTITY.deployment_id:
            raise IdentityUnavailable('A platform deployment identity in this organization is required.')
    return IDENTITY


@contextmanager
def connection():
    database = sqlite3.connect(DATABASE)
    database.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, collection TEXT NOT NULL, data TEXT NOT NULL)')
    database.execute('CREATE INDEX IF NOT EXISTS records_collection_cursor ON records(collection)')
    database.execute('CREATE TABLE IF NOT EXISTS accounting (id TEXT PRIMARY KEY, subject TEXT, collection TEXT, operation TEXT, outcome TEXT, created INTEGER)')
    try:
        with database:
            yield database
    finally:
        database.close()


def account(subject, collection, operation, outcome):
    # Payloads, prompts, authorization headers, and credentials never enter accounting.
    with connection() as database:
        database.execute('INSERT INTO accounting VALUES (?, ?, ?, ?, ?, ?)',
            (str(uuid.uuid4()), subject, collection, operation, outcome, int(time.time())))


def inherited_rules(token, purpose):
    core = CoreClient.from_environment()
    core.purpose_code = purpose
    if token.startswith('ca_'):
        core.token = token
    return AIRules(core, user_access_token=None if token.startswith('ca_') else token)


def valid_record(collection, data):
    fields = COLLECTIONS[collection]['fields']
    if not isinstance(data, dict) or set(data) != set(fields):
        return False
    kinds = {'string': str, 'integer': int, 'boolean': bool}
    return all(type(data[key]) is kinds[kind] and (kind != 'string' or len(data[key]) <= 2000) and
               (kind != 'integer' or abs(data[key]) <= 2**53 - 1) for key, kind in fields.items())


def authorize_token(client, token, permission, purpose, environ, collection=None):
    if not token.startswith('ca_'):
        return client.require(token, permission, purpose, 'app_collection' if collection else '', collection or '')
    core = CoreClient.from_environment()
    core.purpose_code = purpose
    value = core.agent_authorization(token, audience=f'urn:core:service:{client.service_id}', issuer=client.issuer,
        environment=client.environment, task_id=environ.get('HTTP_X_CORE_TASK_ID'), permission=permission,
        resource_type='app_collection' if collection else None, resource_id=collection)
    if not value.get('active') or value.get('consumer_service_id') != client.service_id:
        raise Unauthenticated('Agent placement is not approved for this application.')
    return value


def origin():
    value = os.environ['CORE_APPLICATION_ORIGIN'].rstrip('/')
    parsed = urlparse(value)
    local = os.environ.get('CORE_ALLOW_LOCAL_HTTP') == '1' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}
    if not parsed.netloc or parsed.username or parsed.path or parsed.query or parsed.fragment or not (parsed.scheme == 'https' or local and parsed.scheme == 'http'):
        raise IdentityUnavailable('A configured HTTPS application origin is required.')
    return value


def cookie(value, age=900):
    return f'core_app_session={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}' + ('; Secure' if origin().startswith('https://') else '')


def prune(store):
    now = time.time()
    for key in list(store):
        if store[key]['expires'] < now:
            store.pop(key, None)
    if len(store) >= 1024:
        raise IdentityUnavailable('Session capacity reached.')


def application(environ, start_response):
    def reply(status, value, headers=(), mime='application/json'):
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        local_images = ' http://localhost:* http://127.0.0.1:* http://[::1]:*' if os.environ.get('CORE_ALLOW_LOCAL_HTTP') == '1' else ''
        start_response(status, [('Content-Type', mime), ('Content-Length', str(len(payload))), ('Cache-Control', 'no-store'),
            ('X-Content-Type-Options', 'nosniff'), ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:" + local_images + "; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
            ('Referrer-Policy', 'no-referrer'), *headers])
        return [payload]

    method, path = environ.get('REQUEST_METHOD', 'GET'), environ.get('PATH_INFO', '/')
    try:
        if method == 'GET' and path == '/health':
            return reply('200 OK', {'status': 'ok'})
        if method == 'GET' and path in {'/', '/ui.js', '/header.js'}:
            name = 'index.html' if path == '/' else path.removeprefix('/')
            return reply('200 OK', (ROOT / name).read_bytes(), mime='text/html' if path == '/' else 'application/javascript')
        cookies = SimpleCookie()
        cookies.load(environ.get('HTTP_COOKIE', ''))
        sid = cookies['core_app_session'].value if 'core_app_session' in cookies else ''
        if method == 'GET' and path == '/login':
            client = identity()
            prune(STATES)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            STATES[state] = {'verifier': verifier, 'expires': time.time() + 300}
            url = client.issuer + '/oauth/authorize/?' + urlencode({'client_id': client.client_id, 'response_type': 'code',
                'redirect_uri': origin() + '/oauth/callback', 'scope': 'openid authorization', 'state': state,
                'code_challenge': challenge, 'code_challenge_method': 'S256'})
            return reply('302 Found', {}, [('Location', url), ('Set-Cookie', cookie(state, 300))])
        if method == 'GET' and path == '/oauth/callback':
            values = parse_qs(environ.get('QUERY_STRING', ''), max_num_fields=20)
            state = values.get('state', [''])[0]
            if not state or not sid or not secrets.compare_digest(state, sid):
                return reply('401 Unauthorized', {'error': 'invalid_login_state'})
            pending = STATES.pop(state, None)
            if not pending or pending['expires'] < time.time() or not values.get('code'):
                return reply('401 Unauthorized', {'error': 'expired_login'})
            client = identity()
            result = requests.post(client.issuer + '/oauth/token/', data={'grant_type': 'authorization_code',
                'code': values['code'][0], 'redirect_uri': origin() + '/oauth/callback', 'code_verifier': pending['verifier'],
                'client_id': client.client_id, 'client_secret': client.client_secret}, timeout=5, allow_redirects=False)
            token = result.json().get('access_token') if result.status_code == 200 else None
            if not isinstance(token, str) or not 1 <= len(token) <= 8192:
                raise Unauthenticated('Login failed.')
            client.require(token, 'app-read')
            prune(SESSIONS)
            session_id = secrets.token_urlsafe(32)
            SESSIONS[session_id] = {'token': token, 'csrf': secrets.token_urlsafe(32), 'expires': time.time() + 900}
            return reply('302 Found', {}, [('Location', '/'), ('Set-Cookie', cookie(session_id))])
        prune(SESSIONS)
        session = SESSIONS.get(sid)
        auth = environ.get('HTTP_AUTHORIZATION', '')
        token = auth[7:] if auth.startswith('Bearer ') else session['token'] if session else None
        if not token:
            return reply('401 Unauthorized', {'error': 'authentication_required'})
        client = identity()
        purpose = environ.get('HTTP_X_CORE_PURPOSE', '')
        if path.startswith('/core-header/'):
            if not session or auth:
                return reply('401 Unauthorized', {'error': 'session_required'})
            authorize_token(client, session['token'], 'app-read', purpose, environ)
            payload = None
            if method == 'POST':
                if (environ.get('HTTP_ORIGIN') != origin() or
                        not secrets.compare_digest(environ.get('HTTP_X_CSRFTOKEN', ''), session['csrf'])):
                    return reply('403 Forbidden', {'error': 'csrf_required'})
                length = int(environ.get('CONTENT_LENGTH') or 0)
                if not 0 < length <= 32768 or environ.get('CONTENT_TYPE') != 'application/json':
                    raise ValueError('Use bounded JSON.')
                payload = json.loads(environ['wsgi.input'].read(length))
            parameters = parse_qs(environ.get('QUERY_STRING', ''), max_num_fields=1)
            if any(len(values) != 1 for values in parameters.values()):
                raise ValueError('Use one read cursor.')
            core = CoreClient.from_environment()
            core.purpose_code = purpose
            try:
                value = HeaderAdapter(core).handle(method, path.removeprefix('/core-header/'), user_access_token=session['token'],
                    payload=payload, query={key: values[0] for key, values in parameters.items()})
            except CoreServiceError as error:
                code = getattr(error, 'status', 503)
                statuses = {400: '400 Bad Request', 401: '401 Unauthorized', 403: '403 Forbidden', 404: '404 Not Found', 409: '409 Conflict', 503: '503 Service Unavailable'}
                return reply(statuses.get(code, '503 Service Unavailable'), {'error': 'user_tools_unavailable'})
            return reply('200 OK', value)
        if method == 'GET' and path == '/api/spec':
            authorize_token(client, token, 'app-read', purpose, environ)
            return reply('200 OK', {'name': SPEC['name'], 'resources': SPEC['resources'], 'organization_id': SPEC['organization_id'],
                'csrf': session['csrf'] if session else None})
        if method == 'GET' and path == '/api/documentation':
            authorize_token(client, token, 'docs-read', purpose, environ)
            parameters = parse_qs(environ.get('QUERY_STRING', ''), max_num_fields=4)
            if set(parameters) - {'q', 'limit', 'after', 'document_id'} or any(len(values) != 1 for values in parameters.values()):
                raise ValueError('Use bounded documentation parameters.')
            core = CoreClient.from_environment()
            core.purpose_code = purpose
            if token.startswith('ca_'):
                # Agent identity was just checked against this application's live placement.
                core.token = token
            docs = OrganizationDocumentation(core, user_access_token=None if token.startswith('ca_') else token)
            if 'document_id' in parameters:
                value = docs.read(parameters['document_id'][0])
            else:
                value = docs.search(parameters.get('q', [''])[0], limit=int(parameters.get('limit', ['20'])[0]),
                    after=parameters.get('after', [None])[0])
            return reply('200 OK', value)
        if method == 'POST' and path == '/logout':
            if not session or not secrets.compare_digest(environ.get('HTTP_X_CSRF_TOKEN', ''), session['csrf']):
                return reply('403 Forbidden', {'error': 'csrf_required'})
            SESSIONS.pop(sid, None)
            return reply('200 OK', {}, [('Set-Cookie', cookie('', 0))])
        collection = path.removeprefix('/api/records/')
        if not path.startswith('/api/records/') or collection not in COLLECTIONS:
            return reply('404 Not Found', {'error': 'not_found'})
        if method not in {'GET', 'POST'}:
            return reply('405 Method Not Allowed', {'error': 'method_not_allowed'}, [('Allow', 'GET, POST')])
        if method == 'POST' and not auth.startswith('Bearer ') and (not session or not secrets.compare_digest(environ.get('HTTP_X_CSRF_TOKEN', ''), session['csrf'])):
            return reply('403 Forbidden', {'error': 'csrf_required'})
        permission = 'app-read' if method == 'GET' else 'app-write'
        try:
            claims = authorize_token(client, token, permission, purpose, environ, collection)
        except (Unauthenticated, Forbidden, IdentityUnavailable):
            account(None, collection, method, 'denied')
            raise
        if method == 'GET':
            parameters = parse_qs(environ.get('QUERY_STRING', ''), keep_blank_values=True)
            if set(parameters) - {'after', 'limit'} or any(len(values) != 1 for values in parameters.values()):
                raise ValueError('Use cursor and limit parameters.')
            after = int(parameters.get('after', [str(2 ** 63 - 1)])[0])
            requested = int(parameters.get('limit', ['100'])[0])
            if not 0 <= after <= 2 ** 63 - 1 or not 1 <= requested <= 100:
                raise ValueError('Invalid query bounds.')
            rules = inherited_rules(token, purpose)
            policy = rules.read()['rules']['query']
            limit = min(requested, policy['max_rows'])
            def read_records(bounds, plan):
                with connection() as database:
                    deadline = time.monotonic() + plan['timeout_ms'] / 1000
                    database.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
                    rows = database.execute('SELECT rowid, id, data FROM records WHERE collection=? AND rowid<? ORDER BY rowid DESC LIMIT ?',
                        (collection, after, plan['rows'])).fetchall()
                    database.set_progress_handler(None, 0)
                    database.execute('INSERT INTO accounting VALUES (?, ?, ?, ?, ?, ?)',
                        (str(uuid.uuid4()), claims['sub'], collection, method, 'allowed', int(time.time())))
                return {'records': [{'id': row[1], 'data': json.loads(row[2])} for row in rows],
                    'next_cursor': rows[-1][0] if rows else after, 'effective_limit': limit}, {'requests': 1, 'query_rows': len(rows)}
            value = rules.execute(uuid.uuid4().hex, 'query', {'requests': 1, 'query_rows': limit}, read_records,
                query={'rows': limit, 'timeout_ms': policy['timeout_ms'], 'paginated': True, 'indexed_filters': True})
            return reply('200 OK', value)
        length = int(environ.get('CONTENT_LENGTH') or 0)
        if length <= 0 or length > 32768 or environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            return reply('400 Bad Request', {'error': 'bounded_json_required'})
        data = json.loads(environ['wsgi.input'].read(length))
        if not valid_record(collection, data):
            return reply('400 Bad Request', {'error': 'invalid_record'})
        record_id = str(uuid.uuid4())
        def create_record(bounds, plan):
            with connection() as database:
                database.execute('BEGIN IMMEDIATE')
                if database.execute('SELECT COUNT(*) FROM records WHERE collection=?', (collection,)).fetchone()[0] >= 10000:
                    return {'error': 'collection_capacity_reached'}, {'requests': 1}
                database.execute('INSERT INTO records VALUES (?, ?, ?)', (record_id, collection, json.dumps(data, allow_nan=False)))
                database.execute('INSERT INTO accounting VALUES (?, ?, ?, ?, ?, ?)',
                    (str(uuid.uuid4()), claims['sub'], collection, method, 'allowed', int(time.time())))
            return {'id': record_id}, {'requests': 1}
        value = inherited_rules(token, purpose).execute(uuid.uuid4().hex, 'workflow', {'requests': 1}, create_record)
        return reply('409 Conflict' if 'error' in value else '201 Created', value)
    except Unauthenticated:
        return reply('401 Unauthorized', {'error': 'invalid_token'})
    except Forbidden:
        return reply('403 Forbidden', {'error': 'permission_denied'})
    except CoreServiceError as error:
        code = getattr(error, 'code', 'service_unavailable')
        if code == 'quota_exceeded':
            return reply('429 Too Many Requests', {'error': code, 'metric': getattr(error, 'metric', None), 'reset_at': getattr(error, 'reset_at', None)})
        if code == 'insufficient_permission':
            return reply('403 Forbidden', {'error': 'permission_denied'})
        if code in {'operation_disabled', 'paid_calls_disabled', 'usage_overrun_requires_review', 'query_plan_rejected'}:
            return reply('409 Conflict', {'error': code})
        return reply('503 Service Unavailable', {'error': 'service_unavailable'})
    except (IdentityUnavailable, requests.RequestException, KeyError, sqlite3.Error):
        return reply('503 Service Unavailable', {'error': 'service_unavailable'})
    except (ValueError, TypeError, UnicodeDecodeError):
        return reply('400 Bad Request', {'error': 'invalid_request'})


if __name__ == '__main__':
    from wsgiref.simple_server import make_server, WSGIRequestHandler
    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass  # Query strings can contain OAuth codes. Do not log request URLs.
    make_server('127.0.0.1', 8080, application, handler_class=QuietHandler).serve_forever()
