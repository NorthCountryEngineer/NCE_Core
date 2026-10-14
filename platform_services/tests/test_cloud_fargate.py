"""Offline Fargate release, identity and isolation contracts; no cloud calls."""
import copy
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.core.exceptions import ValidationError
from identity.access import Principal
from identity.models import Organization, Membership, User
from platform_services import cloud_aws, cloud_deployments
from platform_services.cloud_contract import ContractError, fargate_backend, identity_contract
from platform_services.cloud_models import CloudPlatform
from platform_services.environments import create_environment
from .test_cloud_aws import platform, snapshot, runner


IDENTITY = {'callback_path': '/api/auth/core/callback', 'permissions': [
    {'codename': 'app-read', 'name': 'Read Proposal Studio'},
    {'codename': 'app-write', 'name': 'Edit proposals'},
    {'codename': 'app-admin', 'name': 'Administer workspace'}]}


def configuration():
    value = platform()
    value.config['archetypes'].append('amplify-fargate')
    value.config['fargate_cloudformation_role_arn'] = 'arn:aws:iam::123456789012:role/nce-fargate-cfn'
    value.config['fargate_apps'] = {value.allowed_repositories[0]: {
        'image_repository': '123456789012.dkr.ecr.us-east-1.amazonaws.com/nce-tests-proposal-api',
        'browser_image_repository': '123456789012.dkr.ecr.us-east-1.amazonaws.com/nce-tests-proposal-browser',
        'runtime_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nce-tests-proposal-runtime-abcdef',
        'certificate_arn': 'arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012',
        'api_host': 'api.proposal.example.test'}}
    return value


def manifest(value):
    approved = value.config['fargate_apps'][value.allowed_repositories[0]]
    return {'schema': 1, 'archetype': 'amplify-fargate', 'frontend': {'directory': '.', 'output': 'dist'},
        'backend': {'image': approved['image_repository'] + '@sha256:' + 'a' * 64,
                    'browser_image': approved['browser_image_repository'] + '@sha256:' + 'b' * 64},
        'identity': copy.deepcopy(IDENTITY)}


