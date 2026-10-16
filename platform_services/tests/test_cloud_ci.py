"""Deployment CI admission and encrypted-credential tests; all external calls mocked."""
import base64
import json
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from identity.access import Principal
from identity.models import Membership, Organization, User
from platform_services import cloud_aws, cloud_deployments
from platform_services.cloud_models import CloudPlatform, CloudRelease
from platform_services.environments import create_environment
from platform_services.github_ci import CIGateError
from .test_cloud_aws import platform, snapshot


class AWSAdmissionTests(SimpleTestCase):
    def test_ci_failure_precedes_aws_session_clients_and_billable_start(self):
        value = platform()
        with patch('platform_services.github_ci._read_json', side_effect=CIGateError('CI has not passed')) as get, \
             patch.object(cloud_aws, '_clients') as clients:
            with self.assertRaisesMessage(CIGateError, 'CI has not passed'):
                cloud_aws.start_run(value, snapshot(value), 'test-release')
            clients.assert_not_called()
            get.assert_called_once()

    def test_current_server_token_is_used_but_never_forwarded_to_aws(self):
        value = platform()
        value.ci_github_token = 'github_pat_' + 'x' * 30
        clients = {'codebuild': Mock()}
        clients['codebuild'].start_build.return_value = {'build': {'id': 'runner:build'}}
        snap = snapshot(value)
        snap['ci_github_token'] = 'attacker-supplied-token'
        with patch.object(cloud_aws, 'verify_deployment', return_value=[{'commit': 'a' * 40}]) as verify:
            cloud_aws.start_run(value, snap, 'test-release', client_factory=clients.__getitem__)
        verify.assert_called_once_with(value.allowed_repositories[0], 'a' * 40,
            'NorthCountryEngineer/NCE_Core', 'b' * 40, token=value.ci_github_token)
        arguments = clients['codebuild'].start_build.call_args.kwargs
        encoded = next(row['value'] for row in arguments['environmentVariablesOverride'] if row['name'] == 'NCE_DEPLOYMENT')
        self.assertNotIn(value.ci_github_token, base64.b64decode(encoded).decode())
        self.assertNotIn('attacker-supplied-token', json.dumps(arguments))


@override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=True)
class CloudCIApprovalTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='CI fixture', slug='ci-fixture')
        self.admin = User.objects.create_user('ci-fixture@example.test', 'local-ci-test-only')
        Membership.objects.create(organization=self.organization, user=self.admin, kind='administrator')
        self.environment = create_environment(self.organization, name='development', namespace='ci-dev', application_origin='https://ci.example.test')
        fixture = platform()
        fixture.config['environment_ids'] = [str(self.environment.pk)]
        self.platform = CloudPlatform(organization=self.organization, name='CI gate', mode='aws',
            **{key: getattr(fixture, key) for key in ['account_id', 'region', 'prefix', 'role_arn', 'runner_project', 'connection_arn', 'allowed_repositories', 'config']})
        self.platform.external_id = fixture.external_id
        self.platform.ci_github_token = 'github_pat_' + 'z' * 30
        self.platform.save()
        with patch.object(cloud_aws, 'verify', return_value={}):
            self.platform = cloud_deployments.verify_platform(self.platform, Principal(user=self.admin))
        self.app = cloud_deployments.create_application(self.organization, Principal(user=self.admin), platform=self.platform,
            environment=self.environment, name='CI app', slug='ci-app', repository=fixture.allowed_repositories[0],
            branch='main', commit='a' * 40, archetype='amplify-static')

    def release(self):
        with patch.object(cloud_aws, 'verify_deployment', return_value=[]):
            return cloud_deployments.prepare_release(self.app, Principal(user=self.admin))

    def test_prepare_refuses_unverified_commit_without_creating_plan_or_calling_aws(self):
        with patch('platform_services.github_ci._read_json', side_effect=CIGateError('latest CI failed')), \
             patch.object(cloud_aws, '_clients') as clients:
            with self.assertRaisesMessage(ValidationError, 'latest CI failed'):
                cloud_deployments.prepare_release(self.app, Principal(user=self.admin))
            clients.assert_not_called()
        self.assertFalse(CloudRelease.objects.exists())

    def test_approval_rechecks_ci_after_successful_prepare_and_keeps_draft_if_rerun_failed(self):
        release = self.release()
        with patch('platform_services.github_ci._read_json', side_effect=CIGateError('latest CI failed')):
            with self.assertRaisesMessage(ValidationError, 'latest CI failed'):
                cloud_deployments.approve_release(release, Principal(user=self.admin), confirm_costs=True)
        release.refresh_from_db()
        self.assertEqual(release.status, 'draft')
        self.assertIsNone(release.approved_at)

    def test_worker_rechecks_ci_and_does_not_recover_or_start_an_unstarted_build(self):
        release = self.release()
        with patch.object(cloud_aws, 'verify_deployment', return_value=[]):
            cloud_deployments.approve_release(release, Principal(user=self.admin), confirm_costs=True)
        with patch('platform_services.github_ci._read_json', side_effect=CIGateError('latest CI failed')), \
             patch.object(cloud_aws, '_clients') as clients, patch.object(cloud_aws, 'recover_run') as recover:
            self.assertTrue(cloud_deployments.process_one())
            clients.assert_not_called()
            recover.assert_not_called()
        release.refresh_from_db()
        self.assertEqual(release.status, 'failed')
        self.assertIn('latest CI failed', release.error)
        self.assertNotIn('starting', release.provider_state)
        self.assertNotIn('build_id', release.provider_state)

    def test_token_is_encrypted_and_absent_from_review_snapshot_and_plan(self):
        release = self.release()
        token = self.platform.ci_github_token
        self.assertNotIn(token, self.platform.encrypted_ci_github_token)
        self.assertNotIn(token, json.dumps(release.snapshot))
        self.assertNotIn(self.platform.encrypted_ci_github_token, json.dumps(release.snapshot))
        self.assertEqual(CloudPlatform.objects.get(pk=self.platform.pk).ci_github_token, token)
        self.platform.ci_github_token = 'github_pat_' + 'y' * 30
        self.platform.save()
        self.assertFalse(self.platform.active)
        with self.assertRaises(ValidationError):
            cloud_deployments.approve_release(release, Principal(user=self.admin), confirm_costs=True)
