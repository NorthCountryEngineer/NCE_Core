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
        self.rules = Mock()
        self.rules.read.return_value = {'rules': {'query': {'max_rows': 100, 'timeout_ms': 10000}}}
        self.rules.execute.side_effect = lambda key, operation, estimate, callback, **kwargs: callback(estimate, kwargs.get('query'))[0]
        self.rules_patch = patch.object(server, 'inherited_rules', return_value=self.rules)
        self.rules_patch.start()
        self.addCleanup(self.rules_patch.stop)
        server.SESSIONS.clear()
        server.STATES.clear()
        self.collection = next(iter(server.COLLECTIONS))
        self.data = {key: {'string': 'Example', 'integer': 7, 'boolean': True}[kind]
            for key, kind in server.COLLECTIONS[self.collection]['fields'].items()}

    def request(self, method='GET', data=None, token=True, cookie=None, csrf=None, path=None, query=''):
        raw = json.dumps(data).encode() if data is not None else b''
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path or '/api/records/' + self.collection, 'QUERY_STRING': query,
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

    def test_documentation_requires_live_role_and_bound_delegation(self):
        with patch.object(server.CoreClient, 'from_environment') as factory, patch.object(server, 'OrganizationDocumentation') as adapter:
            adapter.return_value.search.return_value = {'documents': []}
            result = self.request(path='/api/documentation')
            self.assertEqual(result['status'], 200)
            self.assertEqual(self.client.require.call_args.args[1], 'docs-read')
            self.assertTrue(adapter.call_args.kwargs['user_access_token'])
            self.assertEqual(adapter.call_args.args[0], factory.return_value)
            self.client.require.side_effect = Forbidden('Revoked role')
            adapter.reset_mock()
            self.assertEqual(self.request(path='/api/documentation')['status'], 403)
            adapter.assert_not_called()

    def test_documentation_requires_authentication_and_core_availability(self):
        self.assertEqual(self.request(path='/api/documentation', token=False)['status'], 401)
        with patch.object(server.CoreClient, 'from_environment', side_effect=server.CoreServiceError('Authority unavailable')):
            result = self.request(path='/api/documentation')
            self.assertEqual(result['status'], 503)
            self.assertNotIn('documents', result['data'])

    def test_create_read_and_live_collection_authorization(self):
        self.assertEqual(self.request('POST', self.data)['status'], 201)
        result = self.request()
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['data']['records'][0]['data'], self.data)
        args = self.client.require.call_args.args
        self.assertEqual(args[1:], ('app-read', '', 'app_collection', self.collection))
        self.assertEqual(result['headers']['Cache-Control'], 'no-store')

    def test_shared_quota_denial_prevents_read_and_write(self):
        error = server.CoreServiceError('Central quota denied')
        error.code, error.metric = 'quota_exceeded', 'requests'
        self.rules.execute.side_effect = error
        self.assertEqual(self.request('POST', self.data)['status'], 429)
        self.assertEqual(self.request()['data'], {'error': 'quota_exceeded', 'metric': 'requests', 'reset_at': None})
        with server.connection() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_inherited_query_cap_and_cursor(self):
        for _ in range(3):
            self.assertEqual(self.request('POST', self.data)['status'], 201)
        self.rules.read.return_value['rules']['query']['max_rows'] = 1
        first = self.request()['data']
        second = self.request(query='after=' + str(first['next_cursor']))['data']
        self.assertEqual(len(first['records']), 1)
        self.assertNotEqual(first['records'][0]['id'], second['records'][0]['id'])
        self.assertEqual(self.rules.execute.call_args.kwargs['query']['rows'], 1)

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
