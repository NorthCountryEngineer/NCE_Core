"""Offline Core recipe, identity and exact-host publication acceptance.

No AWS/network requests, paid builds, model calls, or application code execution.
"""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from identity.access import Principal
from identity.models import Membership, Organization, User
from platform_services import cloud_aws, cloud_contract
from platform_services.cloud_deployments import approve_release, create_application, prepare_release, process_one, verify_platform
from platform_services.cloud_models import CloudPlatform, CloudRelease
from platform_services.environments import create_environment
from platform_services.tests.test_cloud_aws import platform, snapshot, runner

REPOSITORY = 'NorthCountryEngineer/Game_Studio'


def studio_config():
    return {'version': 1, 'branding': {'appName': 'Game Studio', 'badgeText': 'Powered by Game Studio', 'accentColor': '#176a63'},
        'hosts': [{'id': 'academy', 'name': 'Academy lessons', 'origin': 'https://academy.example.test', 'returnPath': '/lessons/games'}]}


def configuration():
    value = platform()
    value.allowed_repositories = [REPOSITORY]
    value.config['archetypes'].append('amplify-game-studio')
    value.config['game_studio_apps'] = {REPOSITORY: studio_config()}
    return value


def game_snapshot(value, action='provision'):
    result = snapshot(value, action)
    result['archetype'] = 'amplify-game-studio'
    return result


def checkout(root):
    (root / 'backend/nce_api').mkdir(parents=True)
    (root / 'backend/nce_api/handler.py').write_text('def handler(event, context): return {}\n')
    (root / 'backend/nce_api/integration.py').write_text('def configuration(): return {}\n')
    (root / 'backend/nce_api/agent-framework.json').write_text('{"schema":1}')
    (root / 'package.json').write_text(json.dumps({'scripts': {'build': 'exit 7'}}))
    (root / 'package-lock.json').write_text('{}')
    value = {'schema': 1, 'archetype': 'amplify-game-studio', 'frontend': {'directory': '.', 'output': 'dist'},
        'backend': {'directory': 'backend/nce_api', 'handler': 'handler.handler'}}
    (root / 'nce-deploy.json').write_text(json.dumps(value))
    return value


