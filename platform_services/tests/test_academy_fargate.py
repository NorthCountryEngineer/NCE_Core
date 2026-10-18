"""Offline Academy recipe admission, isolation and controlled migration tests."""
import copy
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import Mock

from django.test import SimpleTestCase, TestCase
from platform_services import cloud_aws
from platform_services.cloud_contract import ContractError, academy_fargate_backend
from platform_services.github_ci import POLICIES
from .test_cloud_aws import platform, snapshot, runner


def configuration():
    value = platform()
    value.allowed_repositories = ['NorthCountryEngineer/NCAcademy']
    value.config['archetypes'].append('amplify-academy-fargate')
    value.config['fargate_cloudformation_role_arn'] = 'arn:aws:iam::123456789012:role/nce-fargate-cfn'
    value.config['academy_fargate_apps'] = {value.allowed_repositories[0]: {
        'image_repository': '123456789012.dkr.ecr.us-east-1.amazonaws.com/nce-tests-academy-api',
        'runtime_secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nce-tests-academy-runtime-abcdef',
        'certificate_arn': 'arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012',
        'api_host': 'api.academy.example.test'}}
    return value


def manifest(value):
    approved = value.config['academy_fargate_apps'][value.allowed_repositories[0]]
    return {'schema': 1, 'archetype': 'amplify-academy-fargate',
        'frontend': {'directory': '.', 'output': 'dist'},
        'backend': {'image': approved['image_repository'] + '@sha256:' + 'a' * 64},
        'identity': {'callback_path': '/api/auth/core/callback', 'permissions': [
            {'codename': 'academy-use', 'name': 'Use Academy'}]}}


