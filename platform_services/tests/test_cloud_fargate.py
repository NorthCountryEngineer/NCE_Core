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
    def test_optional_frontend_binding_defaults_disabled_and_rejects_wildcards(self):
        import re
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        parameters = onboarding['Parameters']
        app = parameters['FrontendAppId']
        branch = parameters['FrontendBranch']
        self.assertEqual(app['Default'], '')
        self.assertEqual(branch['Default'], 'production')
        condition = onboarding['Conditions']['HasBoundFrontendApp']
        self.assertEqual(condition, {'Fn::Not': [{'Fn::Equals': [{'Ref': 'FrontendAppId'}, '']}]})

        def resolve(value, values):
            if isinstance(value, dict):
                if 'Ref' in value:
                    return values[value['Ref']]
                if 'Fn::Equals' in value:
                    return resolve(value['Fn::Equals'][0], values) == resolve(value['Fn::Equals'][1], values)
                if 'Fn::Not' in value:
                    return not resolve(value['Fn::Not'][0], values)
                self.fail('Unexpected frontend binding condition')
            return value

        self.assertFalse(resolve(condition, {'FrontendAppId': app['Default']}))
        for value in ['', 'd1', 'dapproved123', 'd' + 'a' * 40]:
            with self.subTest(valid_app=value):
                self.assertIsNotNone(re.fullmatch(app['AllowedPattern'], value))
                self.assertEqual(resolve(condition, {'FrontendAppId': value}), bool(value))
        for value in ['*', 'd*', 'd?', 'Dabc', 'abc', 'dabc/branches/production', 'd' + 'a' * 41, '${AppId}']:
            with self.subTest(invalid_app=value):
                self.assertIsNone(re.fullmatch(app['AllowedPattern'], value))
        for value in ['production', 'Release-1.2_candidate', 'a' * 255]:
            self.assertIsNotNone(re.fullmatch(branch['AllowedPattern'], value))
        for value in ['', '*', 'prod?', 'branch/production', 'a' * 256, '${Branch}', 'prod\n']:
            with self.subTest(invalid_branch=value):
                self.assertIsNone(re.fullmatch(branch['AllowedPattern'], value))

    def test_optional_frontend_publish_grant_selects_one_runner_app_branch_and_region(self):
        import fnmatch
        import re
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        resource = onboarding['Resources']['BoundFrontendPublishPolicy']
        self.assertEqual(resource['Condition'], 'HasBoundFrontendApp')
        self.assertEqual(resource['Properties']['Roles'], [{'Ref': 'RunnerRole'}])
        statements = resource['Properties']['PolicyDocument']['Statement']
        self.assertEqual(len(statements), 1)
        grant = statements[0]
        self.assertEqual(grant['Effect'], 'Allow')
        self.assertEqual(set(grant['Action']), {'amplify:CreateDeployment', 'amplify:StartDeployment'})
        self.assertEqual(grant['Condition'], {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}})
        values = {'AWS::Partition': 'aws', 'AWS::Region': 'us-east-1', 'AWS::AccountId': '123456789012',
                  'FrontendAppId': 'dapproved123', 'FrontendBranch': 'production'}
        resources = [re.sub(r'\$\{([^}]+)\}', lambda match: values[match[1]], item['Fn::Sub'])
                     for item in grant['Resource']]
        arn = resources[0]
        self.assertEqual(arn, 'arn:aws:amplify:us-east-1:123456789012:apps/dapproved123/branches/production')
        self.assertNotIn('*', arn)
        self.assertNotIn('?', arn)
        self.assertEqual(resources, [arn, arn + '/*'])

        # Evaluate this optional grant independently of the existing tag grant:
        # live publishing needs descendants, but tags cannot expand the owner
        # binding into another app, branch, account, region or action.
        def admitted(action, requested_arn, region):
            return (action in grant['Action'] and region == values['AWS::Region']
                    and any(fnmatch.fnmatchcase(requested_arn, resource) for resource in resources))

        for action in grant['Action']:
            for selected in [arn, arn + '/jobs/1', arn + '/jobs/123456789', arn + '/']:
                with self.subTest(action=action, selected_resource=selected):
                    self.assertTrue(admitted(action, selected, 'us-east-1'))
                    self.assertFalse(admitted(action, selected, 'us-west-2'))
            for other in [arn.replace('dapproved123', 'danother123'), arn.replace('/production', '/preview'),
                          arn.replace('/production', '/preview') + '/jobs/1',
                          arn.replace('/production', '/production-other') + '/jobs/1',
                          arn.replace('dapproved123', 'danother123') + '/jobs/1',
                          arn.replace('123456789012', '999999999999'), arn.replace('us-east-1', 'us-west-2'),
                          arn.rsplit('/branches/', 1)[0], arn.rsplit('/branches/', 1)[0] + '/branches/preview']:
                with self.subTest(action=action, unrelated_resource=other):
                    self.assertFalse(admitted(action, other, 'us-east-1'))
        for action in ['amplify:GetApp', 'amplify:UpdateApp', 'amplify:StartJob', 'amplify:DeleteBranch',
                       'iam:PassRole', 'codebuild:StartBuild', 'ssm:GetParameter', 's3:GetObject']:
            for selected in [arn, arn + '/jobs/1']:
                self.assertFalse(admitted(action, selected, 'us-east-1'))
        normal = next(item for policy in onboarding['Resources']['RunnerRole']['Properties']['Policies']
                      for item in policy['PolicyDocument']['Statement'] if item.get('Sid') == 'PublishOnlyTaggedFrontends')
        self.assertEqual(normal['Condition'], {'StringEquals': {'aws:ResourceTag/nce:platform-prefix': {'Ref': 'Prefix'}}})

    def test_retained_database_keeps_its_dependency_chain(self):
        import re
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        resources = template['Resources']

        def references(node):
            if isinstance(node, dict):
                reference = node.get('Ref')
                if reference in resources:
                    yield reference
                substitution = node.get('Fn::Sub')
                if isinstance(substitution, str):
                    for reference in re.findall(r'\$\{([^}.]+)(?:\.[^}]+)?\}', substitution):
                        if reference in resources:
                            yield reference
                for child in node.values():
                    yield from references(child)
            elif isinstance(node, list):
                for child in node:
                    yield from references(child)

        # Retaining only the instance leaves rollback trying to remove network
        # and configuration resources still used by the protected database.
        retained, pending = set(), ['Database']
        while pending:
            name = pending.pop()
            if name in retained:
                continue
            retained.add(name)
            pending.extend(references(resources[name]['Properties']))
        self.assertEqual(retained, {'Database', 'DatabaseSecret', 'DatabaseSubnetGroup',
                                   'DatabaseParameters', 'DatabaseSecurityGroup',
                                   'PrivateSubnet1', 'PrivateSubnet2', 'Vpc'})
        for name in retained:
            with self.subTest(resource=name):
                self.assertEqual(resources[name]['DeletionPolicy'], 'Retain')
                self.assertEqual(resources[name]['UpdateReplacePolicy'], 'Retain')
        self.assertTrue(resources['Database']['Properties']['DeletionProtection'])

    def test_namespace_zone_creation_requires_cloud_map_forward_access(self):
        from fnmatch import fnmatchcase
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        resources = onboarding['Resources']
        namespace = resources['FargatePrivateNamespacePolicy']
        self.assertEqual(namespace['Properties']['Roles'], [{'Ref': 'FargateCloudFormationRole'}])
        statements = namespace['Properties']['PolicyDocument']['Statement']
        grant = next(statement for statement in statements if 'route53:CreateHostedZone' in statement['Action'])
        conditions = grant['Condition']

        # Evaluate the actual grant's supported conditions against positive and
        # negative AWS context. This is an offline boundary regression, not
        # evidence that a live Cloud Map call supplies a particular context.
        def admitted(context):
            for operator, entries in conditions.items():
                for key, required in entries.items():
                    value = context.get(key)
                    if operator == 'Bool':
                        if value is None or str(value).lower() != required.lower():
                            return False
                    elif operator == 'StringEquals':
                        if value != required:
                            return False
                    elif operator == 'Null':
                        if (value is None) != (required == 'true'):
                            return False
                    else:
                        self.fail('Unexpected namespace delegation condition operator')
            return True

        self.assertTrue(admitted({'aws:ViaAWSService': True, 'aws:CalledViaLast': 'servicediscovery.amazonaws.com'}))
        for context in [{}, {'aws:ViaAWSService': False}, {'aws:ViaAWSService': True},
                        {'aws:CalledViaLast': 'servicediscovery.amazonaws.com'},
                        {'aws:ViaAWSService': False, 'aws:CalledViaLast': 'servicediscovery.amazonaws.com'},
                        {'aws:ViaAWSService': True, 'aws:CalledViaLast': 'cloudformation.amazonaws.com'},
                        {'aws:ViaAWSService': True, 'aws:CalledViaLast': 's3.amazonaws.com'}]:
            with self.subTest(context=context):
                self.assertFalse(admitted(context))
        role_statements = [statement for policy in resources['FargateCloudFormationRole']['Properties']['Policies']
                           for statement in policy['PolicyDocument']['Statement']]
        role_statements.extend(statement for resource in resources.values()
                               if resource['Type'] == 'AWS::IAM::ManagedPolicy'
                               and {'Ref': 'FargateCloudFormationRole'} in resource['Properties'].get('Roles', [])
                               for statement in resource['Properties']['PolicyDocument']['Statement'])
        allowed_actions = [action for statement in role_statements if statement['Effect'] == 'Allow'
                           for action in statement['Action']]
        self.assertTrue(any(fnmatchcase('servicediscovery:CreatePrivateDnsNamespace', action) for action in allowed_actions))
        self.assertFalse(any(fnmatchcase('servicediscovery:CreatePublicDnsNamespace', action) for action in allowed_actions))

    def test_cleanup_grants_keep_supported_resource_and_region_bounds(self):
        from fnmatch import fnmatchcase
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        resources = onboarding['Resources']
        namespace = resources['FargatePrivateNamespacePolicy']['Properties']['PolicyDocument']['Statement']
        creation = next(item for item in namespace if 'route53:CreateHostedZone' in item['Action'])
        deletion = next(item for item in namespace if 'route53:DeleteHostedZone' in item['Action'])
        self.assertEqual(deletion['Condition'], creation['Condition'])
        self.assertEqual(deletion['Resource'], {'Fn::Sub': 'arn:${AWS::Partition}:route53:::hostedzone/*'})
        cleanup = resources['FargateTaskDefinitionCleanupPolicy']
        self.assertEqual(cleanup['Condition'], 'EnableFargate')
        self.assertEqual(cleanup['Properties']['Roles'], [{'Ref': 'FargateCloudFormationRole'}])
        self.assertEqual(cleanup['Properties']['PolicyDocument']['Statement'], [{
            'Effect': 'Allow', 'Action': ['ecs:DeregisterTaskDefinition'], 'Resource': '*',
            'Condition': {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}}])
        role_statements = [item for policy in resources['FargateCloudFormationRole']['Properties']['Policies']
                           for item in policy['PolicyDocument']['Statement']]
        bucket = next(item for item in role_statements if item.get('Sid') == 'AppBuckets')
        self.assertIn('s3:DeleteBucketPolicy', bucket['Action'])
        self.assertEqual(bucket['Resource'], {'Fn::Sub': 'arn:${AWS::Partition}:s3:::${Prefix}-${AWS::AccountId}-*'})
        normal_namespace = next(item for item in role_statements if any(fnmatchcase('servicediscovery:DeleteNamespace', action) for action in item['Action']))
        self.assertIn('aws:ResourceTag/nce:platform-prefix', normal_namespace['Condition']['StringEquals'])
        self.assertEqual(normal_namespace['Resource'], {'Fn::Sub': 'arn:${AWS::Partition}:servicediscovery:${AWS::Region}:${AWS::AccountId}:*/*'})

    def test_request_tag_guarded_creation_has_explicit_platform_and_application_tags(self):
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        onboarding = json.loads((runner.ROOT.parent / 'aws-platform/platform.template.json').read_text())
        policies = onboarding['Resources']['FargateCloudFormationRole']['Properties']['Policies']
        guarded_actions = {action for policy in policies for statement in policy['PolicyDocument']['Statement']
                           if 'aws:RequestTag/nce:platform-prefix' in statement.get('Condition', {}).get('StringEquals', {})
                           for action in statement['Action']}
        create_actions = {
            'AWS::Amplify::App': 'amplify:CreateApp',
            'AWS::Amplify::Branch': 'amplify:CreateBranch',
            'AWS::EC2::VPC': 'ec2:CreateVpc',
            'AWS::EC2::Subnet': 'ec2:CreateSubnet',
            'AWS::EC2::SecurityGroup': 'ec2:CreateSecurityGroup',
            'AWS::EC2::RouteTable': 'ec2:CreateRouteTable',
            'AWS::EC2::InternetGateway': 'ec2:CreateInternetGateway',
            'AWS::EC2::EIP': 'ec2:AllocateAddress',
            'AWS::EC2::NatGateway': 'ec2:CreateNatGateway',
            'AWS::EC2::VPCEndpoint': 'ec2:CreateVpcEndpoint',
            'AWS::ECS::TaskDefinition': 'ecs:RegisterTaskDefinition',
            'AWS::ServiceDiscovery::PrivateDnsNamespace': 'servicediscovery:CreatePrivateDnsNamespace',
            'AWS::ServiceDiscovery::Service': 'servicediscovery:CreateService',
        }
        self.assertLessEqual(set(create_actions.values()), guarded_actions)
        expected = {'nce:platform-prefix': {'Ref': 'PlatformPrefix'},
                    'nce:application': {'Ref': 'ApplicationIdentifier'}}
        for name, resource in template['Resources'].items():
            if resource['Type'] not in create_actions:
                continue
            with self.subTest(resource=name, create_action=create_actions[resource['Type']]):
                # CloudFormation cannot rely on inherited app/stack tags when
                # the create call itself must satisfy aws:RequestTag.
                tags = {tag['Key']: tag['Value'] for tag in resource['Properties'].get('Tags', [])}
                self.assertEqual({key: tags.get(key) for key in expected}, expected)

    def test_canonical_www_redirect_precedes_proxies_and_spa_fallback(self):
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        rules = template['Resources']['AmplifyApp']['Properties']['CustomRules']
        redirect = rules[0]
        self.assertEqual(redirect['Status'], '301')
        self.assertEqual(redirect['Target'], {'Ref': 'ApplicationOrigin'})

        def resolve(expression, origin):
            if not isinstance(expression, dict):
                return expression
            if 'Ref' in expression:
                self.assertEqual(expression['Ref'], 'ApplicationOrigin')
                return origin
            if 'Fn::Split' in expression:
                delimiter, value = expression['Fn::Split']
                return resolve(value, origin).split(delimiter)
            if 'Fn::Select' in expression:
                index, values = expression['Fn::Select']
                return resolve(values, origin)[int(index)]
            if 'Fn::Join' in expression:
                delimiter, values = expression['Fn::Join']
                return delimiter.join(resolve(value, origin) for value in values)
            self.fail('Unexpected canonical redirect expression')

        for origin in ['https://proposalstudio.ai', 'https://customer.example.test', 'https://pending.invalid']:
            with self.subTest(origin=origin):
                # Domain-only source is required: Amplify appends paths itself.
                self.assertEqual(resolve(redirect['Source'], origin), 'https://www.' + origin.split('://')[1])
        self.assertEqual([rule['Source'] for rule in rules[1:3]], ['/api/<*>', '/integrations/<*>'])
        for rule, path in zip(rules[1:3], ['/api/<*>', '/integrations/<*>']):
            self.assertEqual(rule['Status'], '200')
            self.assertEqual(rule['Target'], {'Fn::Sub': 'https://${ApiHost}' + path})
        self.assertEqual(rules[3]['Target'], '/index.html')
        self.assertEqual(rules[3]['Status'], '200')
        self.assertTrue(rules[3]['Source'].startswith('</^'))
        self.assertFalse(any(rule['Type'] == 'AWS::Amplify::Domain' for rule in template['Resources'].values()))

    def test_canonical_origin_parameter_rejects_ports_paths_and_credentials(self):
        import re
        template = json.loads((runner.ROOT / 'fargate.template.json').read_text())
        pattern = template['Parameters']['ApplicationOrigin']['AllowedPattern']
        for origin in ['https://proposalstudio.ai', 'https://customer.example.test', 'https://pending.invalid']:
            with self.subTest(origin=origin):
                self.assertIsNotNone(re.fullmatch(pattern, origin))
        for origin in ['http://proposalstudio.ai', 'https://proposalstudio.ai:443',
                       'https://proposalstudio.ai/', 'https://proposalstudio.ai/path',
                       'https://proposalstudio.ai?query=1', 'https://proposalstudio.ai#fragment',
                       'https://user@proposalstudio.ai', 'https://user:password@proposalstudio.ai']:
            with self.subTest(origin=origin):
                self.assertIsNone(re.fullmatch(pattern, origin))

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
