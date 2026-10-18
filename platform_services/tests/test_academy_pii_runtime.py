"""Offline tests of the operator-owned Academy PII runtime boundary."""
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from django.test import SimpleTestCase

from platform_services import cloud_aws
from .test_academy_fargate import configuration, manifest
from .test_cloud_aws import runner, snapshot


PII_ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nce-tests-academy-pii-AbC123'  # pragma: allowlist secret -- synthetic ARN only


def selected(node, enabled):
    """Resolve only the PII condition to inspect the actual injected boundary."""
    if isinstance(node, dict):
        if 'Fn::If' in node and node['Fn::If'][0] == 'HasFamilyPiiSecret':
            return selected(node['Fn::If'][1 if enabled else 2], enabled)
        return {key: selected(value, enabled) for key, value in node.items()}
    if isinstance(node, list):
        return [value for item in node if (value := selected(item, enabled)) != {'Ref': 'AWS::NoValue'}]
    return node


class AcademyPiiRuntimeTests(SimpleTestCase):
    def template(self):
        return json.loads((runner.ROOT / 'academy-fargate.template.json').read_text())

    def parameters(self, *, existing=False, previous=None, action='release', activate=False, document=None):
        value = configuration()
        snap = snapshot(value, action)
        snap.update(archetype='amplify-academy-fargate', secret_parameter='/nce-tests/apps/app/core-resource-client')
        result = runner.stack_parameters(cloud_aws.configuration(value), snap, document or manifest(value), '',
                                        existing=existing, activate=activate, previous_parameters=previous)
        return {row['ParameterKey']: row for row in result}

    def test_new_and_legacy_stacks_default_off_without_secret_lookup(self):
        template = self.template()
        self.assertEqual(template['Parameters']['PiiSecretArn']['Default'], '')
        self.assertEqual(template['Conditions']['HasFamilyPiiSecret'],
                         {'Fn::Not': [{'Fn::Equals': [{'Ref': 'PiiSecretArn'}, '']}]})
        for existing in [False, True]:
            self.assertEqual(self.parameters(existing=existing)['PiiSecretArn'],
                             {'ParameterKey': 'PiiSecretArn', 'ParameterValue': ''})
        off = selected(template, False)['Resources']
        self.assertNotIn('PII_ENCRYPTION_KEY', json.dumps(off['TaskDefinition']['Properties']['ContainerDefinitions']))
        self.assertNotIn('family-pii', json.dumps(off['ExecutionRole']['Properties']['Policies']))

    def test_enabled_key_and_exact_read_grant_are_api_only(self):
        resources = selected(self.template(), True)['Resources']
        api = resources['TaskDefinition']['Properties']
        container = api['ContainerDefinitions'][0]
        pii = [row for row in container['Secrets'] if row.get('Name') == 'PII_ENCRYPTION_KEY']
        self.assertEqual(pii, [{'Name': 'PII_ENCRYPTION_KEY', 'ValueFrom': {'Ref': 'PiiSecretArn'}}])
        self.assertNotIn('PII_ENCRYPTION_KEY', json.dumps(container['Environment']))
        self.assertNotIn('TaskRoleArn', api)
        policy = next(row for row in resources['ExecutionRole']['Properties']['Policies'] if row.get('PolicyName') == 'family-pii')
        self.assertEqual(policy['PolicyDocument']['Statement'], [{
            'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': {'Ref': 'PiiSecretArn'},
            'Condition': {'StringEquals': {'aws:ResourceAccount': {'Ref': 'AWS::AccountId'},
                                           'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}}])
        for name, resource in resources.items():
            if name not in {'TaskDefinition', 'ExecutionRole'}:
                self.assertNotIn('PiiSecretArn', json.dumps(resource), name)
                self.assertNotIn('PII_ENCRYPTION_KEY', json.dumps(resource), name)
        self.assertEqual(resources['MigrationTaskDefinition']['Properties']['ExecutionRoleArn'],
                         {'Fn::GetAtt': ['MigrationExecutionRole', 'Arn']})

    def test_shared_runtime_secret_cannot_be_reused_for_pii(self):
        rule = self.template()['Rules']['FamilyPiiSeparateFromSharedRuntime']['Assertions'][0]['Assert']
        self.assertEqual(rule, {'Fn::Not': [{'Fn::Equals': [{'Ref': 'PiiSecretArn'}, {'Ref': 'RuntimeSecretArn'}]}]})
        shared = configuration().config['academy_fargate_apps']['NorthCountryEngineer/NCAcademy']['runtime_secret_arn']
        with self.assertRaises(runner.RunnerError):
            self.parameters(existing=True, previous=[{'ParameterKey': 'PiiSecretArn', 'ParameterValue': shared}])

    def test_operator_binding_survives_provision_release_and_activation(self):
        for value in ['', PII_ARN]:
            for action in ['provision', 'release']:
                for activate in [False, True]:
                    previous = [{'ParameterKey': 'PiiSecretArn', 'ParameterValue': value}]
                    parameters = self.parameters(existing=True, previous=previous, action=action, activate=activate)
                    self.assertEqual(parameters['PiiSecretArn'], {'ParameterKey': 'PiiSecretArn', 'UsePreviousValue': True})
                    if action == 'provision':
                        self.assertEqual(parameters['DesiredCount'], {'ParameterKey': 'DesiredCount', 'UsePreviousValue': True})
                        self.assertEqual(parameters['ApiImageUri'], {'ParameterKey': 'ApiImageUri', 'UsePreviousValue': True})
                    else:
                        self.assertEqual(parameters['DesiredCount']['ParameterValue'], '1' if activate else '0')

    def test_provision_uses_the_provider_binding_before_planning_cloud_changes(self):
        value = configuration()
        snap = snapshot(value)
        snap.update(archetype='amplify-academy-fargate', secret_parameter='/nce-tests/apps/app/core-resource-client',
                    platform=cloud_aws.configuration(value))
        client = Mock()
        client.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE',
            'Parameters': [{'ParameterKey': 'PiiSecretArn', 'ParameterValue': PII_ARN}],
            'Outputs': [{'OutputKey': 'AmplifyAppId', 'OutputValue': 'dtest'},
                        {'OutputKey': 'Origin', 'OutputValue': 'https://production.dtest.amplifyapp.com'}]}]}
        target = runner.Runner(snap, 'c' * 64, 'runs/test/result.json', client_factory=lambda _: client)
        target.wait_change_set = Mock(return_value=[])
        target.provision(manifest(value), Path('.'))
        parameters = {row['ParameterKey']: row for row in client.create_change_set.call_args.kwargs['Parameters']}
        self.assertEqual(parameters['PiiSecretArn'], {'ParameterKey': 'PiiSecretArn', 'UsePreviousValue': True})
        self.assertEqual(parameters['DesiredCount'], {'ParameterKey': 'DesiredCount', 'UsePreviousValue': True})
        client.reset_mock()
        client.describe_stacks.return_value['Stacks'][0]['Parameters'][0]['ParameterValue'] = PII_ARN.replace('us-east-1', 'us-west-2')
        with self.assertRaises(runner.RunnerError):
            target.provision(manifest(value), Path('.'))
        client.create_change_set.assert_not_called()

    def test_bad_existing_binding_fails_before_a_cloud_change(self):
        for invalid in [None, 3, PII_ARN.replace('123456789012', '999999999999'),
                        PII_ARN.replace('us-east-1', 'us-west-2'), PII_ARN + ':key::',
                        PII_ARN + ':version', PII_ARN.replace('arn:aws:', 'arn:aws-cn:'),
                        PII_ARN.replace('AbC123', '*'), PII_ARN.replace(':secret:', ':parameter:')]:
            with self.subTest(invalid=invalid), self.assertRaises(runner.RunnerError):
                self.parameters(existing=True, previous=[{'ParameterKey': 'PiiSecretArn', 'ParameterValue': invalid}])

    def test_application_manifest_cannot_select_or_override_the_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'package.json').write_text(json.dumps({'scripts': {'build': 'vite build'}}))
            (root / 'package-lock.json').write_text('{}')
            for extra in [{'PiiSecretArn': PII_ARN}, {'environment': {'PII_ENCRYPTION_KEY': 'never-accepted'}},
                          {'secrets': {'PII_ENCRYPTION_KEY': PII_ARN}}]:
                document = manifest(configuration()) | extra
                (root / 'nce-deploy.json').write_text(json.dumps(document))
                with self.assertRaises(runner.RunnerError):
                    runner.read_manifest(root, 'amplify-academy-fargate')
            document = manifest(configuration())
            document['backend']['pii_secret_arn'] = PII_ARN
            (root / 'nce-deploy.json').write_text(json.dumps(document))
            with self.assertRaises(runner.RunnerError):
                runner.read_manifest(root, 'amplify-academy-fargate')
        value = configuration()
        value.config['academy_fargate_apps']['NorthCountryEngineer/NCAcademy']['pii_secret_arn'] = PII_ARN
        with self.assertRaises(cloud_aws.AWSAdapterError):
            cloud_aws.configuration(value)
