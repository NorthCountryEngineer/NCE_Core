"""Approval and worker boundaries exercised without cloud credentials or calls."""
from unittest.mock import patch
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from identity.access import Principal
from identity.models import Membership, Organization, PermissionTag, User
from platform_services.cloud_deployments import (approve_release, authorized, cancel_release, create_application,
    prepare_release, process_one, update_application, verify_platform)
from platform_services.cloud_deployments import resolve_source
from platform_services.cloud_models import CloudPlatform, CloudRelease
from platform_services.environments import create_environment


class CloudDeploymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Deploy test', slug='deploy-test')
        cls.admin = User.objects.create_user('deploy@example.test', 'local-tests-only')
        cls.membership = Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        cls.env = create_environment(cls.org, name='development', namespace='deploy-dev', application_origin='https://app.example.test')
        cls.platform = CloudPlatform.objects.create(organization=cls.org, name='Preview', mode='demo',
            allowed_repositories=['Example/app'], config={'environment_ids': [str(cls.env.pk)], 'archetypes': ['amplify-static', 'amplify-serverless']})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.admin))

    def app(self):
        return create_application(self.org, Principal(user=self.admin), platform=self.platform, environment=self.env,
            name='Demo app', slug='demo', repository='Example/app', branch='main', commit='a' * 40, archetype='amplify-serverless')

    def release(self):
        return prepare_release(self.app(), Principal(user=self.admin))

    def due(self, release):
        CloudRelease.objects.filter(pk=release.pk).update(next_poll=None)

    def test_demo_completes_and_registers_identity_without_provider(self):
        release = self.release()
        approve_release(release, Principal(user=self.admin))
        with patch('platform_services.cloud_aws.start_run', side_effect=AssertionError('AWS must never be called')):
            self.assertTrue(process_one())
            release.refresh_from_db()
            self.assertEqual(release.status, 'running', release.error)
            self.assertEqual(release.provider_state['phase'], 'release')
            self.assertFalse(process_one())  # durable polling delay
            self.due(release)
            self.assertTrue(process_one())
        release.refresh_from_db()
        self.assertEqual(release.status, 'succeeded')
        self.assertEqual(release.outputs['application_url'], 'https://demo.example.test')
        self.assertNotIn('client_secret', str(release.outputs) + str(release.provider_state))
        release.application.refresh_from_db()
        self.assertIsNotNone(release.application.installation_id)
        self.assertEqual(release.application.installation.deployments.get(target='external').application.client_type, 'public')

    def test_exact_review_changes_invalidate_approval(self):
        release = self.release()
        app = release.application
        update_application(app, Principal(user=self.admin), commit='b' * 40)
        with self.assertRaises(ValidationError):
            approve_release(release, Principal(user=self.admin))
        release.refresh_from_db()
        self.assertEqual(release.status, 'draft')

    def test_tampered_snapshot_rejected(self):
        release = self.release()
        release.snapshot['commit'] = 'b' * 40
        release.save()
        with self.assertRaises(ValidationError):
            approve_release(release, Principal(user=self.admin))

    def test_only_one_active_release_per_app(self):
        one = self.release()
        two = prepare_release(one.application, Principal(user=self.admin))
        approve_release(one, Principal(user=self.admin))
        with self.assertRaises(ValidationError):
            approve_release(two, Principal(user=self.admin))

    def test_platform_edits_revoke_verification(self):
        self.platform.allowed_repositories.append('Example/other')
        self.platform.save()
        self.assertFalse(self.platform.active)
        with self.assertRaises(ValidationError):
            self.app()

    def test_worker_rechecks_revoked_administrator(self):
        release = self.release()
        approve_release(release, Principal(user=self.admin))
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        with patch('platform_services.cloud_aws.start_run') as start:
            process_one()
            start.assert_not_called()
        release.refresh_from_db()
        self.assertEqual(release.status, 'failed')

    def test_worker_rechecks_tags_after_approval(self):
        release = self.release()
        approve_release(release, Principal(user=self.admin))
        tag = PermissionTag.objects.create(organization=self.org, name='MFA', slug='mfa', requires_mfa=True)
        self.platform.permission_tags.add(tag)
        process_one()
        release.refresh_from_db()
        self.assertEqual(release.status, 'failed')

    def test_environment_cannot_cross_tenants(self):
        other = Organization.objects.create(name='Other', slug='other-deploy')
        env = create_environment(other, name='development', namespace='other-dev', application_origin='https://other.example.test')
        with self.assertRaises((ValidationError, PermissionDenied)):
            create_application(self.org, Principal(user=self.admin), platform=self.platform, environment=env,
                name='Bad', slug='bad', repository='Example/app', branch='main', commit='a' * 40, archetype='amplify-static')

    def test_machine_cannot_approve(self):
        release = self.release()
        with self.assertRaises(PermissionDenied):
            authorized(Principal(user=self.admin, credential=object()), release)

    def test_active_lease_not_claimed_and_expired_lease_recovers(self):
        release = self.release()
        approve_release(release, Principal(user=self.admin))
        CloudRelease.objects.filter(pk=release.pk).update(lease_until=timezone.now() + timedelta(minutes=1))
        self.assertFalse(process_one())
        CloudRelease.objects.filter(pk=release.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(process_one())

    def test_cancelled_draft_never_runs(self):
        release = self.release()
        cancel_release(release, Principal(user=self.admin))
        self.assertFalse(process_one())

    @override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=False)
    def test_live_gate_requires_cost_consent_and_operator_switch(self):
        release = self.release()
        # Test control-plane gating independently of AWS configuration checks.
        self.platform.mode = 'aws'
        self.platform.save()
        with patch('platform_services.cloud_deployments._check'):
            with self.assertRaises(ValidationError):
                approve_release(release, Principal(user=self.admin), confirm_costs=True)

    def test_public_origin_and_placement_immutable(self):
        app = self.app()
        with self.assertRaises(ValidationError):
            update_application(app, Principal(user=self.admin), public_origin='https://new.example.test')

    def test_demo_branch_resolution_never_contacts_github(self):
        with patch('requests.get', side_effect=AssertionError('No network in simulation')):
            self.assertEqual(resolve_source(self.platform, 'Example/app', 'main'), 'd' * 40)

    def test_public_branch_resolution_pins_result_and_does_not_follow_redirects(self):
        self.platform.mode = 'aws'
        with patch('requests.get') as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {'sha': 'c' * 40}
            self.assertEqual(resolve_source(self.platform, 'Example/app', 'feature/name'), 'c' * 40)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(get.call_args.args[0].endswith('/feature%2Fname'))
            get.return_value.status_code = 302
            with self.assertRaises(ValidationError):
                resolve_source(self.platform, 'Example/app', 'main')

    @override_settings(CORE_ENABLE_CLOUD_DEPLOYMENTS=True)
    def test_live_two_phase_provider_handoff_stores_secret_only_in_ssm(self):
        self.platform.mode = 'aws'
        self.platform.verified_digest = self.platform.fingerprint()
        self.platform.save()
        with patch('platform_services.cloud_deployments._ready'):
            release = self.release()
            approve_release(release, Principal(user=self.admin), confirm_costs=True)
            with patch('platform_services.cloud_aws.start_run', side_effect=[
                    {'build_id': 'runner:provision'}, {'build_id': 'runner:release'}]) as start, \
                    patch('platform_services.cloud_aws.poll_run', side_effect=[
                        {'status': 'succeeded', 'outputs': {'origin': 'https://demo.example.test', 'api_url': 'https://api.example.test'}, 'provider_state': {}},
                        {'status': 'succeeded', 'outputs': {'commit': 'a' * 40}, 'provider_state': {}}]) as poll, \
                    patch('platform_services.cloud_aws.store_credentials') as store:
                for _ in range(4):
                    self.due(release)
                    self.assertTrue(process_one())
                self.assertEqual(start.call_count, 2)
                self.assertEqual(poll.call_count, 2)
                store.assert_called_once()
                self.assertEqual(set(store.call_args.args[2]), {'client_id', 'client_secret'})
                self.assertEqual(start.call_args_list[0].args[1]['action'], 'provision')
                release_payload = start.call_args_list[1].args[1]
                self.assertEqual(release_payload['action'], 'release')
                self.assertIn('VITE_CORE_BROWSER_CLIENT_ID', release_payload['public_config'])
                self.assertNotIn('client_secret', str(release_payload))
        release.refresh_from_db()
        self.assertEqual(release.status, 'succeeded', release.error)
        self.assertNotIn('client_secret', str(release.provider_state) + str(release.outputs))