class GameStudioCloudContractTests(SimpleTestCase):
    def test_platform_requires_explicit_repository_approval_before_any_aws_access(self):
        value = configuration()
        approved = cloud_aws.configuration(value)
        self.assertIn('game_studio_apps', approved['config'])
        request = game_snapshot(value)
        cloud_aws._snapshot(value, request)
        value.config.pop('game_studio_apps')
        request['platform_config'] = deepcopy(value.config)
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, 'Approve this Game Studio'):
            cloud_aws._snapshot(value, request)
        value.config['game_studio_apps'] = {'Attacker/other': studio_config()}
        with self.assertRaisesMessage(cloud_aws.AWSAdapterError, 'approved repository'):
            cloud_aws.configuration(value)

    def test_configuration_rejects_authority_and_markup_injection(self):
        for changes in [{'credentials': 'no'}, {'version': True}, {'hosts': [{'id': 'x', 'name': 'X', 'origin': '*', 'returnPath': '/'}]}]:
            with self.subTest(changes=changes), self.assertRaises(cloud_contract.ContractError):
                cloud_contract.game_studio_configuration({**studio_config(), **changes})
        for origin in ['https://*.example.test', 'http://example.test', 'https://user:pass@example.test', 'https://example.test/path', 'https://example.test?token=x', 'https://example.test#x', 'https://EXAMPLE.test', 'https://example.test:443', 'https://@example.test', 'https://example.test?', 'https://example.test#', 'https://0177.0.0.1', 'https://2130706433']:
            config = studio_config(); config['hosts'][0]['origin'] = origin
            with self.assertRaises(cloud_contract.ContractError): cloud_contract.game_studio_configuration(config)
        for path in ['//attacker.test', '/a?code=x', '/../x', '/%2f%2fattacker.test']:
            config = studio_config(); config['hosts'][0]['returnPath'] = path
            with self.assertRaises(cloud_contract.ContractError): cloud_contract.game_studio_configuration(config)
        for changes in [{'appName': '<img onerror=evil>'}, {'accentColor': 'red;display:none'}, {'logoUrl': 'data:image/svg+xml,evil'}]:
            config = studio_config(); config['branding'].update(changes)
            with self.assertRaises(cloud_contract.ContractError): cloud_contract.game_studio_configuration(config)

    def test_duplicate_hosts_and_lambda_environment_overflow_are_rejected(self):
        config = studio_config(); config['hosts'] *= 2
        with self.assertRaises(cloud_contract.ContractError): cloud_contract.game_studio_configuration(config)
        config = studio_config()
        config['hosts'] = [{'id': 'host-' + str(i), 'name': 'H', 'origin': 'https://host' + str(i) + '.example.test', 'returnPath': '/' + 'x' * 200} for i in range(10)]
        with self.assertRaises(cloud_contract.ContractError): cloud_contract.game_studio_configuration(config)

    def test_paid_models_require_explicit_operator_settings_and_exact_account_bound_arns(self):
        value = configuration(); approved = value.config['game_studio_apps'][REPOSITORY]
        model = 'arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0'
        profile = 'arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-micro-v1:0'
        approved.update(allow_paid_models=True, bedrock_model_arns=[model, profile])
        cloud_aws.configuration(value)
        request = game_snapshot(value, 'release'); _, encoded = cloud_aws._snapshot(value, request); parsed = json.loads(encoded)
        parameters = {row['ParameterKey']: row['ParameterValue'] for row in runner.stack_parameters(parsed['platform'], parsed, {'backend': {'handler': 'handler.handler'}}, 'code/api.zip')}
        self.assertEqual(parameters['AllowPaidModels'], 'true')
        self.assertEqual(parameters['ApprovedModelArns'], model + ',' + profile)
        self.assertEqual(json.loads(parameters['GameStudioConfigJson']), studio_config())
        for arns in [[], ['*'], [model + '*'], [profile.replace('123456789012', '999999999999')], [model, model]]:
            approved['bedrock_model_arns'] = arns
            with self.assertRaises(cloud_aws.AWSAdapterError): cloud_aws.configuration(value)

    def test_game_studio_manifest_and_backend_are_fixed_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manifest = checkout(root)
            with patch.object(runner.subprocess, 'run', side_effect=AssertionError('Never execute application code in privileged runner')):
                self.assertEqual(runner.read_manifest(root, 'amplify-game-studio'), manifest)
                archive = root / 'api.zip'; runner.package_lambda(root, manifest['backend'], archive)
            with ZipFile(archive) as packed:
                self.assertEqual(set(packed.namelist()), {'handler.py', 'integration.py', 'agent-framework.json'})
            for invalid in [{'directory': 'backend/nce_api', 'handler': 'integration.configuration'}, {'directory': '.', 'handler': 'handler.handler'}]:
                manifest['backend'] = invalid; (root / 'nce-deploy.json').write_text(json.dumps(manifest))
                with self.assertRaises(runner.RunnerError): runner.read_manifest(root, 'amplify-game-studio')
            manifest = {'schema': 1, 'archetype': 'amplify-game-studio', 'frontend': {'directory': '.', 'output': 'dist'}, 'backend': {'directory': 'backend/nce_api', 'handler': 'handler.handler'},
                'identity': {'callback_path': '/oauth/callback', 'permissions': [{'codename': 'admin', 'name': 'Admin'}]}}
            (root / 'nce-deploy.json').write_text(json.dumps(manifest))
            with self.assertRaises(runner.RunnerError): runner.read_manifest(root, 'amplify-game-studio')

    def test_missing_or_symlinked_agent_registry_fails_packaging_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); checkout(root); registry = root / 'backend/nce_api/agent-framework.json'
            registry.unlink()
            with self.assertRaises(runner.RunnerError): runner.read_manifest(root, 'amplify-game-studio')
            registry.symlink_to(root / 'package.json')
            with self.assertRaises(runner.RunnerError): runner.read_manifest(root, 'amplify-game-studio')

    def test_fixed_template_parameters_use_only_operator_approved_hosts_and_config(self):
        value = configuration(); request = game_snapshot(value, 'release')
        request['public_config'] = {'VITE_APPLICATION_ORIGIN': 'https://games.example.test', 'VITE_CORE_ISSUER': 'https://core.example.test',
            'VITE_CORE_ORGANIZATION_ID': 'org', 'VITE_CORE_SERVICE_ID': 'service', 'VITE_GAME_STUDIO_CONFIG_JSON': '{"hosts":["https://attacker.test"]}'}
        _, encoded = cloud_aws._snapshot(value, request); parsed = json.loads(encoded)
        parameters = {row['ParameterKey']: row['ParameterValue'] for row in runner.stack_parameters(parsed['platform'], parsed,
            {'backend': {'directory': 'backend/nce_api', 'handler': 'handler.handler'}}, 'code/api.zip')}
        self.assertEqual(json.loads(parameters['GameStudioConfigJson']), studio_config())
        self.assertEqual(parameters['AllowedOrigins'], 'https://games.example.test,https://academy.example.test')
        self.assertNotIn('attacker', str(parameters))
        template = json.loads((runner.ROOT / 'game-studio.template.json').read_text())
        self.assertEqual(set(parameters), set(template['Parameters']))
        parsed['action'] = 'provision'
        preserved = {row['ParameterKey']: row for row in runner.stack_parameters(parsed['platform'], parsed, {'backend': {'handler': 'handler.handler'}}, 'new.zip', existing=True)}
        for key in ['CoreIssuer', 'CoreOrganizationId', 'CoreServiceId', 'ApplicationOrigin', 'AllowedOrigins', 'GameStudioConfigJson', 'AllowPaidModels', 'ApprovedModelArns']:
            self.assertEqual(preserved[key], {'ParameterKey': key, 'UsePreviousValue': True})

    def test_template_keeps_five_tables_private_and_retained_with_existing_boundary(self):
        template = json.loads((runner.ROOT / 'game-studio.template.json').read_text())
        self.assertLess(len((runner.ROOT / 'game-studio.template.json').read_bytes()), 51200)
        resources = template['Resources']
        tables = {key for key, value in resources.items() if value['Type'] == 'AWS::DynamoDB::Table'}
        self.assertEqual(tables, {'PrivateTable', 'PublicTable', 'AssistantState', 'GameDrafts', 'PublishedGames'})
        for name in tables:
            self.assertEqual(resources[name]['DeletionPolicy'], 'Retain'); self.assertEqual(resources[name]['UpdateReplacePolicy'], 'Retain')
            self.assertEqual(resources[name]['Properties']['BillingMode'], 'PAY_PER_REQUEST')
        role = resources['RuntimeRole']['Properties']
        self.assertEqual(role['PermissionsBoundary'], {'Ref': 'PermissionsBoundaryArn'})
        statements = role['Policies'][0]['PolicyDocument']['Statement']
        ddb = next(item for item in statements if 'dynamodb:GetItem' in item['Action'])
        self.assertEqual({item['Fn::GetAtt'][0] for item in ddb['Resource']}, tables)
        self.assertIn('dynamodb:ConditionCheckItem', ddb['Action'])
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        boundary = next(item for item in onboarding['Resources']['AppBoundary']['Properties']['PolicyDocument']['Statement'] if item.get('Sid') == 'AppTables')
        self.assertIn('dynamodb:ConditionCheckItem', boundary['Action'])
        self.assertNotIn('rekognition:', json.dumps(statements))
        paid = statements[-1]['Fn::If']
        self.assertEqual(paid[0], 'PaidGameModels')
        self.assertEqual(paid[1]['Resource'], {'Ref': 'ApprovedModelArns'})
        self.assertEqual(paid[1]['Action'], ['bedrock:InvokeModel', 'bedrock:GetInferenceProfile'])
        self.assertEqual(paid[2], {'Ref': 'AWS::NoValue'})
        self.assertEqual(template['Parameters']['AllowPaidModels']['Default'], 'false')
        self.assertEqual(template['Parameters']['ApprovedModelArns']['Default'], '')
        self.assertEqual(onboarding['Parameters']['GameStudioModelArns']['Default'], '')
        self.assertEqual(resources['ApiIntegration']['Properties']['PayloadFormatVersion'], '2.0')
        self.assertEqual(resources['Api']['Properties']['CorsConfiguration']['AllowOrigins'], {'Ref': 'AllowedOrigins'})

    def test_runner_selects_pinned_core_template_not_repository_template(self):
        value = configuration(); request = game_snapshot(value)
        _, encoded = cloud_aws._snapshot(value, request); parsed = json.loads(encoded)
        s3, cfn = Mock(), Mock()
        cfn.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'Parameters': [], 'Outputs': [
            {'OutputKey': 'AmplifyAppId', 'OutputValue': 'dtest123'}, {'OutputKey': 'Origin', 'OutputValue': 'https://production.dtest123.amplifyapp.com'}, {'OutputKey': 'ApiUrl', 'OutputValue': 'https://api.example.test'}]}]}
        target = runner.Runner(parsed, 'c' * 64, 'runs/result.json', client_factory={'s3': s3, 'cloudformation': cfn}.__getitem__)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manifest = checkout(root)
            (root / 'game-studio.template.json').write_text('{"Resources":{"EVIL":{}}}')
            with patch.object(target, 'wait_change_set', return_value=[]): target.provision(manifest, root)
        arguments = cfn.create_change_set.call_args.kwargs
        self.assertEqual(arguments['TemplateBody'], (runner.ROOT / 'game-studio.template.json').read_text())
        self.assertEqual(arguments['RoleARN'], value.config['cloudformation_role_arn'])
        s3.upload_file.assert_called_once()

    def test_trusted_hosting_headers_deny_creator_framing_and_allow_only_public_player(self):
        text = cloud_contract.game_studio_hosting_headers(studio_config())
        self.assertIn('frame-ancestors https://academy.example.test;', text)
        sections = text.split('  - pattern: ')
        embed = next(part for part in sections if part.startswith('"/embed/games/**"'))
        self.assertNotIn('X-Frame-Options', embed)
        studio = next(part for part in sections if part.startswith('"/game-studio"'))
        self.assertIn('DENY', studio); self.assertIn("frame-ancestors 'none'", studio)
        config = studio_config(); config['hosts'] = []
        self.assertNotIn('frame-ancestors https://', cloud_contract.game_studio_hosting_headers(config))

    def test_publisher_replaces_untrusted_archive_headers_before_amplify_upload(self):
        value = configuration()
        value.config['game_studio_apps'][REPOSITORY].update(allow_paid_models=True, bedrock_model_arns=['arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0'])
        _, encoded = cloud_aws._snapshot(value, game_snapshot(value, 'release')); parsed = json.loads(encoded)
        s3, amplify = Mock(), Mock()
        s3.head_object.return_value = {'ContentLength': 100}
        amplify.create_deployment.return_value = {'zipUploadUrl': 'https://upload.s3.amazonaws.com/file', 'jobId': '1'}
        amplify.get_job.return_value = {'job': {'summary': {'status': 'SUCCEED'}}}
        target = runner.Runner(parsed, 'c' * 64, 'runs/result.json', client_factory={'s3': s3, 'amplify': amplify}.__getitem__)
        target.outputs = {'amplify_app_id': 'dtest123', 'origin': 'https://games.example.test'}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'frontend.zip'
            with ZipFile(source, 'w') as archive:
                archive.writestr('index.html', '<h1>Game Studio</h1>')
                archive.writestr('customHttp.yml', "customHeaders: evil wildcard policy")
            s3.download_file.side_effect = lambda bucket, key, path, **kwargs: shutil.copyfile(source, path)
            response = Mock(); response.status = 200
            opened = Mock(); opened.__enter__ = Mock(return_value=response); opened.__exit__ = Mock(return_value=False)
            with patch.object(runner, 'urlopen', return_value=opened) as upload:
                target.publish_frontend('builds/test/frontend.zip')
            with ZipFile(io.BytesIO(upload.call_args.args[0].data)) as archive:
                self.assertEqual(archive.read('customHttp.yml').decode(), cloud_contract.game_studio_hosting_headers(studio_config()))
                self.assertEqual(archive.read('index.html'), b'<h1>Game Studio</h1>')
        self.assertEqual(target.steps, ['frontend-published'])

    def test_game_studio_identity_is_a_fixed_scoped_capability_vocabulary(self):
        identity = cloud_contract.game_studio_identity()
        self.assertEqual(identity['callback_path'], '/oauth/callback')
        self.assertEqual({entry['codename'] for entry in identity['permissions']}, {'games-play', 'games-create', 'games-publish', 'games-manage', 'agents-manage', 'accounts-manage'})
        roles = {role['id']: role for role in identity['account_roles']}
        self.assertEqual(set(roles), {'player', 'creator', 'publisher', 'administrator'})
        self.assertEqual(roles['player']['permissions'], ['games-play'])
        self.assertEqual(roles['creator']['permissions'], ['games-create'])
        self.assertNotIn('games-create', [entry['codename'] for entry in identity['permissions'] if entry['codename'] == 'app-read'])


class GameStudioCloudDeploymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Game Studio tests', slug='game-studio-cloud')
        cls.admin = User.objects.create_user('game-studio-cloud@example.test', 'offline-test-password')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        cls.environment = create_environment(cls.org, name='development', namespace='games-dev', application_origin='https://games.example.test')
        cls.platform = CloudPlatform.objects.create(organization=cls.org, name='Game Studio preview', mode='demo', allowed_repositories=[REPOSITORY],
            config={'environment_ids': [str(cls.environment.pk)], 'archetypes': ['amplify-game-studio'], 'game_studio_apps': {REPOSITORY: studio_config()}})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.admin))

    def test_console_pipeline_registers_real_game_roles_and_public_config_without_provider_calls(self):
        principal = Principal(user=self.admin)
        app = create_application(self.org, principal, platform=self.platform, environment=self.environment,
            name='Game Studio', slug='game-studio', repository=REPOSITORY, branch='main', commit='a' * 40, archetype='amplify-game-studio')
        release = prepare_release(app, principal); approve_release(release, principal)
        with patch('platform_services.cloud_aws.start_run', side_effect=AssertionError('No AWS calls in simulation')):
            self.assertTrue(process_one())
            release.refresh_from_db(); app.refresh_from_db()
            self.assertEqual(release.status, 'running', release.error)
            self.assertIsNotNone(app.installation_id)
            self.assertEqual(app.installation.manifest['permissions'], cloud_contract.game_studio_identity()['permissions'])
            self.assertEqual(app.installation.manifest['account_roles'], cloud_contract.game_studio_identity()['account_roles'])
            self.assertEqual(app.installation.deployments.get(target='external').application.redirect_uris, 'https://game-studio.example.test/oauth/callback')
            public = release.provider_state['public_config']
            self.assertEqual(public['VITE_APP_MODE'], 'nce')
            self.assertEqual(json.loads(public['VITE_GAME_STUDIO_CONFIG_JSON']), studio_config())
            self.assertTrue(all(key.startswith('VITE_') for key in public))
            self.assertNotIn('client_secret', json.dumps(release.provider_state))
            CloudRelease.objects.filter(pk=release.pk).update(next_poll=None)
            self.assertTrue(process_one())
        release.refresh_from_db(); self.assertEqual(release.status, 'succeeded', release.error)
