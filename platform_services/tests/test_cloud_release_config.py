"""No-cloud checks of release preparation and AWS platform isolation constraints."""
import copy
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from identity.models import Organization
from platform_services.cloud_models import CloudPlatform


HELPER_PATH = Path(settings.BASE_DIR) / 'infra/aws/prepare-cloud-console-release.py'
HELPER_SPEC = importlib.util.spec_from_file_location('cloud_release_config_helper', HELPER_PATH)
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)
ACCOUNT = '015454941623'
IMAGE = f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/core@sha256:' + 'b' * 64
PREVIOUS_IMAGE = f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/core@sha256:' + 'a' * 64


def stable_stack(status='UPDATE_COMPLETE'):
    return {
        'StackId': f'arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/nce-core-production/example-id',
        'StackStatus': status,
        'Parameters': [
            {'ParameterKey': 'CoreImageUri', 'ParameterValue': PREVIOUS_IMAGE},
            {'ParameterKey': 'DesiredCount', 'ParameterValue': '1'},
            {'ParameterKey': 'EnableCloudDeployments', 'ParameterValue': '0'},
            {'ParameterKey': 'CoreIssuer', 'ParameterValue': 'https://core.example.test'},
            {'ParameterKey': 'RuntimeSecretArn', 'ParameterValue': 'redacted-reference-only'},
            {'ParameterKey': 'DatabasePassword', 'ParameterValue': '****'},
            {'ParameterKey': 'FutureParameter', 'ParameterValue': 'preserve-this-without-copying'},
        ],
    }


def parameter_map(document):
    return {row['ParameterKey']: row for row in document}