class FargateContractTests(SimpleTestCase):
    def test_fixed_recipe_keeps_database_and_browser_credentials_isolated(self):
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        resources = template['Resources']
        self.assertEqual(template['Parameters']['DesiredCount']['Default'], 0)
        self.assertFalse(resources['Database']['Properties']['PubliclyAccessible'])
        self.assertTrue(resources['Database']['Properties']['StorageEncrypted'])
        self.assertGreaterEqual(resources['Database']['Properties']['BackupRetentionPeriod'], 7)
        for name in ['Database', 'DocumentBucket']:
            self.assertEqual(resources[name]['DeletionPolicy'], 'Retain')
            self.assertEqual(resources[name]['UpdateReplacePolicy'], 'Retain')
        browser = resources['BrowserTaskDefinition']['Properties']
        self.assertNotIn('TaskRoleArn', browser)
        self.assertNotIn('Secrets', browser['ContainerDefinitions'][0])
        self.assertNotIn('Environment', browser['ContainerDefinitions'][0])
        for name in ['Service', 'WorkerService', 'BrowserService']:
            self.assertEqual(resources[name]['Properties']['NetworkConfiguration']['AwsvpcConfiguration']['AssignPublicIp'], 'DISABLED')
        bootstrap = json.loads((runner.ROOT / 'fargate-bootstrap.template.json').read_text())
        for name in ['ApiRepository', 'BrowserRepository']:
            self.assertEqual(bootstrap['Resources'][name]['Properties']['ImageTagMutability'], 'IMMUTABLE')
            self.assertEqual(bootstrap['Resources'][name]['DeletionPolicy'], 'Retain')
        self.assertEqual(bootstrap['Resources']['RuntimeSecret']['Properties']['GenerateSecretString']['GenerateStringKey'], 'session_secret')

    def test_operator_references_reject_other_account_region_and_unknown_fields(self):
        value = configuration()
        self.assertEqual(cloud_aws.configuration(value)['config'], value.config)
        for field, replacement in [('runtime_secret_arn', 'arn:aws:secretsmanager:us-east-1:999999999999:secret:nce-runtime'),
                                   ('certificate_arn', 'arn:aws:acm:us-west-2:123456789012:certificate/12345678-1234-1234-1234-123456789012'),
                                   ('image_repository', '123456789012.dkr.ecr.us-east-1.amazonaws.com/other-app'),
                                   ('runtime_secret_arn', 'arn:aws:secretsmanager:us-east-1:123456789012:secret:another-runtime-abcdef'),
                                   ('api_host', 'api.example.test/path')]:
            altered = configuration()
            altered.config['fargate_apps'][altered.allowed_repositories[0]][field] = replacement
            with self.subTest(field=field), self.assertRaises(cloud_aws.AWSAdapterError):
                cloud_aws.configuration(altered)
        value.config['fargate_apps'][value.allowed_repositories[0]]['session_secret'] = 'must-not-be-propagated'
        with self.assertRaises(cloud_aws.AWSAdapterError):
            cloud_aws.configuration(value)

    def test_images_must_use_approved_repository_and_immutable_digest(self):
        value = configuration()
        approved = value.config['fargate_apps'][value.allowed_repositories[0]]
        backend = manifest(value)['backend']
        fargate_backend(backend, approved)
        for image in [approved['image_repository'] + ':latest', '999999999999.dkr.ecr.us-east-1.amazonaws.com/nce-other@sha256:' + 'a' * 64]:
            with self.subTest(image=image), self.assertRaises(ContractError):
                fargate_backend({**backend, 'image': image}, approved)
        with self.assertRaises(ContractError):
            fargate_backend({**backend, 'migration_command': ['arbitrary', 'command']}, approved)

    def test_identity_cannot_use_reserved_or_duplicate_permissions_or_remote_callback(self):
        identity_contract(IDENTITY)
        for value in [{'callback_path': '//attacker.test', 'permissions': IDENTITY['permissions']},
                      {'callback_path': '/api/callback', 'permissions': [{'codename': 'admin', 'name': 'Admin'}]},
                      {'callback_path': '/api/callback', 'permissions': IDENTITY['permissions'] * 2}]:
            with self.assertRaises(ContractError):
                identity_contract(value)

    def test_runner_manifest_cannot_select_a_template_or_commands(self):
        value = configuration()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'package.json').write_text(json.dumps({'scripts': {'build': 'vite build'}}))
            (root / 'package-lock.json').write_text('{}')
            document = manifest(value)
            (root / 'nce-deploy.json').write_text(json.dumps(document))
            self.assertEqual(runner.read_manifest(root, 'amplify-fargate'), document)
            document['template'] = 'application-supplied.json'
            (root / 'nce-deploy.json').write_text(json.dumps(document))
            with self.assertRaises(runner.RunnerError):
                runner.read_manifest(root, 'amplify-fargate')

    def test_update_preserves_running_image_until_controlled_migration(self):
        value = configuration()
        snap = snapshot(value)
        snap.update(archetype='amplify-fargate', public_origin='https://proposal.example.test')
        payload = cloud_aws._snapshot(value, snap)[1]
        snap = json.loads(payload)
        params = {v['ParameterKey']: v for v in runner.stack_parameters(snap['platform'], snap, manifest(value), '', existing=True)}
        for name in ['ApiImageUri', 'BrowserImageUri', 'DesiredCount', 'CoreIssuer']:
            self.assertTrue(params[name]['UsePreviousValue'])
        snap['action'] = 'release'
        params = {v['ParameterKey']: v for v in runner.stack_parameters(snap['platform'], snap, manifest(value), '', existing=True)}
        self.assertEqual(params['DesiredCount']['ParameterValue'], '0')
        self.assertNotIn('LambdaCodeBucket', params)
        self.assertEqual(params['ApiRepositoryName']['ParameterValue'], 'nce-tests-proposal-api')
        activated = {v['ParameterKey']: v for v in runner.stack_parameters(snap['platform'], snap, manifest(value), '', existing=True, activate=True)}
        self.assertEqual(activated['DesiredCount']['ParameterValue'], '1')

    def test_unregistered_provision_satisfies_the_real_template_parameter_constraints(self):
        import re
        value = configuration()
        snap = snapshot(value)
        snap.update(archetype='amplify-fargate', public_origin='https://proposal.example.test')
        snap = json.loads(cloud_aws._snapshot(value, snap)[1])
        parameters = runner.stack_parameters(snap['platform'], snap, manifest(value), '')
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        for parameter in parameters:
            key, text = parameter['ParameterKey'], parameter['ParameterValue']
            contract = template['Parameters'][key]
            if contract.get('AllowedPattern'):
                with self.subTest(parameter=key):
                    self.assertIsNotNone(re.fullmatch(contract['AllowedPattern'], text))
        values = {entry['ParameterKey']: entry['ParameterValue'] for entry in parameters}
        self.assertEqual(values['CoreIssuer'], 'https://pending.invalid')
        self.assertEqual(values['CoreRedirectUri'], 'https://proposal.example.test/api/auth/core/callback')
        self.assertEqual(values['DesiredCount'], '0')

    def test_only_immutable_task_revisions_may_be_replaced(self):
        runner.no_destructive_changes([{'ResourceChange': {'Action': 'Modify', 'ResourceType': 'AWS::ECS::TaskDefinition', 'Replacement': 'True'}}])
        for resource in ['AWS::RDS::DBInstance', 'AWS::S3::Bucket', 'AWS::ECS::Service']:
            with self.assertRaises(runner.RunnerError):
                runner.no_destructive_changes([{'ResourceChange': {'Action': 'Modify', 'ResourceType': resource, 'Replacement': 'True'}}])
        with self.assertRaises(runner.RunnerError):
            runner.no_destructive_changes([{'ResourceChange': {'Action': 'Remove', 'ResourceType': 'AWS::ECS::TaskDefinition'}}])

    def migration_runner(self):
        value = configuration()
        snap = snapshot(value, 'release')
        snap['archetype'] = 'amplify-fargate'
        snap = json.loads(cloud_aws._snapshot(value, snap)[1])
        ecs = Mock()
        instance = runner.Runner(snap, 'c' * 64, 'runs/result.json', client_factory=lambda _: ecs)
        instance.runtime_outputs = {'Cluster': 'nce-cluster', 'MigrationTaskDefinition': 'nce-migration',
            'PrivateSubnet1': 'subnet-one', 'PrivateSubnet2': 'subnet-two', 'TaskSecurityGroup': 'sg-task',
            'Service': 'api', 'WorkerService': 'worker', 'BrowserService': 'browser'}
        ecs.run_task.return_value = {'tasks': [{'taskArn': 'migration-task'}]}
        instance.provision = Mock()
        return instance, ecs, manifest(value)

    def test_failed_migration_never_activates_services(self):
        instance, ecs, document = self.migration_runner()
        ecs.describe_tasks.return_value = {'tasks': [{'containers': [{'name': 'migration', 'exitCode': 1}]}]}
        with self.assertRaisesMessage(runner.RunnerError, 'migration failed'):
            instance.migrate_and_activate(document, Path('/tmp'))
        instance.provision.assert_not_called()
        args = ecs.run_task.call_args.kwargs
        self.assertNotIn('overrides', args)
        self.assertEqual(args['networkConfiguration']['awsvpcConfiguration']['assignPublicIp'], 'DISABLED')
        self.assertEqual(args['networkConfiguration']['awsvpcConfiguration']['subnets'], ['subnet-one', 'subnet-two'])

    def test_successful_migration_starts_only_expected_services(self):
        instance, ecs, document = self.migration_runner()
        ecs.describe_tasks.return_value = {'tasks': [{'containers': [{'name': 'migration', 'exitCode': 0}]}]}
        ecs.describe_services.return_value = {'services': [{'runningCount': 1, 'desiredCount': 1}] * 3}
        instance.migrate_and_activate(document, Path('/tmp'))
        instance.provision.assert_called_once_with(document, Path('/tmp'), activate=True)
        self.assertEqual(instance.steps, ['database-migrated', 'services-healthy'])

    def test_receipt_identity_and_api_host_match_only_approved_fargate_contract(self):
        value = configuration()
        snap = snapshot(value)
        snap['archetype'] = 'amplify-fargate'
        clients = {'s3': Mock(), 'codebuild': Mock()}
        clients['codebuild'].batch_get_builds.return_value = {'builds': [{'buildStatus': 'SUCCEEDED'}]}
        state = {'build_id': 'nce-test-runner:job-1', 'result_key': 'runs/' + 'c' * 64 + '/result.json'}
        receipt = {'status': 'succeeded', 'commit': snap['commit'], 'action': 'provision', 'outputs': {
            'origin': 'https://production.dtest.amplifyapp.com', 'amplify_app_id': 'dtest',
            'stack_name': 'nce-tests-123456789abc-12345678', 'api_url': 'https://api.proposal.example.test',
            'identity': json.dumps(IDENTITY), 'client_secret': 'private-should-be-filtered'}}
        clients['s3'].get_object.return_value = {'Body': io.BytesIO(json.dumps(receipt).encode())}
        result = cloud_aws.poll_run(value, snap, state, client_factory=clients.__getitem__)
        self.assertNotIn('private-should-be-filtered', json.dumps(result))
        receipt['outputs']['api_url'] = 'https://another-app.example.test'
        clients['s3'].get_object.return_value = {'Body': io.BytesIO(json.dumps(receipt).encode())}
        with self.assertRaises(cloud_aws.AWSAdapterError):
            cloud_aws.poll_run(value, snap, state, client_factory=clients.__getitem__)


