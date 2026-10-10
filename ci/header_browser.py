"""A separate-origin application consumes the actual Core header APIs in browsers.

Only synthetic disposable credentials enter this server's memory. No endpoint
accepts arbitrary URLs or user tokens, and request logging is disabled.
"""
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from threading import Thread
from urllib.parse import parse_qs, urlparse

from core_client import CoreClient, CoreServiceError
from header_client import HeaderAdapter


@contextmanager
def header_application(instance, slug, email):
    fixture = {}
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def reply(self, code, payload, mime='application/json', cookie=False):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', f'header_test_session={session}; HttpOnly; SameSite=Lax; Path=/')
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            parsed = urlparse(self.path)
            if self.command == 'GET' and parsed.path == '/header.js':
                return self.reply(200, (Path(__file__).resolve().parents[1] / 'platform_services/assets/header.js').read_bytes(), 'text/javascript; charset=utf-8')
            if self.command == 'GET' and parsed.path == '/logo.svg':
                return self.reply(200, b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="12" fill="#e3a428"/><path d="M16 46V18h9l14 18V18h9v28h-9L25 28v18z" fill="#153c31"/></svg>', 'image/svg+xml')
            if self.command == 'GET' and parsed.path == '/':
                markup = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<script src="/header.js" defer></script>'
                    f'<core-header endpoint="/core-header/" organization-id="{fixture["organization_id"]}" csrf-token="{csrf}"></core-header>'
                    '<main><h1>Review workspace</h1><label>Application draft <textarea aria-label="Application draft"></textarea></label></main>')
                return self.reply(200, markup.encode(), 'text/html', cookie=True)
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            sid = cookies['header_test_session'].value if 'header_test_session' in cookies else ''
            if not sid or not secrets.compare_digest(sid, session):
                return self.reply(401, {'error': 'session_required'})
            if not parsed.path.startswith('/core-header/'):
                return self.reply(404, {'error': 'unknown_route'})
            values = None
            if self.command == 'POST':
                if self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-CSRFToken', ''), csrf):
                    return self.reply(403, {'error': 'csrf_required'})
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768:
                    return self.reply(400, {'error': 'invalid_request'})
                values = json.loads(self.rfile.read(length))
            core = CoreClient(instance['origin'] + '/api/platform/v1', fixture['workload_token'], fixture['organization_id'], allow_local_http=True)
            try:
                parameters = parse_qs(parsed.query, max_num_fields=1)
                result = HeaderAdapter(core).handle(self.command, parsed.path.removeprefix('/core-header/'),
                    user_access_token=fixture['user_token'], payload=values, query={key: row[0] for key, row in parameters.items()})
                return self.reply(200, result)
            except CoreServiceError as error:
                return self.reply(getattr(error, 'status', 503), {'error': 'user_tools_unavailable'})
            except (ValueError, TypeError):
                return self.reply(400, {'error': 'invalid_request'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    origin = f'http://127.0.0.1:{server.server_port}'
    started = False
    try:
        fixture.update(json.loads(instance['job']('header-fixture', json.dumps({'slug': slug, 'email': email, 'endpoint': origin}))))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = True
        yield origin, fixture
    finally:
        if started:
            server.shutdown()
        server.server_close()