class AcademyFargateTests(SimpleTestCase):
    def test_configuration_requires_exact_approved_runtime_references(self):
        value = configuration()
        cloud_aws.configuration(value)
        for name, item in [('browser_image_repository', 'bad'), ('command', ['sh']), ('session_secret', 'bad'), ('allow_live_models', 'true')]:
            altered = copy.deepcopy(value)
            altered.config['academy_fargate_apps'][altered.allowed_repositories[0]][name] = item
            with self.assertRaises(cloud_aws.AWSAdapterError):
                cloud_aws.configuration(altered)
        altered = copy.deepcopy(value)
        altered.config['academy_fargate_apps']['Unapproved/repository'] = altered.config['academy_fargate_apps'].pop(value.allowed_repositories[0])
        with self.assertRaises(cloud_aws.AWSAdapterError):
            cloud_aws.configuration(altered)

    def test_backend_refuses_tags_extra_images_and_other_repositories(self):
        value = configuration()
        good = manifest(value)['backend']
        approval = value.config['academy_fargate_apps'][value.allowed_repositories[0]]
        academy_fargate_backend(good, approval)
        for bad in ({'image': good['image'].split('@')[0] + ':latest'},
                    {**good, 'browser_image': good['image']}, {**good, 'command': 'anything'},
                    {'image': good['image'].replace('academy-api', 'another-api')}):
            with self.assertRaises(ContractError):
                academy_fargate_backend(bad, approval)

    def test_runner_admits_fixed_manifest_and_rejects_injected_command(self):
        document = manifest(configuration())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'package.json').write_text(json.dumps({'scripts': {'build': 'vite build'}}))
            (root / 'package-lock.json').write_text('{}')
            (root / 'nce-deploy.json').write_text(json.dumps(document))
            self.assertEqual(runner.read_manifest(root, 'amplify-academy-fargate'), document)
            document['backend']['command'] = 'sh'
            (root / 'nce-deploy.json').write_text(json.dumps(document))
            with self.assertRaises(runner.RunnerError):
                runner.read_manifest(root, 'amplify-academy-fargate')

    def test_stack_parameters_match_dedicated_template_without_browser_values(self):
        value = configuration()
        settings = cloud_aws.configuration(value)
        snap = snapshot(value)
        snap.update(archetype='amplify-academy-fargate', secret_parameter='/nce-tests/apps/app/core-resource-client')
        parameters = runner.stack_parameters(settings, snap, manifest(value), '', activate=False)
        mapped = {item['ParameterKey']: item.get('ParameterValue') for item in parameters}
        template = json.loads((runner.ROOT / 'academy-fargate.template.json').read_text())
        self.assertLess((runner.ROOT / 'academy-fargate.template.json').stat().st_size, 51200)
        self.assertTrue(set(mapped) <= set(template['Parameters']))
        self.assertEqual(mapped['DesiredCount'], '0')
        self.assertEqual(mapped['AllowLiveModels'], 'false')
        self.assertNotIn('BrowserImageUri', mapped)
        self.assertNotIn('StorageName', mapped)
        self.assertEqual(mapped['CoreRedirectUri'], 'https://pending.invalid/api/auth/core/callback')

    def test_template_isolated_api_and_database_with_no_object_or_browser_authority(self):
        template = json.loads((runner.ROOT / 'academy-fargate.template.json').read_text())
        resources = template['Resources']
        self.assertEqual(sum(r['Type'] == 'AWS::ECS::Service' for r in resources.values()), 1)
        self.assertNotIn('DocumentBucket', resources)
        self.assertNotIn('BrowserTaskDefinition', resources)
        self.assertNotIn('WorkerTaskDefinition', resources)
        for name, command in [('TaskDefinition', ['python', '-m', 'academy.runtime', 'api']),
                              ('MigrationTaskDefinition', ['python', '-m', 'academy.migrate'])]:
            props = resources[name]['Properties']
            self.assertNotIn('TaskRoleArn', props)
            self.assertEqual(props['RuntimePlatform']['CpuArchitecture'], 'ARM64')
            container = props['ContainerDefinitions'][0]
            self.assertEqual(container['Command'], command)
            self.assertEqual(container['User'], '10001:10001')
            self.assertTrue(container['ReadonlyRootFilesystem'])
            env = {item['Name']: item['Value'] for item in container['Environment']}
            self.assertEqual(env['DEMO_SEED'], 'false')
            self.assertEqual(env['ALLOW_LIVE_MODELS'], {'Ref': 'AllowLiveModels'})
            self.assertEqual(env['DB_SSLMODE'], 'verify-full')
            self.assertEqual(env['DB_NAME'], 'academy')
        self.assertEqual(resources['TargetGroup']['Properties']['HealthCheckPath'], '/api/ready')
        self.assertEqual(resources['Database']['DeletionPolicy'], 'Retain')
        self.assertTrue(resources['Database']['Properties']['DeletionProtection'])
        grants = resources['ExecutionRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        self.assertTrue(any('ecr:BatchGetImage' in grant['Action'] for grant in grants))
        serialized = json.dumps(template)
        self.assertNotIn('BrowserRepositoryName', serialized)
        self.assertNotIn('studio.runtime', serialized)
        known = set(resources) | set(template['Parameters']) | {'AWS::AccountId', 'AWS::Region', 'AWS::Partition'}
        def check(node):
            if isinstance(node, dict):
                if 'Ref' in node: self.assertIn(node['Ref'], known)
                if 'Fn::GetAtt' in node: self.assertIn(node['Fn::GetAtt'][0], known)
                if isinstance(node.get('Fn::Sub'), str):
                    for name in re.findall(r'\$\{([^}.]+)(?:\.[^}]+)?\}', node['Fn::Sub']): self.assertIn(name, known)
                for child in node.values(): check(child)
            elif isinstance(node, list):
                for child in node: check(child)
        check(template)

    def test_api_cannot_read_owner_secret_and_migration_bootstraps_runtime_login(self):
        template = json.loads((runner.ROOT / 'academy-fargate.template.json').read_text())
        resources = template['Resources']
        api = resources['TaskDefinition']['Properties']
        migration = resources['MigrationTaskDefinition']['Properties']
        self.assertEqual(api['ExecutionRoleArn'], {'Fn::GetAtt': ['ExecutionRole', 'Arn']})
        self.assertEqual(migration['ExecutionRoleArn'], {'Fn::GetAtt': ['MigrationExecutionRole', 'Arn']})
        api_secrets = {item['Name']: item['ValueFrom'] for item in api['ContainerDefinitions'][0]['Secrets']}
        migration_secrets = {item['Name']: item['ValueFrom'] for item in migration['ContainerDefinitions'][0]['Secrets']}
        for field, key in [('DB_USERNAME', 'username'), ('DB_PASSWORD', 'password')]:
            self.assertEqual(api_secrets[field], {'Fn::Sub': '${DatabaseRuntimeSecret}:' + key + '::'})
            self.assertEqual(migration_secrets[field], {'Fn::Sub': '${DatabaseSecret}:' + key + '::'})
            self.assertEqual(migration_secrets['ACADEMY_RUNTIME_' + field], api_secrets[field])
        grants = resources['ExecutionRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        readable = next(item['Resource'] for item in grants if item['Action'] == ['secretsmanager:GetSecretValue'])
        self.assertIn({'Ref': 'DatabaseRuntimeSecret'}, readable)
        self.assertNotIn({'Ref': 'DatabaseSecret'}, readable)
        self.assertEqual(resources['DatabaseRuntimeSecret']['DeletionPolicy'], 'Retain')
        self.assertEqual(resources['DatabaseRuntimeSecret']['UpdateReplacePolicy'], 'Retain')
        migration_role = resources['MigrationExecutionRole']['Properties']
        self.assertEqual(migration_role['PermissionsBoundary'], {'Ref': 'PermissionsBoundaryArn'})
        self.assertEqual(migration_role['RoleName'], {'Fn::Sub': '${ResourceName}-migrate-execution'})
        self.assertEqual(template['Outputs']['MigrationExecutionRoleArn']['Value'], migration['ExecutionRoleArn'])

    def runtime(self, exit_code):
        value = configuration()
        snap = snapshot(value, 'release')
        snap.update(archetype='amplify-academy-fargate', platform=cloud_aws.configuration(value))
        ecs = Mock()
        ecs.run_task.return_value = {'tasks': [{'taskArn': 'migration-task'}]}
        ecs.describe_tasks.return_value = {'tasks': [{'containers': [{'exitCode': exit_code}]}]}
        ecs.describe_services.return_value = {'services': [{'runningCount': 1, 'desiredCount': 1}]}
        obj = runner.Runner(snap, 'a' * 64, 'results/test.json', client_factory=lambda _: ecs)
        obj.runtime_outputs = {name: name for name in ['Cluster', 'MigrationTaskDefinition', 'PrivateSubnet1', 'PrivateSubnet2', 'TaskSecurityGroup', 'Service']}
        obj.provision = Mock()
        return obj, ecs

    def test_migration_success_precedes_only_api_activation(self):
        obj, ecs = self.runtime(0)
        obj.migrate_and_activate(manifest(configuration()), Path('.'))
        obj.provision.assert_called_once()
        self.assertTrue(obj.provision.call_args.kwargs['activate'])
        self.assertEqual(ecs.describe_services.call_args.kwargs['services'], ['Service'])
        self.assertEqual(obj.steps, ['database-migrated', 'services-healthy'])

    def test_migration_failure_keeps_api_stopped(self):
        obj, _ = self.runtime(1)
        with self.assertRaises(runner.RunnerError):
            obj.migrate_and_activate(manifest(configuration()), Path('.'))
        obj.provision.assert_not_called()

    def test_exact_repository_ci_policy_is_owned_by_core(self):
        matches = [p for p in POLICIES if p.repository == 'NorthCountryEngineer/NCAcademy']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].workflow, '.github/workflows/ci.yml')
        self.assertEqual(matches[0].required_job, 'NCAcademy required checks')