class CloudReleasePreparationTests(SimpleTestCase):
    def test_update_stops_web_then_restores_one_task_using_same_immutable_image(self):
        documents = HELPER.prepare(stable_stack(), IMAGE, ACCOUNT, '1')
        self.assertEqual(set(documents), {'stopped', 'running', 'recovery'})
        stopped, running = parameter_map(documents['stopped']), parameter_map(documents['running'])
        self.assertEqual(stopped['DesiredCount']['ParameterValue'], '0')
        self.assertEqual(running['DesiredCount']['ParameterValue'], '1')
        for parameters in [stopped, running]:
            self.assertEqual(parameters['CoreImageUri']['ParameterValue'], IMAGE)
            self.assertEqual(parameters['EnableCloudDeployments']['ParameterValue'], '1')

    def test_every_non_release_parameter_is_preserved_without_copying_values_or_secrets(self):
        stack = stable_stack()
        documents = HELPER.prepare(stack, IMAGE, ACCOUNT, '0')
        preserved = {'CoreIssuer', 'RuntimeSecretArn', 'DatabasePassword', 'FutureParameter'}
        for name, document in documents.items():
            with self.subTest(name=name):
                parameters = parameter_map(document)
                self.assertEqual(len(parameters), len(document))
                self.assertEqual(set(parameters), {row['ParameterKey'] for row in stack['Parameters']})
                for key in preserved:
                    self.assertEqual(parameters[key], {'ParameterKey': key, 'UsePreviousValue': True})
                rendered = json.dumps(document)
                self.assertNotIn('redacted-reference-only', rendered)
                self.assertNotIn('preserve-this-without-copying', rendered)
                self.assertNotIn('****', rendered)

    def test_recovery_uses_original_image_and_always_disables_live_cloud_execution(self):
        for enabled in ['0', '1']:
            with self.subTest(enabled=enabled):
                recovery = parameter_map(HELPER.prepare(stable_stack(), IMAGE, ACCOUNT, enabled)['recovery'])
                self.assertEqual(recovery['CoreImageUri']['ParameterValue'], PREVIOUS_IMAGE)
                self.assertEqual(recovery['DesiredCount']['ParameterValue'], '1')
                self.assertEqual(recovery['EnableCloudDeployments']['ParameterValue'], '0')

    def test_prepare_does_not_mutate_stack_metadata(self):
        stack = stable_stack()
        original = copy.deepcopy(stack)
        HELPER.prepare(stack, IMAGE, ACCOUNT, '1')
        self.assertEqual(stack, original)

    def test_only_stable_stack_states_are_accepted(self):
        for status in ['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']:
            with self.subTest(status=status):
                self.assertEqual(len(HELPER.prepare(stable_stack(status), IMAGE, ACCOUNT, '0')), 3)
        for status in ['CREATE_IN_PROGRESS', 'UPDATE_IN_PROGRESS', 'UPDATE_ROLLBACK_IN_PROGRESS', 'ROLLBACK_COMPLETE', 'DELETE_COMPLETE', 'UPDATE_FAILED', 'IMPORT_COMPLETE', None]:
            with self.subTest(status=status):
                with self.assertRaisesMessage(ValueError, 'must be stable'):
                    HELPER.prepare(stable_stack(status), IMAGE, ACCOUNT, '0')

    def test_stack_must_match_expected_account_and_region(self):
        for stack_id in [f'arn:aws:cloudformation:us-west-2:{ACCOUNT}:stack/core/id', 'arn:aws:cloudformation:us-east-1:221082208745:stack/core/id', 'nce-core-production', '']:
            with self.subTest(stack_id=stack_id):
                stack = stable_stack()
                stack['StackId'] = stack_id
                with self.assertRaisesMessage(ValueError, 'expected account in us-east-1'):
                    HELPER.prepare(stack, IMAGE, ACCOUNT, '0')

    def test_existing_service_must_have_one_desired_task_and_original_image(self):
        for count in ['0', '2', '', None]:
            with self.subTest(count=count):
                stack = stable_stack()
                next(row for row in stack['Parameters'] if row['ParameterKey'] == 'DesiredCount')['ParameterValue'] = count
                with self.assertRaisesMessage(ValueError, 'one running Core service'):
                    HELPER.prepare(stack, IMAGE, ACCOUNT, '0')
        for missing in ['DesiredCount', 'CoreImageUri']:
            with self.subTest(missing=missing):
                stack = stable_stack()
                stack['Parameters'] = [row for row in stack['Parameters'] if row['ParameterKey'] != missing]
                with self.assertRaisesMessage(ValueError, 'one running Core service'):
                    HELPER.prepare(stack, IMAGE, ACCOUNT, '0')
        stack = stable_stack()
        next(row for row in stack['Parameters'] if row['ParameterKey'] == 'CoreImageUri')['ParameterValue'] = ''
        with self.assertRaises(ValueError):
            HELPER.prepare(stack, IMAGE, ACCOUNT, '0')

    def test_tag_only_wrong_account_wrong_region_and_bad_digest_images_are_rejected(self):
        for image in [
            f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/core:latest',
            '221082208745.dkr.ecr.us-east-1.amazonaws.com/core@sha256:' + 'b' * 64,
            f'{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/core@sha256:' + 'b' * 64,
            f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/core@sha256:' + 'b' * 63,
            f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/core@sha256:' + 'z' * 64,
            'docker.io/example/core@sha256:' + 'b' * 64,
            IMAGE + '\n',
        ]:
            with self.subTest(image=image):
                with self.assertRaisesMessage(ValueError, 'immutable ECR image digest'):
                    HELPER.prepare(stable_stack(), image, ACCOUNT, '0')

    def test_new_enable_parameter_is_added_to_legacy_stack_without_losing_other_parameters(self):
        stack = stable_stack()
        stack['Parameters'] = [row for row in stack['Parameters'] if row['ParameterKey'] != 'EnableCloudDeployments']
        documents = HELPER.prepare(stack, IMAGE, ACCOUNT, '0')
        for document in documents.values():
            parameters = parameter_map(document)
            self.assertEqual(parameters['EnableCloudDeployments']['ParameterValue'], '0')
            self.assertEqual(parameters['CoreIssuer'], {'ParameterKey': 'CoreIssuer', 'UsePreviousValue': True})

    def test_cli_writes_only_local_private_parameter_files_with_live_disabled_by_default(self):
        with tempfile.TemporaryDirectory(prefix='core release preparation ') as directory:
            root = Path(directory)
            source = root / 'stack metadata.json'
            source.write_text(json.dumps({'Stacks': [stable_stack()]}))
            target = root / 'prepared updates'
            result = subprocess.run([sys.executable, str(HELPER_PATH), '--stack-file', str(source), '--image', IMAGE, '--account', ACCOUNT, '--output-directory', str(target)], text=True, capture_output=True, check=True)
            self.assertIn('No AWS calls were made', result.stdout)
            self.assertEqual(set(path.name for path in target.iterdir()), {'core-cloud-stopped.json', 'core-cloud-running.json', 'core-cloud-recovery.json'})
            for path in target.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                parameters = parameter_map(json.loads(path.read_text()))
                self.assertEqual(parameters['EnableCloudDeployments']['ParameterValue'], '0')

    def test_cli_rejects_unsupported_live_gate_without_creating_output_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'stack.json'
            source.write_text(json.dumps({'Stacks': [stable_stack()]}))
            target = root / 'prepared'
            result = subprocess.run([sys.executable, str(HELPER_PATH), '--stack-file', str(source), '--image', IMAGE, '--enable-live', 'yes', '--output-directory', str(target)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())


class AWSPlatformIsolationConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name='Primary AWS owner', slug='release-config-primary')
        cls.other = Organization.objects.create(name='Other AWS owner', slug='release-config-other')
        cls.base = {'name': 'AWS platform', 'mode': 'aws', 'account_id': ACCOUNT, 'region': 'us-east-1', 'prefix': 'shared', 'role_arn': f'arn:aws:iam::{ACCOUNT}:role/primary-control', 'allowed_repositories': ['Example/app'], 'config': {}}
        # These are deliberately unverified rows: SQL constraints must protect
        # the boundary even if a caller bypasses model form validation.
        CloudPlatform.objects.create(organization=cls.organization, **cls.base)

    def test_aws_resource_prefix_cannot_be_shared_across_organizations_in_same_account_region(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CloudPlatform.objects.create(organization=self.other, **(self.base | {'role_arn': f'arn:aws:iam::{ACCOUNT}:role/other-control'}))

    def test_aws_control_role_cannot_be_shared_across_organizations_even_with_another_prefix(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CloudPlatform.objects.create(organization=self.other, **(self.base | {'prefix': 'distinct'}))

    def test_different_region_with_distinct_control_role_can_use_same_resource_prefix(self):
        platform = CloudPlatform.objects.create(organization=self.other, **(self.base | {'region': 'us-west-2', 'role_arn': f'arn:aws:iam::{ACCOUNT}:role/west-control'}))
        self.assertEqual(platform.prefix, 'shared')
        self.assertEqual(CloudPlatform.objects.filter(mode='aws').count(), 2)

    def test_demo_platforms_do_not_reserve_real_aws_resource_prefixes_or_control_roles(self):
        for organization in [self.organization, self.other]:
            CloudPlatform.objects.create(organization=organization, **(self.base | {'mode': 'demo'}))
        self.assertEqual(CloudPlatform.objects.filter(mode='demo').count(), 2)
