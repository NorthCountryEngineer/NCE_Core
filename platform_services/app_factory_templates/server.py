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
        start_response(status, [('Content-Type', mime), ('Content-Length', str(len(payload))), ('Cache-Control', 'no-store'),
            ('X-Content-Type-Options', 'nosniff'), ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
            ('Referrer-Policy', 'no-referrer'), *headers])
        return [payload]

    method, path = environ.get('REQUEST_METHOD', 'GET'), environ.get('PATH_INFO', '/')
    try:
        if method == 'GET' and path == '/health':
            return reply('200 OK', {'status': 'ok'})
        if method == 'GET' and path in {'/', '/ui.js'}:
            name = 'index.html' if path == '/' else 'ui.js'
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
        if method == 'GET' and path == '/api/spec':
            authorize_token(client, token, 'app-read', purpose, environ)
            return reply('200 OK', {'name': SPEC['name'], 'resources': SPEC['resources'], 'csrf': session['csrf'] if session else None})
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
            with connection() as database:
                rows = database.execute('SELECT id, data FROM records WHERE collection=? ORDER BY rowid DESC LIMIT 100', (collection,)).fetchall()
                database.execute('INSERT INTO accounting VALUES (?, ?, ?, ?, ?, ?)',
                    (str(uuid.uuid4()), claims['sub'], collection, method, 'allowed', int(time.time())))
            return reply('200 OK', {'records': [{'id': row[0], 'data': json.loads(row[1])} for row in rows]})
        length = int(environ.get('CONTENT_LENGTH') or 0)
        if length <= 0 or length > 32768 or environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            return reply('400 Bad Request', {'error': 'bounded_json_required'})
        data = json.loads(environ['wsgi.input'].read(length))
        if not valid_record(collection, data):
            return reply('400 Bad Request', {'error': 'invalid_record'})
        record_id = str(uuid.uuid4())
        with connection() as database:
            database.execute('BEGIN IMMEDIATE')
            if database.execute('SELECT COUNT(*) FROM records WHERE collection=?', (collection,)).fetchone()[0] >= 10000:
                return reply('409 Conflict', {'error': 'collection_capacity_reached'})
            database.execute('INSERT INTO records VALUES (?, ?, ?)', (record_id, collection, json.dumps(data, allow_nan=False)))
            database.execute('INSERT INTO accounting VALUES (?, ?, ?, ?, ?, ?)',
                (str(uuid.uuid4()), claims['sub'], collection, method, 'allowed', int(time.time())))
        return reply('201 Created', {'id': record_id})
    except Unauthenticated:
        return reply('401 Unauthorized', {'error': 'invalid_token'})
    except Forbidden:
        return reply('403 Forbidden', {'error': 'permission_denied'})
    except (IdentityUnavailable, CoreServiceError, requests.RequestException, KeyError, sqlite3.Error):
        return reply('503 Service Unavailable', {'error': 'service_unavailable'})
    except (ValueError, TypeError, UnicodeDecodeError):
        return reply('400 Bad Request', {'error': 'invalid_request'})


if __name__ == '__main__':
    from wsgiref.simple_server import make_server, WSGIRequestHandler
    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass  # Query strings can contain OAuth codes. Do not log request URLs.
    make_server('127.0.0.1', 8080, application, handler_class=QuietHandler).serve_forever()