class FargateRegistrationTests(TestCase):
    @override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=True)
    def test_worker_stores_fargate_credentials_before_release_without_exposing_them(self):
        organization = Organization.objects.create(name='Proposal handoff', slug='proposal-handoff')
        actor = User.objects.create_user('handoff-fixture@example.test', 'local-test-only')
        Membership.objects.create(organization=organization, user=actor, kind='administrator')
        environment = create_environment(organization, name='production', namespace='handoff-production', application_origin='https://proposal.example.test')
        fixture = configuration()
        fixture.config['environment_ids'] = [str(environment.pk)]
        destination = CloudPlatform(organization=organization, name='AWS handoff fixture', mode='aws',
            **{key: getattr(fixture, key) for key in ['account_id', 'region', 'prefix', 'role_arn', 'runner_project', 'connection_arn', 'allowed_repositories', 'config']})
        destination.external_id = fixture.external_id
        destination.full_clean()
        destination.save()
        with patch('platform_services.cloud_aws.verify', return_value={}):
            destination = cloud_deployments.verify_platform(destination, Principal(user=actor))
        app = cloud_deployments.create_application(organization, Principal(user=actor), platform=destination, environment=environment,
            name='Proposal Studio', slug='proposal-studio', repository=fixture.allowed_repositories[0], branch='main',
            commit='a' * 40, archetype='amplify-fargate', public_origin='https://proposal.example.test')
        release = cloud_deployments.prepare_release(app, Principal(user=actor))
        release = cloud_deployments.approve_release(release, Principal(user=actor), confirm_costs=True)
        with patch('platform_services.cloud_aws.start_run', return_value={'build_id': 'fixture-runner:build-one'}):
            self.assertTrue(cloud_deployments.process_one())
        type(release).objects.filter(pk=release.pk).update(next_poll=None)
        result = {'status': 'succeeded', 'outputs': {'origin': 'https://production.dtest.amplifyapp.com',
            'api_url': 'https://api.proposal.example.test', 'identity': json.dumps(IDENTITY)}}
        with patch('platform_services.cloud_aws.poll_run', return_value=result), patch('platform_services.cloud_aws.store_credentials') as store:
            self.assertTrue(cloud_deployments.process_one())
        release.refresh_from_db()
        self.assertEqual(release.status, 'running', release.error)
        self.assertEqual(release.provider_state['phase'], 'release')
        store.assert_called_once()
        _, parameter, credentials = store.call_args.args
        self.assertEqual(parameter, f'/{destination.prefix}/apps/{app.pk}/core-resource-client')
        self.assertEqual(set(credentials), {'client_id', 'client_secret'})
        self.assertNotIn(credentials['client_secret'], json.dumps(release.outputs) + json.dumps(release.provider_state))
        self.assertEqual(release.steps[1]['status'], 'succeeded')

    def test_registration_preserves_permission_vocabulary_callback_and_paired_public_client(self):
        organization = Organization.objects.create(name='Proposal organization', slug='proposal-test')
        admin = User.objects.create_user('proposal-admin@example.test', 'local-test-only')
        Membership.objects.create(organization=organization, user=admin, kind='administrator')
        environment = create_environment(organization, name='development', namespace='proposal-dev', application_origin='https://proposal.example.test')
        destination = CloudPlatform.objects.create(organization=organization, name='Proposal preview', mode='demo',
            allowed_repositories=['Example/proposal'], config={'environment_ids': [str(environment.pk)], 'archetypes': ['amplify-fargate']})
        destination = cloud_deployments.verify_platform(destination, Principal(user=admin))
        app = cloud_deployments.create_application(organization, Principal(user=admin), platform=destination, environment=environment,
            name='Proposal Studio', slug='proposal-studio', repository='Example/proposal', branch='main', commit='a' * 40,
            archetype='amplify-fargate', public_origin='https://proposal.example.test')
        release = cloud_deployments.prepare_release(app, Principal(user=admin))
        variables, credentials, deployment = cloud_deployments._register(release, {'identity': json.dumps(IDENTITY)})
        self.assertEqual(deployment.application.client_type, 'public')
        self.assertEqual(deployment.resource_application.client_type, 'confidential')
        self.assertEqual(deployment.application.redirect_uris, 'https://proposal.example.test/api/auth/core/callback')
        self.assertEqual(deployment.installation.manifest['permissions'], IDENTITY['permissions'])
        self.assertEqual(variables['VITE_CORE_DEPLOYMENT_ID'], str(deployment.pk))
        self.assertNotIn(credentials['client_secret'], json.dumps(variables))
        altered = copy.deepcopy(IDENTITY)
        altered['permissions'].append({'codename': 'new-permission', 'name': 'Unreviewed permission change'})
        with self.assertRaises(ValidationError):
            cloud_deployments._register(release, {'identity': json.dumps(altered)})
