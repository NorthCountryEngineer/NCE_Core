"""Offline regressions for verification races and durable provider handoffs."""
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, User
from platform_services.cloud_aws import AWSAdapterError
from platform_services.cloud_deployments import approve_release, create_application, prepare_release, process_one, verify_platform
from platform_services.cloud_models import CloudPlatform, CloudRelease
from platform_services.environments import create_environment


@override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=True)
class CloudRecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name='Recovery tests', slug='cloud-recovery')
        cls.admin = User.objects.create_user('cloud-recovery@example.test', 'Local-test-password-only')
        cls.membership = Membership.objects.create(organization=cls.organization, user=cls.admin, kind='administrator')
        cls.environment = create_environment(cls.organization, name='development', namespace='cloud-recovery-dev', application_origin='https://recovery.example.test')
        cls.platform = CloudPlatform.objects.create(organization=cls.organization, name='Simulation', mode='demo', allowed_repositories=['Example/app'], config={'environment_ids': [str(cls.environment.pk)], 'archetypes': ['amplify-static']})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.admin))

    def live_release(self):
        # Exercise the worker's AWS branches with provider operations mocked;
        # SDK configuration and network verification have separate adapter tests.
        CloudPlatform.objects.filter(pk=self.platform.pk).update(mode='aws')
        self.platform.refresh_from_db()
        with patch('platform_services.cloud_deployments._ready'):
            app = create_application(self.organization, Principal(user=self.admin), platform=self.platform, environment=self.environment, name='Recovery application', slug='recovery-app', repository='Example/app', branch='main', commit='a' * 40, archetype='amplify-static')
            release = prepare_release(app, Principal(user=self.admin))
            approve_release(release, Principal(user=self.admin), confirm_costs=True)
        release.refresh_from_db()
        return release

    def test_verification_cannot_overwrite_settings_edited_during_provider_probe(self):
        CloudPlatform.objects.filter(pk=self.platform.pk).update(mode='aws', runner_project='original-runner')
        self.platform.refresh_from_db()

        def concurrent_settings_edit(platform):
            CloudPlatform.objects.filter(pk=platform.pk).update(runner_project='edited-runner', allowed_repositories=['Example/replacement'])

        with patch.object(CloudPlatform, 'full_clean'), patch('platform_services.cloud_aws.verify', side_effect=concurrent_settings_edit):
            with self.assertRaisesMessage(ValidationError, 'Platform settings changed during verification'):
                verify_platform(self.platform, Principal(user=self.admin))
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.runner_project, 'edited-runner')
        self.assertEqual(self.platform.allowed_repositories, ['Example/replacement'])
        self.assertFalse(self.platform.active)
        self.assertIsNone(self.platform.verified_at)
        self.assertEqual(self.platform.verified_digest, '')

    def test_start_intent_is_persisted_before_billable_provider_call(self):
        release = self.live_release()

        def inspect_durable_intent(platform, snapshot, key):
            recorded = CloudRelease.objects.get(pk=release.pk)
            self.assertTrue(recorded.provider_state['starting'])
            self.assertEqual(recorded.provider_state['phase'], 'provision')
            self.assertEqual(len(recorded.provider_state['run_token']), 64)
            self.assertIsNotNone(recorded.lease_owner)
            return {'build_id': 'runner:accepted-job', 'result_key': 'runs/' + 'a' * 64 + '/result.json'}

        with patch('platform_services.cloud_deployments._ready'), patch('platform_services.cloud_aws.start_run', side_effect=inspect_durable_intent) as start, patch('platform_services.cloud_aws.recover_run') as recover:
            self.assertTrue(process_one())
        release.refresh_from_db()
        start.assert_called_once()
        recover.assert_not_called()
        self.assertEqual(release.provider_state['build_id'], 'runner:accepted-job')
        self.assertNotIn('starting', release.provider_state)
        self.assertEqual(release.status, 'running')

    def test_ambiguous_start_recovers_recorded_build_without_starting_again(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'starting': True, 'phase': 'provision', 'run_token': 'b' * 64})
        with patch('platform_services.cloud_deployments._ready'), patch('platform_services.cloud_aws.recover_run', return_value={'build_id': 'runner:recovered-job', 'result_key': 'runs/' + 'b' * 64 + '/result.json'}) as recover, patch('platform_services.cloud_aws.start_run') as start, patch('platform_services.cloud_aws.poll_run') as poll:
            self.assertTrue(process_one())
        release.refresh_from_db()
        recover.assert_called_once()
        start.assert_not_called()
        poll.assert_not_called()
        self.assertEqual(release.provider_state['build_id'], 'runner:recovered-job')
        self.assertNotIn('starting', release.provider_state)
        self.assertEqual(release.status, 'running')

    def test_unresolved_start_fails_closed_without_billable_retry(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'starting': True, 'phase': 'provision', 'run_token': 'b' * 64})
        with patch('platform_services.cloud_deployments._ready'), patch('platform_services.cloud_aws.recover_run', side_effect=AWSAdapterError('AWS start outcome is ambiguous. Review the runner before retrying.')), patch('platform_services.cloud_aws.start_run') as start, patch('platform_services.cloud_aws.stop_run') as stop:
            self.assertTrue(process_one())
            self.assertFalse(process_one())
        release.refresh_from_db()
        start.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(release.status, 'failed')
        self.assertIn('ambiguous', release.error)
        self.assertTrue(release.provider_state['starting'])

    def test_revoked_admin_stops_active_recorded_runner_and_prevents_next_phase(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'build_id': 'runner:active-job', 'phase': 'provision'})
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        with patch('platform_services.cloud_aws.stop_run') as stop, patch('platform_services.cloud_aws.start_run') as start, patch('platform_services.cloud_aws.poll_run') as poll:
            self.assertTrue(process_one())
        release.refresh_from_db()
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[2]['build_id'], 'runner:active-job')
        self.assertEqual(stop.call_args.args[1]['action'], 'provision')
        start.assert_not_called()
        poll.assert_not_called()
        self.assertEqual(release.status, 'failed')
        self.assertEqual(release.provider_state['build_id'], 'runner:active-job')

    def test_failed_automatic_stop_preserves_named_job_and_manual_recovery_notice(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'build_id': 'runner:active-job', 'phase': 'release', 'public_config': {}})
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        with patch('platform_services.cloud_aws.stop_run', side_effect=AWSAdapterError('Scoped role denied cancellation.')) as stop:
            self.assertTrue(process_one())
        release.refresh_from_db()
        stop.assert_called_once()
        self.assertEqual(release.status, 'failed')
        self.assertEqual(release.provider_state['build_id'], 'runner:active-job')
        self.assertIn('could not be stopped automatically', release.error)

    def test_operator_disable_stops_active_job_before_any_provider_poll(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'build_id': 'runner:active-job', 'phase': 'provision'})
        with override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=False), patch('platform_services.cloud_deployments._ready'), patch('platform_services.cloud_aws.stop_run') as stop, patch('platform_services.cloud_aws.poll_run') as poll:
            self.assertTrue(process_one())
        release.refresh_from_db()
        stop.assert_called_once()
        poll.assert_not_called()
        self.assertEqual(release.status, 'failed')
        self.assertIn('disabled by the operator', release.error)

    def test_worker_that_loses_lease_before_start_cannot_launch_billable_build(self):
        release = self.live_release()
        replacement_owner = uuid.uuid4()

        def lose_lease_during_access_check(value, principal):
            CloudRelease.objects.filter(pk=value.pk).update(lease_owner=replacement_owner)

        with patch('platform_services.cloud_deployments._check', side_effect=lose_lease_during_access_check), patch('platform_services.cloud_aws.start_run') as start:
            process_one()
        start.assert_not_called()
        release.refresh_from_db()
        self.assertEqual(release.lease_owner, replacement_owner)
        self.assertEqual(release.status, 'running')
        self.assertNotIn('starting', release.provider_state)

    def test_stale_worker_cannot_commit_terminal_status_or_success_audit_after_losing_lease(self):
        release = self.live_release()
        CloudRelease.objects.filter(pk=release.pk).update(provider_state={'build_id': 'runner:active-job', 'phase': 'release', 'public_config': {}})
        replacement_owner = uuid.uuid4()

        def lose_lease_before_result(platform, snapshot, state):
            CloudRelease.objects.filter(pk=release.pk).update(lease_owner=replacement_owner)
            return {'status': 'succeeded', 'outputs': {}, 'provider_state': {}}

        with patch('platform_services.cloud_deployments._ready'), patch('platform_services.cloud_aws.poll_run', side_effect=lose_lease_before_result):
            self.assertTrue(process_one())
        release.refresh_from_db()
        self.assertEqual(release.status, 'running')
        self.assertEqual(release.lease_owner, replacement_owner)
        self.assertFalse(AuditEvent.objects.filter(target=str(release.pk), action__in=['cloud.release_succeeded', 'cloud.release_failed']).exists())
