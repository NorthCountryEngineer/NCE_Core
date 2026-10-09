import copy
from contextlib import contextmanager
from datetime import timedelta
import json
import os
import secrets
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from ci import pipeline as p

SHA = 'b' * 40
DIGEST = 'sha256:' + 'a' * 64


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.artifacts = Path(self.directory.name) / 'artifacts'
        self.artifacts.mkdir()
        self.config = p.load_config(p.ROOT / 'ci/environments/example.json')
        self.image = self.config['image_repository'] + '@' + DIGEST

    def load(self, config):
        path = Path(self.directory.name) / 'config.json'
        path.write_text(json.dumps(config))
        return p.load_config(path)

    def release(self):
        (self.artifacts / 'release.json').write_text(json.dumps({'schema_version': 1, 'commit': SHA,
            'image': self.image, 'config_hash': p.config_hash(self.config)}))

    def test_unknown_secret_values_never_echoed_by_validation(self):
        password = secrets.token_urlsafe(32)
        self.config['password'] = password
        with self.assertRaises(p.PipelineError) as error:
            self.load(self.config)
        self.assertNotIn(password, str(error.exception))

    def test_bad_issuer_namespace_privileged_settings_and_provider_paths_rejected(self):
        for field, value in [('issuer', 'http://core.example'), ('namespace', '../other'), ('context', ''),
                             ('extra_environment', {'CORE_DEBUG': '1'}), ('web_replicas', 0)]:
            config = copy.deepcopy(self.config)
            config['kubernetes'][field] = value
            with self.subTest(field=field), self.assertRaises(p.PipelineError):
                self.load(config)
        self.config['kubernetes']['provider_secrets'] = [{'secret_name': 'idp', 'organization_id': 'a' * 36, 'key': 'secret', 'filename': '../../escape'}]
        with self.assertRaises(p.PipelineError):
            self.load(self.config)

    def test_digest_required_and_fixed_to_configured_repository(self):
        for image in [self.config['image_repository'] + ':latest', 'evil.example/app@' + DIGEST]:
            with self.assertRaises(p.PipelineError):
                p.manifests(self.config, image, SHA)

    def test_manifests_have_real_health_checks_bounded_jobs_and_secret_references(self):
        configmap, job, workloads = p.manifests(self.config, self.image, SHA)
        self.assertEqual(configmap['data']['CORE_DEBUG'], '0')
        self.assertEqual({item['kind'] for item in [configmap, job, *workloads]}, {'ConfigMap', 'Job', 'Deployment', 'Service', 'Ingress'})
        self.assertNotIn('Secret', {item['kind'] for item in workloads})
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 600)
        self.assertIn('check', job['spec']['template']['spec']['containers'][0]['command'][-1])
        for resource in [job, *workloads[:2]]:
            pod = resource['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            container = pod['containers'][0]
            self.assertEqual(container['image'], self.image)
            self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
            self.assertTrue(container['securityContext']['runAsNonRoot'])
            self.assertNotIn('value', container['env'][0])
        web = workloads[0]['spec']['template']['spec']['containers'][0]
        self.assertEqual(web['readinessProbe']['httpGet']['path'], '/health/')
        self.assertEqual(workloads[2]['spec']['type'], 'ClusterIP')

    def test_provider_mounts_are_tenant_scoped_subpaths_and_duplicates_rejected(self):
        organization = 'aabbccdd-0000-4000-8000-112233445566'
        secret = {'secret_name': 'idp', 'organization_id': organization, 'key': 'oidc', 'filename': 'customer.secret'}
        self.config['kubernetes']['provider_secrets'] = [secret]
        self.load(self.config)
        pod = p.manifests(self.config, self.image, SHA)[1]['spec']['template']['spec']
        mount = pod['containers'][0]['volumeMounts'][-1]
        self.assertEqual(mount['mountPath'], '/run/secrets/identity-providers/' + organization + '/customer.secret')
        self.assertEqual(mount['subPath'], 'oidc')
        self.assertTrue(mount['readOnly'])
        self.config['kubernetes']['provider_secrets'].append(secret)
        with self.assertRaises(p.PipelineError):
            self.load(self.config)

    def test_external_mutations_default_to_disabled(self):
        with patch.dict(os.environ, {'CORE_CI_ALLOW_PUBLISH': '0', 'CORE_CI_ALLOW_DEPLOY': '0'}), patch.object(p, 'command') as command:
            with self.assertRaises(p.PipelineError):
                p.publish(self.config)
            with self.assertRaises(p.PipelineError):
                p.deploy(self.config)
            command.assert_not_called()

    def test_chat_federation_secrets_use_tenant_scoped_subpaths_and_named_rbac(self):
        organization = '11223344-5566-4788-9900-aabbccddeeff'
        reference_name = 'chat-peer'
        self.config['kubernetes']['chat_federation_secrets'] = [{'secret_name': reference_name,
            'organization_id': organization, 'key': 'credential', 'filename': 'peer.credential'}]
        self.load(self.config)
        _, job, _ = p.manifests(self.config, self.image, SHA)
        mount = job['spec']['template']['spec']['containers'][0]['volumeMounts'][-1]
        self.assertEqual(mount['mountPath'], '/run/secrets/chat-federation/' + organization + '/peer.credential')
        self.assertEqual(mount['subPath'], 'credential')
        self.assertTrue(mount['readOnly'])
        rules = p.rbac(self.config)['items'][1]['rules']
        self.assertIn(reference_name, next(rule['resourceNames'] for rule in rules if rule['resources'] == ['secrets']))
        self.config['kubernetes']['chat_federation_secrets'][0]['filename'] = '../escape'
        with self.assertRaises(p.PipelineError):
            self.load(self.config)

    def test_ci_environment_does_not_reuse_production_keys_or_issuer(self):
        with patch.dict(os.environ, {'CORE_SECRET_KEY_FILE': '/production/key', 'CORE_OIDC_KEY_FILE': '/production/oidc',
                                    'CORE_ISSUER': 'https://production.example', 'CORE_OIDC_PREVIOUS_KEY_FILES': '/production/old',
                                    'REGISTRY_PASSWORD': secrets.token_urlsafe(32), 'KUBECONFIG': '/production/kubeconfig'}):
            environment = p.test_environment()
        self.assertEqual(environment['CORE_ISSUER'], 'http://localhost:8000')
        self.assertNotIn('/production', environment['CORE_SECRET_KEY_FILE'])
        self.assertNotIn('CORE_OIDC_PREVIOUS_KEY_FILES', environment)
        self.assertNotIn('REGISTRY_PASSWORD', environment)
        self.assertNotIn('KUBECONFIG', environment)

    def test_ci_requires_postgres(self):
        with patch.dict(os.environ, {'DATABASE_URL': ''}), patch.object(p, 'command') as command:
            with self.assertRaises(p.PipelineError):
                p.test(self.config)
            command.assert_not_called()

    def test_dirty_or_non_tip_checkout_cannot_release(self):
        with patch.object(p, 'dirty', return_value=True), self.assertRaises(p.PipelineError):
            p.trusted_release(self.config)
        with patch.object(p, 'dirty', return_value=False), patch.object(p, 'revision', return_value=SHA), patch.object(p, 'command', return_value='c' * 40 + '\trefs/heads/main'):
            with self.assertRaises(p.PipelineError):
                p.trusted_release(self.config)

    def test_approval_environment_must_match_destination(self):
        with patch.dict(os.environ, {'CORE_CI_ENVIRONMENT': 'production'}), self.assertRaises(p.PipelineError):
            p.trusted_release(self.config)

    def test_https_smoke_checks_verify_tls_and_pin_issuer(self):
        responses = [Mock(status_code=200, json=Mock(return_value=value)) for value in [
            {'status': 'ok'}, {'issuer': self.config['kubernetes']['issuer'], 'jwks_uri': self.config['kubernetes']['issuer'] + '/oauth/jwks/'},
            {'keys': [{'kty': 'RSA', 'alg': 'RS256'}]}, {}]]
        responses.append(Mock(status_code=401))
        with patch('requests.get', side_effect=responses) as get, patch.dict(os.environ, {'CORE_CI_CA_BUNDLE': ''}):
            p.smoke(self.config)
            self.assertTrue(all(call.kwargs['verify'] is True for call in get.call_args_list))
            self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in get.call_args_list))
        health = Mock(status_code=200, json=Mock(return_value={'status': 'ok'}))
        wrong = Mock(status_code=200, json=Mock(return_value={'issuer': 'https://other.example'}))
        with patch('requests.get', side_effect=[health, wrong] * 5), patch.object(p.time, 'sleep'), self.assertRaises(p.PipelineError):
            p.smoke(self.config)

    def test_publish_rejects_stale_build_and_passes_password_only_on_stdin(self):
        password = secrets.token_urlsafe(32)
        build = {'schema_version': 1, 'commit': SHA, 'image_tag': self.config['image_repository'] + ':' + SHA,
                 'dirty': False, 'config_hash': p.config_hash(self.config)}
        (self.artifacts / 'build.json').write_text(json.dumps(build))
        with patch.object(p, 'ARTIFACTS', self.artifacts), patch.object(p, 'trusted_release'), patch.object(p, 'revision', return_value=SHA), \
             patch.dict(os.environ, {'CORE_CI_ALLOW_PUBLISH': '1', 'REGISTRY_USERNAME': 'test', 'REGISTRY_PASSWORD': password}), \
             patch.object(p, 'command', side_effect=['', '', json.dumps({'digest': DIGEST})]) as command:
            result = p.publish(self.config)
            self.assertEqual(result['image'], self.image)
            login = command.call_args_list[0]
            self.assertNotIn(password, login.args[0])
            self.assertEqual(login.kwargs['data'], password + '\n')
            build['commit'] = 'c' * 40
            (self.artifacts / 'build.json').write_text(json.dumps(build))
            with self.assertRaises(p.PipelineError):
                p.publish(self.config)

    @contextmanager
    def lock(self, config):
        yield lambda: None

    def deployment(self, fail_migration=False):
        events = []
        def kubectl(config, args, **kwargs):
            events.append((args, kwargs.get('data')))
            if fail_migration and args[0] == 'wait':
                raise p.PipelineError('Migration failed')
            return ''
        self.release()
        with patch.object(p, 'ARTIFACTS', self.artifacts), patch.object(p, 'revision', return_value=SHA), patch.object(p, 'trusted_release'), \
             patch.object(p, 'release_lock', self.lock), patch.object(p, 'kubectl', side_effect=kubectl), patch.object(p, 'smoke'), patch.dict(os.environ, {'CORE_CI_ALLOW_DEPLOY': '1'}):
            if fail_migration:
                with self.assertRaises(p.PipelineError):
                    p.deploy(self.config)
            else:
                p.deploy(self.config)
        return events

    def test_migration_failure_keeps_existing_workloads_unchanged(self):
        (self.artifacts / 'deployed.json').write_text('{"old": true}')
        events = self.deployment(fail_migration=True)
        applied = [data for args, data in events if args[:2] == ['apply', '-f']]
        self.assertEqual([data['kind'] for data in applied], ['ConfigMap', 'Job'])
        self.assertFalse((self.artifacts / 'deployed.json').exists())

    def test_success_requires_server_validation_migration_and_both_rollouts(self):
        events = self.deployment()
        stages = [args[0] for args, data in events]
        self.assertLess(stages.index('wait'), stages.index('rollout'))
        self.assertEqual(stages.count('rollout'), 2)
        self.assertTrue(any('--dry-run=server' in args for args, data in events))
        self.assertTrue((self.artifacts / 'deployed.json').exists())

    def test_changed_configuration_cannot_reuse_release_artifact(self):
        self.release()
        self.config['environment'] = 'production'
        with patch.object(p, 'ARTIFACTS', self.artifacts), patch.object(p, 'revision', return_value=SHA), patch.object(p, 'trusted_release'), \
             patch.object(p, 'kubectl') as kubectl, patch.dict(os.environ, {'CORE_CI_ALLOW_DEPLOY': '1'}):
            with self.assertRaises(p.PipelineError):
                p.deploy(self.config)
            kubectl.assert_not_called()

    def test_active_lease_blocks_a_second_pipeline(self):
        lease = {'metadata': {'resourceVersion': '1'}, 'spec': {'holderIdentity': 'other-runner', 'renewTime': p.timestamp(p.now()), 'leaseDurationSeconds': 120}}
        with patch.object(p, 'kubectl', side_effect=[p.PipelineError('exists'), json.dumps(lease)]):
            with self.assertRaises(p.PipelineError):
                with p.release_lock(self.config):
                    self.fail('An active lease must not be acquired')

    def test_expired_lease_uses_compare_and_swap_and_releases_ownership(self):
        lease = {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease', 'metadata': {'resourceVersion': '4'},
            'spec': {'holderIdentity': 'old', 'renewTime': p.timestamp(p.now() - timedelta(minutes=5)), 'leaseDurationSeconds': 120}}
        calls = []
        def kubectl(config, args, data=None, **kwargs):
            nonlocal lease
            calls.append((args, data))
            if args[0] == 'create':
                raise p.PipelineError('exists')
            if args[0] == 'replace':
                self.assertEqual(data['metadata']['resourceVersion'], '4')
                lease = copy.deepcopy(data)
                return ''
            return json.dumps(lease)
        with patch.object(p, 'kubectl', side_effect=kubectl):
            with p.release_lock(self.config) as verify:
                self.assertNotEqual(lease['spec']['holderIdentity'], 'old')
                verify()
        self.assertEqual(lease['spec']['holderIdentity'], '')

    def test_rbac_uses_namespace_role_and_cannot_create_secrets_or_clusters(self):
        resources = p.rbac(self.config)['items']
        self.assertEqual({r['kind'] for r in resources}, {'ServiceAccount', 'Role', 'RoleBinding'})
        rules = resources[1]['rules']
        secret_rule = next(rule for rule in rules if rule['resources'] == ['secrets'])
        self.assertEqual(secret_rule['verbs'], ['get'])
        self.assertTrue(secret_rule['resourceNames'])
        self.assertFalse(any('namespaces' in rule['resources'] for rule in rules))


if __name__ == '__main__':
    unittest.main()
