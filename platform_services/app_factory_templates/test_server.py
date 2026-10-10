"""Executable tests shipped with every generated service."""
import io
import json
import secrets
import tempfile
import unittest
from unittest.mock import Mock, patch
import server
from identity_client import Forbidden, IdentityUnavailable, Unauthenticated


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = patch.object(server, 'DATABASE', self.directory.name + '/app.sqlite3')
        self.database.start()
        self.addCleanup(self.database.stop)
        self.client = Mock()
        self.client.require.return_value = {'sub': 'test-subject'}
        self.auth = patch.object(server, 'identity', return_value=self.client)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        server.SESSIONS.clear()
        server.STATES.clear()
        self.collection = next(iter(server.COLLECTIONS))
        self.data = {key: {'string': 'Example', 'integer': 7, 'boolean': True}[kind]
            for key, kind in server.COLLECTIONS[self.collection]['fields'].items()}

    def request(self, method='GET', data=None, token=True, cookie=None, csrf=None, path=None):
        raw = json.dumps(data).encode() if data is not None else b''
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path or '/api/records/' + self.collection,
            'wsgi.input': io.BytesIO(raw), 'CONTENT_LENGTH': str(len(raw)), 'CONTENT_TYPE': 'application/json'}
        if token:
            environ['HTTP_AUTHORIZATION'] = 'Bearer ' + secrets.token_urlsafe(32)
        if cookie:
            environ['HTTP_COOKIE'] = 'core_app_session=' + cookie
        if csrf:
            environ['HTTP_X_CSRF_TOKEN'] = csrf
        response = {}
        def start(status, headers):
            response.update(status=int(status.split()[0]), headers=dict(headers))
        response['data'] = json.loads(b''.join(server.application(environ, start)))
        return response

    def test_no_credentials_denies_data(self):
        self.assertEqual(self.request(token=False)['status'], 401)
        self.client.require.assert_not_called()

    def test_create_read_and_live_collection_authorization(self):
        self.assertEqual(self.request('POST', self.data)['status'], 201)
        result = self.request()
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['data']['records'][0]['data'], self.data)
        args = self.client.require.call_args.args
        self.assertEqual(args[1:], ('app-read', '', 'app_collection', self.collection))
        self.assertEqual(result['headers']['Cache-Control'], 'no-store')

    def test_revocation_and_core_outage_never_expose_records(self):
        self.request('POST', self.data)
        for error, status in [(Forbidden, 403), (Unauthenticated, 401), (IdentityUnavailable, 503)]:
            self.client.require.side_effect = error('Do not expose this error detail')
            result = self.request()
            self.assertEqual(result['status'], status)
            self.assertNotIn('records', result['data'])
            self.assertNotIn('detail', result['data'])

    def test_browser_writes_require_csrf(self):
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        server.SESSIONS[sid] = {'token': secrets.token_urlsafe(32), 'csrf': csrf, 'expires': server.time.time() + 60}
        self.assertEqual(self.request('POST', self.data, token=False, cookie=sid)['status'], 403)
        self.assertEqual(self.request('POST', self.data, token=False, cookie=sid, csrf=csrf)['status'], 201)

    def test_schema_rejects_unknown_fields(self):
        self.assertEqual(self.request('POST', {**self.data, 'unexpected': 'value'})['status'], 400)
        self.assertEqual(self.request()['data']['records'], [])

    def test_accounting_omits_payload_and_tokens(self):
        self.request('POST', self.data)
        with server.connection() as database:
            events = database.execute('SELECT subject, collection, operation, outcome FROM accounting').fetchall()
        self.assertEqual(events, [('test-subject', self.collection, 'POST', 'allowed')])

    def test_callback_requires_matching_state_cookie(self):
        self.assertEqual(self.request(path='/oauth/callback', token=False)['status'], 401)

    def test_integer_and_boolean_are_distinct_and_unknown_collection_denied(self):
        with patch.dict(server.COLLECTIONS, {'numeric': {'fields': {'count': 'integer'}}}):
            self.assertFalse(server.valid_record('numeric', {'count': True}))
            self.assertTrue(server.valid_record('numeric', {'count': 7}))
        self.assertEqual(self.request(path='/api/records/unknown')['status'], 404)

    def test_expired_browser_session_denied(self):
        sid = secrets.token_urlsafe(32)
        server.SESSIONS[sid] = {'token': secrets.token_urlsafe(32), 'csrf': secrets.token_urlsafe(32), 'expires': server.time.time() - 1}
        self.assertEqual(self.request(token=False, cookie=sid)['status'], 401)

    def test_agent_requires_own_live_application_placement_and_collection_scope(self):
        self.client.service_id = 'test-service'
        core = Mock()
        core.agent_authorization.return_value = {'active': True, 'consumer_service_id': 'test-service', 'sub': 'agent-subject'}
        with patch.object(server.CoreClient, 'from_environment', return_value=core):
            token = 'ca_' + secrets.token_urlsafe(32)
            claims = server.authorize_token(self.client, token, 'app-read', 'uat', {'HTTP_X_CORE_TASK_ID': 'approved-task'}, self.collection)
            self.assertEqual(claims['sub'], 'agent-subject')
            self.assertEqual(core.agent_authorization.call_args.kwargs['resource_type'], 'app_collection')
            self.assertEqual(core.agent_authorization.call_args.kwargs['resource_id'], self.collection)
            self.assertEqual(core.agent_authorization.call_args.kwargs['task_id'], 'approved-task')
            core.agent_authorization.return_value = {'active': True, 'consumer_service_id': 'other-app'}
            with self.assertRaises(Unauthenticated):
                server.authorize_token(self.client, token, 'app-read', '', {}, self.collection)
            core.agent_authorization.return_value = {'active': False}
            with self.assertRaises(Unauthenticated):
                server.authorize_token(self.client, token, 'app-read', '', {}, self.collection)

    def test_source_and_ui_use_escaped_text_and_never_execute_specification_code(self):
        source = (server.ROOT / 'ui.js').read_text()
        self.assertIn('textContent', source)
        self.assertNotIn('innerHTML', source)
        self.assertFalse(server.valid_record(self.collection, {'__code__': 'run anything'}))


if __name__ == '__main__':
    unittest.main()