class AcademyRegistrationTests(TestCase):
    def test_simulated_registration_uses_academy_permissions_without_provider_calls(self):
        from unittest.mock import patch
        from identity.access import Principal
        from identity.models import Organization, Membership, User
        from platform_services.cloud_models import CloudPlatform, CloudRelease
        from platform_services import cloud_deployments
        from platform_services.environments import create_environment
        organization = Organization.objects.create(name='Academy fixture', slug='academy-fixture')
        actor = User.objects.create_user('academy-fixture@example.test', 'local-test-only')
        Membership.objects.create(organization=organization, user=actor, kind='administrator')
        environment = create_environment(organization, name='development', namespace='academy-dev', application_origin='https://academy.example.test')
        platform = CloudPlatform.objects.create(organization=organization, name='Academy preview', mode='demo',
            allowed_repositories=['NorthCountryEngineer/NCAcademy'],
            config={'environment_ids': [str(environment.pk)], 'archetypes': ['amplify-academy-fargate']})
        principal = Principal(user=actor)
        platform = cloud_deployments.verify_platform(platform, principal)
        app = cloud_deployments.create_application(organization, principal, platform=platform, environment=environment,
            name='NCAcademy', slug='ncacademy', repository='NorthCountryEngineer/NCAcademy', branch='main',
            commit='a' * 40, archetype='amplify-academy-fargate', public_origin='https://academy.example.test')
        release = cloud_deployments.prepare_release(app, principal)
        self.assertIn('Academy API and private PostgreSQL', json.dumps(release.steps))
        cloud_deployments.approve_release(release, principal)
        with patch('platform_services.cloud_aws.start_run', side_effect=AssertionError('No AWS calls')):
            self.assertTrue(cloud_deployments.process_one())
            CloudRelease.objects.filter(pk=release.pk).update(next_poll=None)
            self.assertTrue(cloud_deployments.process_one())
        release.refresh_from_db()
        self.assertEqual(release.status, 'succeeded', release.error)
        app.refresh_from_db()
        self.assertEqual({p['codename'] for p in app.installation.manifest['permissions']},
                         {'academy-use', 'academy-teach', 'academy-audit'})
        deployment = app.installation.deployments.get(target='external')
        self.assertEqual(deployment.application.redirect_uris, 'https://academy.example.test/api/auth/core/callback')
        self.assertEqual(deployment.resource_application.client_type, 'confidential')
        self.assertNotIn('client_secret', json.dumps(release.outputs) + json.dumps(release.provider_state))
