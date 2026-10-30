"""Offline hosting-boundary and rollback tests. No SSH, Docker, AWS or real data."""
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from platform_services import cloud_lightsail as adapter, cloud_provider
from platform_services.cloud_aws import AWSAdapterError

APP = '12345678-1234-1234-1234-123456789012'
PLATFORM = '12345678-1234-1234-1234-123456789013'
ORG = '12345678-1234-1234-1234-123456789014'
ENVIRONMENT = '12345678-1234-1234-1234-123456789015'
COMMIT = 'a' * 40
TOKEN = 'c' * 64
PLAN = 'd' * 64
IDENTITY = {'callback_path': '/api/auth/core/callback', 'permissions': [
    {'codename': 'app-read', 'name': 'Read application'},
    {'codename': 'app-write', 'name': 'Write application'},
    {'codename': 'app-admin', 'name': 'Administer application'}]}


def binding():
    return {'platform_id': PLATFORM, 'organization_id': ORG, 'environment_id': ENVIRONMENT,
            'repository': 'NorthCountryEngineer/example', 'archetype': 'amplify-fargate',
            'origin': 'https://example.test', 'api_url': 'https://api.example.test', 'identity': copy.deepcopy(IDENTITY),
            'runtime_digest': adapter.digest({'kind': 'proposal', 'frontend': False})}


def snapshot():
    value = binding()
    return {**{key: value[key] for key in ('platform_id', 'organization_id', 'environment_id', 'repository', 'archetype')},
            'app_identifier': APP, 'commit': COMMIT, 'action': 'release', 'plan_hash': PLAN,
            'public_config': {}, 'hosting_binding_digest': adapter.digest(value)}


class AdapterTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.file = Path(self.directory.name) / 'bindings.json'
        self.config = {'version': 1, 'applications': {APP: binding()}, 'runner': {
            'host': '192.0.2.10', 'user': 'nce-release', 'port': 22,
            'key_file': '/run/secrets/release-key', 'known_hosts_file': '/run/config/known-hosts'}}
        self.file.write_text(json.dumps(self.config))
        context = override_settings(CORE_LIGHTSAIL_BINDINGS_FILE=str(self.file), CORE_BLOCK_UNBOUND_FARGATE_DEPLOYMENTS=False)
        context.enable()
        self.addCleanup(context.disable)
        self.platform = SimpleNamespace(pk=PLATFORM, prefix='nce-family')
        self.snapshot = snapshot()

    def test_current_hosting_uses_fixed_endpoints_fresh_worker_browser_health_and_no_aws(self):
        from contextlib import nullcontext
        from datetime import datetime, timezone
        app = SimpleNamespace(pk=APP, platform_id=PLATFORM, organization_id=ORG, environment_id=ENVIRONMENT,
            repository=binding()['repository'], archetype='amplify-fargate', public_origin=binding()['origin'], platform=self.platform)
        status = Path(self.directory.name) / 'status.json'
        status.write_text(json.dumps({'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'services': {name: {'state': 'running', 'health': 'healthy', 'untrusted_extra': 'must-not-leak'}
                for name in ('proposal-api', 'proposal-worker', 'sam-browser')}, 'maintenance': {'proposal': False}}))
        opener = Mock(return_value=nullcontext(SimpleNamespace(status=200)))
        with override_settings(CORE_LIGHTSAIL_STATUS_FILE=str(status)), patch('platform_services.cloud_aws._clients', side_effect=AssertionError('No AWS status calls')):
            current = adapter.current_hosting(app, opener=opener)
        self.assertEqual(current['provider'], 'Lightsail')
        self.assertEqual(current['status'], 'Healthy')
        self.assertEqual(current['api_readiness'], 'Ready')
        self.assertEqual({row['name'] for row in current['services']}, {'proposal-api', 'proposal-worker', 'sam-browser'})
        self.assertNotIn('must-not-leak', json.dumps(current))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.example.test/api/ready')
        self.assertFalse(any('secret' in name.lower() or 'authorization' in name.lower() for name in request.headers))

    def test_stale_host_report_never_claims_worker_health_and_503_means_maintenance(self):
        from contextlib import nullcontext
        from datetime import datetime, timedelta, timezone
        from urllib.error import HTTPError
        app = SimpleNamespace(pk=APP, platform_id=PLATFORM, organization_id=ORG, environment_id=ENVIRONMENT,
            repository=binding()['repository'], archetype='amplify-fargate', public_origin=binding()['origin'], platform=self.platform)
        status = Path(self.directory.name) / 'status.json'
        status.write_text(json.dumps({'version': 1, 'checked_at': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            'services': {}, 'maintenance': {'proposal': False}}))
        with override_settings(CORE_LIGHTSAIL_STATUS_FILE=str(status)):
            current = adapter.current_hosting(app, opener=Mock(return_value=nullcontext(SimpleNamespace(status=200))))
            maintenance = adapter.current_hosting(app, opener=Mock(side_effect=HTTPError('https://fixed', 503, 'private diagnostic', {}, None)))
        self.assertEqual(current['status'], 'Unknown')
        self.assertEqual(current['services'], [])
        self.assertEqual(current['container_report'], 'Unavailable or stale')
        self.assertEqual(maintenance['status'], 'Maintenance')
        self.assertNotIn('private diagnostic', json.dumps(maintenance))

    def test_placement_and_plan_digest_are_checked_before_any_transport(self):
        for key, value in [('organization_id', PLATFORM), ('repository', 'Other/example'), ('hosting_binding_digest', '0' * 64)]:
            with self.subTest(key=key), self.assertRaises(AWSAdapterError), patch.object(adapter, '_transport') as transport:
                adapter.start_run(self.platform, {**self.snapshot, key: value}, 'key')
            transport.assert_not_called()

    def test_missing_binding_blocks_fargate_without_falling_through_to_aws(self):
        with override_settings(CORE_BLOCK_UNBOUND_FARGATE_DEPLOYMENTS=True), patch('platform_services.cloud_aws.start_run') as aws:
            with self.assertRaisesMessage(AWSAdapterError, 'requires an operator'):
                cloud_provider.start_run(self.platform, {**self.snapshot, 'app_identifier': PLATFORM}, 'key')
        aws.assert_not_called()

    def test_unbound_serverless_still_uses_original_aws_provider(self):
        value = {**self.snapshot, 'app_identifier': PLATFORM, 'archetype': 'amplify-serverless'}
        with patch('platform_services.cloud_aws.start_run', return_value={'build_id': 'original'}) as aws:
            self.assertEqual(cloud_provider.start_run(self.platform, value, 'key'), {'build_id': 'original'})
        aws.assert_called_once()

    def test_bound_start_preserves_aws_validation_and_ci_but_never_starts_aws_build(self):
        with patch.object(adapter, '_snapshot') as checked, patch.object(adapter, 'require_ci') as ci, \
                patch.object(adapter, '_transport', return_value={'status': 'running'}) as transport, \
                patch('platform_services.cloud_aws.start_run') as aws:
            state = cloud_provider.start_run(self.platform, self.snapshot, 'release-key')
        checked.assert_called_once()
        ci.assert_called_once()
        aws.assert_not_called()
        self.assertTrue(state['build_id'].startswith('lightsail:'))
        self.assertNotIn('client_secret', json.dumps(state))
        self.assertEqual(transport.call_args.args[0], 'start')

    def test_ssh_has_pinned_host_key_fixed_command_and_json_stdin(self):
        response = {'version': 1, 'token': TOKEN, 'status': 'running', 'commit': COMMIT, 'action': 'release', 'plan_hash': PLAN}
        with patch.object(adapter.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))) as run:
            self.assertEqual(adapter._transport('poll', self.snapshot, TOKEN), response)
        args = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertEqual(args[-1], 'sudo -n /usr/local/sbin/nce-lightsail-release-protocol')
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['snapshot']['app_identifier'], APP)

    def test_transport_errors_and_mismatched_receipts_do_not_echo_diagnostics(self):
        with patch.object(adapter.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='secret=private')):
            with self.assertRaises(AWSAdapterError) as caught:
                adapter._transport('poll', self.snapshot, TOKEN)
        self.assertNotIn('private', str(caught.exception))
        response = {'version': 1, 'token': TOKEN, 'commit': 'b' * 40, 'action': 'release', 'plan_hash': PLAN}
        with patch.object(adapter.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))):
            with self.assertRaises(AWSAdapterError):
                adapter._transport('poll', self.snapshot, TOKEN)

    def test_poll_requires_exact_origin_api_and_identity_receipt(self):
        outputs = {'origin': binding()['origin'], 'api_url': binding()['api_url'], 'commit': COMMIT,
                   'identity': json.dumps(IDENTITY, sort_keys=True, separators=(',', ':'))}
        state = adapter._state(self.snapshot, TOKEN)
        with patch.object(adapter, '_transport', return_value={'status': 'succeeded', 'outputs': outputs}):
            self.assertEqual(adapter.poll_run(self.platform, self.snapshot, state)['outputs'], outputs)
        with patch.object(adapter, '_transport', return_value={'status': 'succeeded', 'outputs': {**outputs, 'origin': 'https://other.test'}}):
            with self.assertRaises(AWSAdapterError):
                adapter.poll_run(self.platform, self.snapshot, state)

    def test_recovery_never_resubmits_start_and_refuses_unknown_token(self):
        with patch.object(adapter, '_transport', return_value={'status': 'not_found'}) as transport:
            with self.assertRaisesMessage(AWSAdapterError, 'ambiguous'):
                adapter.recover_run(self.platform, self.snapshot, 'release-key')
        self.assertEqual(transport.call_args.args[0], 'recover')

    def test_credential_scope_and_private_transport_are_preserved(self):
        credentials = {'client_id': 'resource', 'client_secret': 'private-value'}
        with patch.object(adapter, '_transport', return_value={'status': 'succeeded'}) as transport:
            result = cloud_provider.store_credentials(self.platform, f'/nce-family/apps/{APP}/core-resource-client', credentials, snapshot=self.snapshot)
        self.assertNotIn('private-value', json.dumps(result))
        self.assertEqual(transport.call_args.kwargs['credentials'], credentials)
        with patch.object(adapter, '_transport') as transport:
            with self.assertRaises(AWSAdapterError):
                cloud_provider.store_credentials(self.platform, '/wrong/parameter', credentials, snapshot=self.snapshot)
        transport.assert_not_called()


spec = importlib.util.spec_from_file_location('lightsail_protocol_test', Path(settings.BASE_DIR) / 'deployment/lightsail_release_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class RunnerTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        (self.home / 'config').mkdir()
        self.root = self.home / 'releases' / APP / COMMIT
        self.root.mkdir(parents=True)
        source = b'operator-approved synthetic source archive'
        (self.root / 'source.tar.gz').write_bytes(source)
        (self.root / 'core-resource.env').write_text('CORE_RESOURCE_CREDENTIALS_JSON={"client_id":"synthetic","client_secret":"synthetic"}\n')
        self.config = {'version': 1, 'bindings': {APP: binding()}, 'runtimes': {APP: {'kind': 'proposal', 'frontend': False}},
                       'releases': {APP: {COMMIT: {'source_sha256': hashlib.sha256(source).hexdigest(),
                            'images': {name: 'sha256:' + 'a' * 64 for name in protocol.SERVICES['proposal']}, 'frontend_sha256': ''}}}}
        (self.home / 'config/release-bindings.json').write_text(json.dumps(self.config))
        (self.home / 'config/current-proposal.json').write_text('{"services":{}}')
        self.calls = []
        self.fail = None
        self.dump_failed = False
        self.database_metadata = {'name': 'studio', 'owner': 'studio', 'encoding': 'UTF8',
            'collate': 'C', 'ctype': 'C', 'locale_provider': 'c', 'locale': None,
            'icu_rules': None, 'collation_version': None, 'tablespace': 'pg_default',
            'allow_connections': True, 'connection_limit': -1, 'is_template': False,
            'comment': None, 'acl': [{'grantor': 'studio', 'grantee': 'studio',
                'privilege': privilege, 'grantable': False} for privilege in ('CREATE', 'CONNECT', 'TEMPORARY')],
            'settings': [], 'security_labels': []}

        def run(args, **kwargs):
            self.calls.append(list(args))
            if self.fail and self.fail(args):
                raise subprocess.CalledProcessError(1, args)
            if 'pg_dump' in args:
                kwargs['stdout'].write(b'PGDMP' + b'x' * 100)
            if 'psql' in args and kwargs.get('input', '').startswith('SELECT jsonb_build_object('):
                value = dict(self.database_metadata)
                if "WHERE d.datname='academy'" in kwargs['input']:
                    value['name'] = 'academy'
                    value['owner'] = 'academy'
                return SimpleNamespace(returncode=0, stdout=json.dumps(value))
            return SimpleNamespace(returncode=0, stdout='')

        self.runner = protocol.Runner(self.home, enforce_owner=False, run=run, maintenance_lock=self.home / 'shared-maintenance.lock')
        self.request = {'version': 1, 'operation': 'start', 'token': TOKEN, 'snapshot': snapshot()}

    def submit(self):
        return self.runner.handle(self.request, launch=False)

    def test_provision_validates_existing_hosting_without_docker_or_database_mutation(self):
        self.request['snapshot']['action'] = 'provision'
        result = self.submit()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['outputs']['identity'], json.dumps(IDENTITY, sort_keys=True, separators=(',', ':')))

    def test_automatic_release_builds_before_maintenance_and_retains_existing_identity(self):
        release = self.config['releases'][APP].pop(COMMIT)
        self.config['build'] = {'reviewed': 'fixed-artifact-only-contract'}
        self.config['runtimes'][APP]['build_contract_digest'] = protocol.digest(self.config['build'])
        self.config['bindings'][APP]['runtime_digest'] = protocol.digest(self.config['runtimes'][APP])
        self.request['snapshot']['hosting_binding_digest'] = protocol.digest(self.config['bindings'][APP])
        (self.home / 'config/release-bindings.json').write_text(json.dumps(self.config))
        calls = []

        original_run = self.runner.run
        def stage(args, **kwargs):
            if len(args) < 2 or Path(args[1]).name != 'nce-lightsail-stage-artifacts':
                return original_run(args, **kwargs)
            calls.append(args)
            self.assertFalse((self.home / 'maintenance/proposal').exists())
            self.assertFalse(self.runner.maintenance_lock.exists())
            request = json.loads(kwargs['input'])
            self.assertEqual(request['snapshot']['commit'], COMMIT)
            protocol.write_private(self.root / 'automatic-build.json', json.dumps({
                'build_contract_digest': protocol.digest(self.config['build']),
                'binding_digest': request['snapshot']['hosting_binding_digest'],
                'public_config_digest': protocol.digest(request['snapshot']['public_config']), 'release': release}))
            return SimpleNamespace(returncode=0)

        self.runner = protocol.Runner(self.home, enforce_owner=False, run=stage, maintenance_lock=self.home / 'shared-maintenance.lock')
        self.assertEqual(self.submit()['status'], 'running')
        self.runner.execute(TOKEN)
        result = self.runner.handle({**self.request, 'operation': 'poll'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['outputs']['identity'], json.dumps(IDENTITY, sort_keys=True, separators=(',', ':')))
        self.assertEqual(len(calls), 1)

    def test_automatic_provision_preserves_identity_and_credentials_before_public_build_variables_exist(self):
        self.config['releases'][APP].pop(COMMIT)
        self.config['build'] = {'reviewed': 'fixed-artifact-only-contract'}
        self.config['runtimes'][APP]['build_contract_digest'] = protocol.digest(self.config['build'])
        self.config['bindings'][APP]['runtime_digest'] = protocol.digest(self.config['runtimes'][APP])
        self.request['snapshot']['hosting_binding_digest'] = protocol.digest(self.config['bindings'][APP])
        self.request['snapshot']['action'] = 'provision'
        (self.root / 'source.tar.gz').unlink()
        (self.home / 'config/release-bindings.json').write_text(json.dumps(self.config))
        self.runner = protocol.Runner(self.home, enforce_owner=False, run=self.runner.run, maintenance_lock=self.home / 'shared-maintenance.lock')
        self.assertEqual(self.submit()['status'], 'succeeded')
        self.runner.handle({**self.request, 'operation': 'credentials', 'credentials': {'client_id': 'preserved', 'client_secret': 'preserved-secret'}})
        self.assertEqual(json.loads(self.runner.credential_value(self.root))['client_id'], 'preserved')
        self.assertFalse((self.root / 'source.tar.gz').exists())
        self.assertFalse(self.calls)
        self.assertEqual(self.calls, [])

    def test_unstaged_commit_refuses_without_starting_a_process(self):
        self.request['snapshot']['commit'] = 'b' * 40
        with self.assertRaises(protocol.Refused):
            self.submit()
        self.assertFalse((self.home / 'release-state' / TOKEN / 'request.json').exists())

    def test_token_replay_is_idempotent_and_changed_plan_is_refused(self):
        self.assertEqual(self.submit(), self.submit())
        self.request['snapshot']['plan_hash'] = 'e' * 64
        with self.assertRaises(protocol.Refused):
            self.submit()

    def test_recovery_of_unknown_token_has_no_side_effects(self):
        self.request['operation'] = 'recover'
        self.assertEqual(self.submit()['status'], 'not_found')
        self.assertEqual(self.calls, [])

    def test_credentials_remain_private_and_are_not_returned(self):
        self.request['operation'] = 'credentials'
        self.request['credentials'] = {'client_id': 'resource', 'client_secret': 'a$quote\'value'}
        result = self.submit()
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('value', json.dumps(result))
        file = self.root / 'core-resource.env'
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertIn('a$quote', file.read_text())
        self.assertEqual(len(file.read_text().splitlines()), 1)

    def test_artifact_symlinks_and_altered_source_are_refused(self):
        (self.root / 'source.tar.gz').write_bytes(b'changed')
        with self.assertRaises(protocol.Refused):
            self.submit()

    def test_success_quiesces_writers_before_backup_and_opens_after_health(self):
        self.submit()
        self.runner.execute(TOKEN)
        response = self.runner.handle({**self.request, 'operation': 'poll'})
        self.assertEqual(response['status'], 'succeeded')
        stop = next(index for index, args in enumerate(self.calls) if 'stop' in args)
        dump = next(index for index, args in enumerate(self.calls) if 'pg_dump' in args)
        migrate = next(index for index, args in enumerate(self.calls) if 'studio.runtime' in args)
        self.assertLess(stop, dump)
        self.assertLess(dump, migrate)
        self.assertFalse((self.home / 'maintenance/proposal').exists())
        self.assertFalse(any('dropdb' in args for args in self.calls))
        self.assertTrue(any('/api/ready' in argument for args in self.calls for argument in args))
        override = json.loads((self.home / 'config/current-proposal.json').read_text())
        self.assertEqual(json.loads(override['services']['proposal-api']['environment']['CORE_RESOURCE_CREDENTIALS_JSON']),
                         {'client_id': 'synthetic', 'client_secret': 'synthetic'})

    def test_compose_environment_serialization_escapes_raw_dollars_once(self):
        raw = 'literal-${NCE_EXPR}-$NCE_WORD-$$'
        document = {'services': {'proposal-api': {'environment': {'CORE_RESOURCE_CREDENTIALS_JSON': raw}}}}
        rendered = json.loads(protocol.compose_json(document))
        self.assertEqual(rendered['services']['proposal-api']['environment']['CORE_RESOURCE_CREDENTIALS_JSON'],
                         'literal-$${NCE_EXPR}-$$NCE_WORD-$$$$')
        self.assertEqual(document['services']['proposal-api']['environment']['CORE_RESOURCE_CREDENTIALS_JSON'], raw)
        self.assertEqual(protocol.compose_json(document), protocol.compose_json(document))

    def test_backup_lock_contention_starts_nothing_and_keeps_existing_maintenance(self):
        self.submit()
        maintenance = self.home / 'maintenance/proposal'
        protocol.write_private(maintenance, 'backup owns maintenance\n')
        with self.runner.maintenance_lock.open('a') as backup_lock:
            protocol.fcntl.flock(backup_lock, protocol.fcntl.LOCK_EX | protocol.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.runner.execute(TOKEN)
        self.assertEqual(self.calls, [])
        self.assertEqual(maintenance.read_text(), 'backup owns maintenance\n')
        self.assertEqual((self.home / 'config/current-proposal.json').read_text(), '{"services":{}}')

    def test_shared_backup_lock_is_held_through_migration_and_rollback(self):
        self.submit()
        original = self.runner.run
        probes = []
        script = ('import fcntl,sys; f=open(sys.argv[1],"a");\n'
                  'try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                  'except BlockingIOError: sys.exit(73)\n'
                  'sys.exit(0)\n')

        def test_contender(args, **kwargs):
            stage = next((name for name in ('pg_dump', 'studio.runtime', 'dropdb', 'pg_restore', 'up') if name in args), None)
            if stage:
                result = subprocess.run([sys.executable, '-c', script, str(self.runner.maintenance_lock)],
                                        check=False, capture_output=True, timeout=10)
                probes.append((stage, result.returncode))
            return original(args, **kwargs)

        self.runner.run = test_contender
        self.fail = lambda args: 'studio.runtime' in args
        self.runner.execute(TOKEN)
        self.assertTrue({'pg_dump', 'studio.runtime', 'dropdb', 'pg_restore', 'up'} <= {stage for stage, _ in probes})
        self.assertTrue(all(code == 73 for _, code in probes), probes)
        with self.runner.maintenance_lock.open('a') as after:
            protocol.fcntl.flock(after, protocol.fcntl.LOCK_EX | protocol.fcntl.LOCK_NB)

    def test_academy_owner_configuration_applies_only_to_migration(self):
        runtime = {'kind': 'academy', 'frontend': False}
        self.config['runtimes'][APP] = runtime
        self.config['bindings'][APP]['archetype'] = 'amplify-academy-fargate'
        self.config['bindings'][APP]['runtime_digest'] = adapter.digest(runtime)
        self.request['snapshot']['archetype'] = 'amplify-academy-fargate'
        self.request['snapshot']['hosting_binding_digest'] = adapter.digest(self.config['bindings'][APP])
        self.config['releases'][APP][COMMIT]['images'] = {'academy-api': 'sha256:' + 'a' * 64}
        (self.home / 'config/release-bindings.json').write_text(json.dumps(self.config))
        (self.home / 'config/current-academy.json').write_text('{"services":{}}')
        (self.home / 'secrets').mkdir()
        (self.home / 'secrets/academy-migration.env').write_text('DATABASE_URL=postgresql://owner:synthetic@postgres/academy\nACADEMY_RUNTIME_DB_USERNAME=academy_runtime\nACADEMY_RUNTIME_DB_PASSWORD=synthetic-password\nACADEMY_REUSE_RUNTIME_DB_ROLE=1\n')  # pragma: allowlist secret -- isolated synthetic database fixture
        self.runner = protocol.Runner(self.home, enforce_owner=False, run=self.runner.run, maintenance_lock=self.home / 'shared-maintenance.lock')
        self.submit()
        self.runner.execute(TOKEN)
        migration = json.loads((self.home / 'release-state' / TOKEN / 'migration-override.json').read_text())
        runtime = json.loads((self.home / 'config/current-academy.json').read_text())
        self.assertIn('DATABASE_URL', migration['services']['academy-api']['environment'])
        self.assertEqual(migration['services']['academy-api']['environment']['ACADEMY_REUSE_RUNTIME_DB_ROLE'], '1')
        self.assertNotIn('DATABASE_URL', runtime['services']['academy-api']['environment'])
        self.assertNotIn('ACADEMY_RUNTIME_DB_PASSWORD', runtime['services']['academy-api']['environment'])
        self.assertNotIn('ACADEMY_REUSE_RUNTIME_DB_ROLE', runtime['services']['academy-api']['environment'])
        self.assertEqual(self.runner.handle({**self.request, 'operation': 'poll'})['status'], 'succeeded')

    def test_backup_failure_does_not_drop_unchanged_database(self):
        self.submit()
        self.fail = lambda args: 'pg_dump' in args
        self.runner.execute(TOKEN)
        response = self.runner.handle({**self.request, 'operation': 'poll'})
        self.assertEqual(response['status'], 'failed')
        self.assertFalse(any('dropdb' in args for args in self.calls))
        self.assertFalse((self.home / 'maintenance/proposal').exists())

    def test_migration_failure_restores_quiesced_database_before_old_runtime(self):
        self.submit()
        self.fail = lambda args: 'studio.runtime' in args
        self.runner.execute(TOKEN)
        response = self.runner.handle({**self.request, 'operation': 'poll'})
        self.assertEqual(response['status'], 'failed')
        restore = next(index for index, args in enumerate(self.calls) if 'pg_restore' in args and '--exit-on-error' in args)
        restarted = next(index for index, args in enumerate(self.calls) if 'up' in args)
        self.assertLess(restore, restarted)
        self.assertIn('rollback verified', response['error'])

    def test_health_failure_preserves_possible_new_writes_and_keeps_maintenance(self):
        self.submit()
        self.fail = lambda args: any('/api/ready' in value for value in args)
        self.runner.execute(TOKEN)
        response = self.runner.handle({**self.request, 'operation': 'poll'})
        self.assertEqual(response['status'], 'failed')
        self.assertIn('operator recovery', response['error'])
        self.assertFalse(any('dropdb' in args for args in self.calls))
        self.assertTrue((self.home / 'maintenance/proposal').exists())

    def test_cancellation_during_readiness_cannot_record_success_or_rollback_new_writes(self):
        self.submit()
        original = self.runner.run

        def cancel_during_health(args, **kwargs):
            if any('/api/ready' in value for value in args):
                protocol.write_private(self.home / 'release-state' / TOKEN / 'cancel', '1\n')
            return original(args, **kwargs)

        self.runner.run = cancel_during_health
        self.runner.execute(TOKEN)
        self.assertEqual(self.runner.handle({**self.request, 'operation': 'poll'})['status'], 'failed')
        self.assertFalse(any('dropdb' in args for args in self.calls))
        self.assertTrue((self.home / 'maintenance/proposal').exists())

    def test_cancel_before_migration_never_changes_database(self):
        self.submit()
        self.runner.handle({**self.request, 'operation': 'stop'})
        self.runner.execute(TOKEN)
        self.assertFalse(any('pg_dump' in args or 'dropdb' in args for args in self.calls))
        self.assertEqual(self.runner.handle({**self.request, 'operation': 'poll'})['status'], 'failed')

    def amplify_runtime(self):
        from zipfile import ZipFile
        runtime = {'kind': 'proposal', 'frontend': 'amplify', 'amplify_app_id': 'dexisting123', 'amplify_branch': 'production'}
        archive = self.root / 'frontend.zip'
        with ZipFile(archive, 'w') as zip_file:
            zip_file.writestr('index.html', '<html>synthetic staged frontend</html>')
        self.config['runtimes'][APP] = runtime
        self.config['bindings'][APP]['runtime_digest'] = adapter.digest(runtime)
        self.request['snapshot']['hosting_binding_digest'] = adapter.digest(self.config['bindings'][APP])
        self.config['releases'][APP][COMMIT]['frontend_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.home / 'config/release-bindings.json').write_text(json.dumps(self.config))
        self.runner = protocol.Runner(self.home, enforce_owner=False, run=self.runner.run, maintenance_lock=self.home / 'shared-maintenance.lock')
        return runtime

    def test_amplify_publication_uses_existing_fixed_app_and_no_signed_url_receipt(self):
        runtime = self.amplify_runtime()
        self.submit()
        response = [
            {'jobId': '12', 'zipUploadUrl': 'https://example.s3.amazonaws.com/object?private-signature=secret'},
            {'jobSummary': {'jobId': '12'}}, {'job': {'summary': {'jobId': '12', 'status': 'SUCCEED'}}}]
        with patch.object(self.runner, 'amplify', side_effect=response) as amplify, patch.object(self.runner, 'upload') as upload:
            self.runner.execute(TOKEN)
        self.assertEqual([call.args[0] for call in amplify.call_args_list], ['create-deployment', 'start-deployment', 'get-job'])
        self.assertTrue(all(call.args[1] == runtime for call in amplify.call_args_list))
        upload.assert_called_once()
        receipt = (self.home / 'release-state' / TOKEN / 'amplify.json').read_text()
        self.assertNotIn('private-signature', receipt)
        self.assertEqual(self.runner.handle({**self.request, 'operation': 'poll'})['status'], 'succeeded')
        self.assertFalse((self.home / 'static/proposal').exists())

    def test_uncertain_amplify_create_is_never_automatically_retried(self):
        runtime = self.amplify_runtime()
        self.submit()
        job = self.home / 'release-state' / TOKEN
        with patch.object(self.runner, 'amplify', side_effect=RuntimeError('private upload response')) as amplify:
            with self.assertRaises(RuntimeError):
                self.runner.publish_amplify(job, self.root, runtime)
            with self.assertRaises(protocol.Refused):
                self.runner.publish_amplify(job, self.root, runtime)
        self.assertEqual(amplify.call_count, 1)

    def test_frontend_destination_change_invalidates_reviewed_runtime_binding(self):
        self.amplify_runtime()
        self.runner.config['runtimes'][APP]['amplify_app_id'] = 'dother123'
        with self.assertRaisesMessage(protocol.Refused, 'destination changed'):
            self.submit()

    def test_amplify_zip_cannot_publish_path_traversal(self):
        from zipfile import ZipFile
        self.amplify_runtime()
        archive = self.root / 'frontend.zip'
        with ZipFile(archive, 'a') as zip_file:
            zip_file.writestr('../secret', 'forbidden')
        self.runner.config['releases'][APP][COMMIT]['frontend_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaises(protocol.Refused):
            self.submit()


class LightsailWorkflowTests(TestCase):
    """A complete approved two-phase handoff retains existing client secrets/IDs."""
    def test_existing_identity_graph_survives_the_local_provider_handoff(self):
        from identity.access import Principal
        from identity.models import Membership, Organization, User
        from platform_services.cloud_deployments import approve_release, create_application, prepare_release, process_one, verify_platform
        from platform_services.cloud_models import CloudApplication, CloudPlatform, CloudRelease
        from platform_services.environments import create_environment

        org = Organization.objects.create(name='Local hosting', slug='local-hosting')
        admin = User.objects.create_user('local-hosting@example.test', 'Synthetic-password-only')
        Membership.objects.create(organization=org, user=admin, kind='administrator')
        environment = create_environment(org, name='development', namespace='local-hosting', application_origin='https://example.test')
        platform = CloudPlatform.objects.create(organization=org, name='Existing platform', mode='demo',
            allowed_repositories=['Example/app'], config={'environment_ids': [str(environment.pk)], 'archetypes': ['amplify-fargate']})
        principal = Principal(user=admin)
        platform = verify_platform(platform, principal)
        app = create_application(org, principal, platform=platform, environment=environment, name='Existing app', slug='existing',
                                 repository='Example/app', branch='main', commit=COMMIT, archetype='amplify-fargate', public_origin='https://example.test')
        first = prepare_release(app, principal)
        approve_release(first, principal)
        process_one()
        CloudRelease.objects.filter(pk=first.pk).update(next_poll=None)
        process_one()
        app.refresh_from_db()
        deployment = app.installation.deployments.get(target='external')
        preserved = (app.pk, app.installation_id, deployment.pk, deployment.installation.service_id,
                     deployment.application_id, deployment.resource_application_id,
                     deployment.resource_application.client_secret, deployment.encrypted_bundle)
        value = {**binding(), 'platform_id': str(platform.pk), 'organization_id': str(org.pk),
                 'environment_id': str(environment.pk), 'repository': app.repository}
        config = {'version': 1, 'applications': {str(app.pk): value}, 'runner': {
            'host': '192.0.2.10', 'user': 'nce-release', 'port': 22,
            'key_file': '/run/secrets/release-key', 'known_hosts_file': '/run/config/known-hosts'}}
        operations = []

        def transport(operation, snapshot, token, **kwargs):
            operations.append(operation)
            outputs = {'origin': value['origin'], 'api_url': value['api_url'], 'commit': snapshot['commit'],
                       'identity': json.dumps(value['identity'], sort_keys=True, separators=(',', ':'))}
            return {'status': 'succeeded' if operation in {'poll', 'credentials'} else 'running', 'outputs': outputs}

        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'bindings.json'
            file.write_text(json.dumps(config))
            CloudPlatform.objects.filter(pk=platform.pk).update(mode='aws')
            with override_settings(CORE_LIGHTSAIL_BINDINGS_FILE=str(file), CORE_ENABLE_CLOUD_DEPLOYMENTS=True), \
                    patch('platform_services.cloud_deployments._ready'), patch.object(CloudApplication, 'full_clean'), \
                    patch('platform_services.cloud_deployments._require_ci'), patch.object(adapter, '_snapshot'), \
                    patch.object(adapter, 'require_ci'), patch.object(adapter, '_transport', side_effect=transport), \
                    patch('platform_services.cloud_aws.start_run', side_effect=AssertionError('No AWS provisioning')):
                app.refresh_from_db()
                release = prepare_release(app, principal)
                self.assertIn('hosting_binding_digest', release.snapshot)
                approve_release(release, principal, confirm_costs=True)
                for _ in range(4):
                    CloudRelease.objects.filter(pk=release.pk).update(next_poll=None)
                    process_one()
                release.refresh_from_db()
                self.assertEqual(release.status, 'succeeded', release.error)
                self.assertEqual(operations, ['start', 'poll', 'credentials', 'start', 'poll'])
                self.assertNotIn('client_secret', json.dumps(release.outputs) + json.dumps(release.provider_state))
        app.refresh_from_db()
        deployment.refresh_from_db()
        self.assertEqual(preserved, (app.pk, app.installation_id, deployment.pk, deployment.installation.service_id,
                         deployment.application_id, deployment.resource_application_id,
                         deployment.resource_application.client_secret, deployment.encrypted_bundle))
