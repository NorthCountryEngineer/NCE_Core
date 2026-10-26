"""Durable multi-organization credentials, exercised without AWS or provider calls."""
import io
import json
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from identity.access import Principal
from identity.models import Application, AuditEvent, AuthenticationEvidence, AuthenticationPolicy, Membership, Organization, User
from platform_services.cloud_aws import AWSAdapterError, store_credentials
from platform_services.cloud_deployments import _register, approve_release, create_application, prepare_release, verify_platform
from platform_services.cloud_models import CloudApplication, CloudPlatform, CloudRelease
from platform_services.cloud_registrations import REGISTRY_KEY, registration_bindings, resource_bundle
from platform_services.environments import create_environment
from platform_services.external_apps import external_resource_credentials, register_external_deployment
from platform_services.models import MarketplacePackage
from platform_services.services import install_package
from .test_cloud_aws import platform


IDENTITY = {'callback_path': '/api/auth/core/callback', 'permissions': [
    {'codename': 'workspace-use', 'name': 'Use workspace'}]}
ORIGIN = 'https://workspace.example.test'


class AuthenticationEvidenceRegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        DurableRegistrationTests.setUpTestData.__func__(cls)
        for organization in (cls.owner, cls.target_org):
            AuthenticationPolicy.objects.create(organization=organization, max_session_age=60)
        cls.evidence = AuthenticationEvidence.objects.create(user=cls.actor,
            authenticated_at=timezone.now(), verified_at=timezone.now())

    def configure(self, **overrides):
        args = {'application': str(self.app.pk), 'deployment': [str(self.target.pk)],
            'actor_email': self.actor.email, 'stdout': io.StringIO(), **overrides}
        call_command('configure_shared_application_registrations', **args)
        self.app.refresh_from_db()

    def test_real_authentication_policy_rejects_absent_evidence_and_accepts_exact_current_actor(self):
        with self.assertRaises(CommandError):
            self.configure()
        self.app.refresh_from_db()
        self.assertEqual(self.app.config, {})
        self.configure(authentication_evidence=str(self.evidence.pk))
        self.assertEqual(self.app.config[REGISTRY_KEY], [str(self.target.pk)])
        event = AuditEvent.objects.get(action='cloud.application_registration_evidence', organization=self.owner)
        self.assertEqual(event.target, f'{self.app.pk}:{self.evidence.pk}')
        self.assertEqual(event.actor_id, self.actor.pk)

    def test_foreign_revoked_expired_and_malformed_evidence_never_change_config(self):
        foreign = User.objects.create_user('foreign-proof@example.test', 'synthetic-test-only')
        proof = AuthenticationEvidence.objects.create(user=foreign, authenticated_at=timezone.now())
        candidates = [str(proof.pk), 'not-a-uuid']
        for identifier in candidates:
            with self.subTest(identifier=identifier), self.assertRaises(CommandError):
                self.configure(authentication_evidence=identifier)
        for changes in ({'revoked': True}, {'revoked': False, 'verified_at': timezone.now() - timedelta(minutes=2)},
                        {'verified_at': timezone.now() + timedelta(minutes=2)}):
            AuthenticationEvidence.objects.filter(pk=self.evidence.pk).update(**changes)
            with self.subTest(changes=list(changes)), self.assertRaises(CommandError):
                self.configure(authentication_evidence=str(self.evidence.pk))
        self.app.refresh_from_db()
        self.assertEqual(self.app.config, {})

    def test_target_mfa_and_federation_policy_are_preserved(self):
        policy = AuthenticationPolicy.objects.get(organization=self.target_org)
        policy.require_mfa = True; policy.save(update_fields=['require_mfa'])
        with self.assertRaises(CommandError):
            self.configure(authentication_evidence=str(self.evidence.pk))
        AuthenticationEvidence.objects.filter(pk=self.evidence.pk).update(mfa=True)
        self.configure(authentication_evidence=str(self.evidence.pk))
        policy.require_federated = True; policy.save(update_fields=['require_federated'])
        with self.assertRaises(CommandError):
            self.configure(authentication_evidence=str(self.evidence.pk))

    def test_permission_and_purpose_checks_still_apply_with_valid_evidence(self):
        tag = self.target_env.boundary_tag
        tag.allowed_purposes = ['approved-maintenance']; tag.save(update_fields=['allowed_purposes'])
        with self.assertRaises(CommandError):
            self.configure(authentication_evidence=str(self.evidence.pk))
        self.configure(authentication_evidence=str(self.evidence.pk), purpose='approved-maintenance')
        Membership.objects.filter(organization=self.target_org, user=self.actor).update(kind='member')
        with self.assertRaises(CommandError):
            self.configure(authentication_evidence=str(self.evidence.pk), purpose='approved-maintenance')


class DurableRegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = Organization.objects.create(name='Customer A', slug='registry-a')
        cls.target_org = Organization.objects.create(name='Customer B', slug='registry-b')
        cls.actor = User.objects.create_user('registry-admin@example.test', 'synthetic-local-passphrase')
        for organization in (cls.owner, cls.target_org):
            Membership.objects.create(organization=organization, user=cls.actor, kind='administrator')
        cls.source_env = create_environment(cls.owner, name='development', namespace='registry-a-dev', application_origin=ORIGIN)
        cls.target_env = create_environment(cls.target_org, name='development', namespace='registry-b-dev', application_origin=ORIGIN)
        cls.platform = CloudPlatform.objects.create(organization=cls.owner, name='Local registry fixture', mode='demo',
            allowed_repositories=['Example/app'], config={'environment_ids': [str(cls.source_env.pk)], 'archetypes': ['amplify-fargate']})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.actor))
        cls.app = create_application(cls.owner, Principal(user=cls.actor), platform=cls.platform,
            environment=cls.source_env, name='Workspace', slug='registry-workspace', repository='Example/app',
            branch='main', commit='a' * 40, archetype='amplify-fargate', public_origin=ORIGIN)
        cls.outputs = {'origin': ORIGIN, 'identity': json.dumps(IDENTITY)}
        first = prepare_release(cls.app, Principal(user=cls.actor))
        _, _, cls.primary = _register(first, cls.outputs)
        cls.app.refresh_from_db()
        package = MarketplacePackage.objects.create(organization=cls.target_org, slug='registry-b-workspace', name='Workspace',
            version='1.0.0', kind='application', description='Synthetic local test',
            manifest={'external_runtime': 'aws-amplify', 'capabilities': [], **IDENTITY})
        installation = install_package(cls.target_org, package, actor=cls.actor)
        cls.target = register_external_deployment(installation, cls.target_env, ORIGIN, cls.actor)

    def configure(self, **overrides):
        args = {'application': str(self.app.pk), 'deployment': [str(self.target.pk)],
                'actor_email': self.actor.email, 'stdout': io.StringIO(), **overrides}
        if args.get('clear'):
            args.pop('deployment', None)
        call_command('configure_shared_application_registrations', **args)
        self.app.refresh_from_db()

    def export_release(self):
        release = prepare_release(self.app, Principal(user=self.actor))
        public, bundle, _ = _register(release, self.outputs)
        return release, public, bundle

    def test_repeat_release_rebuilds_registry_and_real_writer_only_puts_scoped_standard_bundle(self):
        self.configure()
        first, public, bundle = self.export_release()
        second, _, repeated = self.export_release()
        self.assertEqual(bundle, repeated)
        self.assertEqual(bundle['registrations'][0]['deployment_id'], str(self.target.pk))
        self.assertEqual(first.snapshot['core_primary_registration_binding']['deployment_id'], str(self.primary.pk))
        self.assertEqual(second.snapshot['core_registration_bindings'], registration_bindings(self.app))
        secret = bundle['registrations'][0]['resource_client_secret']
        self.assertNotIn(secret, json.dumps(public) + json.dumps(first.snapshot))
        ssm = Mock()
        ssm.put_parameter.return_value = {'Version': 7}
        settings = platform()
        parameter = f'/{settings.prefix}/apps/{self.app.pk}/core-resource-client'
        receipt = store_credentials(settings, parameter, repeated, client_factory=lambda service: ssm)
        self.assertEqual(receipt, {'parameter': parameter, 'version': 7})
        written = ssm.put_parameter.call_args.kwargs
        self.assertEqual(json.loads(written['Value']), bundle)
        self.assertEqual((written['Name'], written['Type'], written['Tier'], written['Overwrite']),
                         (parameter, 'SecureString', 'Standard', True))
        ssm.get_parameter.assert_not_called()
        self.assertNotIn(secret, json.dumps(receipt))

    def test_mapping_change_invalidates_review_and_cannot_change_during_queued_release(self):
        self.configure()
        release, _, _ = self.export_release()
        self.configure(deployment=None, clear=True)
        with self.assertRaisesMessage(ValidationError, 'reviewed plan changed'):
            approve_release(release, Principal(user=self.actor))
        self.configure()
        queued = prepare_release(self.app, Principal(user=self.actor))
        approve_release(queued, Principal(user=self.actor))
        with self.assertRaises(CommandError):
            self.configure(deployment=None, clear=True)
        self.app.refresh_from_db()
        self.assertEqual(self.app.config, {REGISTRY_KEY: [str(self.target.pk)]})

    def test_target_admin_revocation_cannot_add_registration_and_leaves_config_unchanged(self):
        Membership.objects.filter(organization=self.target_org, user=self.actor).update(active=False)
        with self.assertRaises(CommandError):
            self.configure()
        self.app.refresh_from_db()
        self.assertEqual(self.app.config, {})

    def test_revoked_target_fails_closed_without_resurrecting_secret(self):
        self.configure()
        self.target.resource_application.active = False
        self.target.resource_application.save(update_fields=['active'])
        with self.assertRaisesMessage(ValidationError, 'unavailable'):
            self.export_release()
        self.configure(deployment=None, clear=True)
        _, _, bundle = self.export_release()
        self.assertEqual(set(bundle), {'client_id', 'client_secret'})
        self.assertTrue(AuditEvent.objects.filter(action='cloud.application_registration_removed',
            target=f'{self.app.pk}:{self.target.pk}').exists())

    def test_ui_rotation_updates_canonical_encrypted_copy_and_new_release_exports_only_current_secret(self):
        self.configure()
        _, _, old = self.export_release()
        self.client.force_login(self.actor)
        response = self.client.post(reverse('rotate_client_secret', args=[self.target_org.slug,
            self.target.installation.service_id, self.target.resource_application_id]))
        self.assertEqual(response.status_code, 200)
        replacement = response.context['client_secret']
        self.target.refresh_from_db()
        self.target.resource_application.refresh_from_db()
        self.assertTrue(check_password(replacement, self.target.resource_application.client_secret))
        self.assertEqual(external_resource_credentials(self.target)['client_secret'], replacement)
        _, _, updated = self.export_release()
        self.assertNotEqual(old['registrations'][0]['resource_client_secret'], replacement)
        self.assertEqual(updated['registrations'][0]['resource_client_secret'], replacement)

    def test_stale_rotated_or_foreign_encrypted_secret_is_never_exported(self):
        self.configure()
        Application.objects.filter(pk=self.target.resource_application_id).update(client_secret=make_password('new-local-secret'))
        with self.assertRaisesMessage(ValidationError, 'current secure deployment copy'):
            self.export_release()
        Application.objects.filter(pk=self.target.resource_application_id).update(active=False)
        with self.assertRaisesMessage(ValidationError, 'unavailable'):
            resource_bundle(self.app, self.primary)

    def test_malformed_unknown_duplicate_and_foreign_origin_allowlists_fail_without_config_change(self):
        for selection in ([str(uuid4())], [str(self.target.pk)] * 2, ['bad'], [str(self.primary.pk)]):
            with self.subTest(selection=selection), self.assertRaises(CommandError):
                self.configure(deployment=selection)
        self.target.application.redirect_uris = 'https://foreign.example.test/api/auth/core/callback'
        self.target.application.save(update_fields=['redirect_uris'])
        with self.assertRaises(CommandError):
            self.configure()
        self.app.refresh_from_db()
        self.assertEqual(self.app.config, {})
        self.app.config = {'registrations': [{'client_secret': uuid4().hex}]}
        with self.assertRaises(ValidationError):
            self.app.full_clean()

    def test_primary_stale_secret_fails_during_review_before_any_cloud_function(self):
        self.configure()
        Application.objects.filter(pk=self.primary.resource_application_id).update(client_secret=make_password('rotated-local-secret'))
        with self.assertRaisesMessage(ValidationError, 'current secure deployment copy'):
            prepare_release(self.app, Principal(user=self.actor))

    def test_primary_rebind_or_foreign_installation_is_not_an_approved_plan(self):
        self.configure()
        release, _, _ = self.export_release()
        CloudApplication.objects.filter(pk=self.app.pk).update(installation=self.target.installation)
        self.app.refresh_from_db()
        with self.assertRaises(ValidationError):
            approve_release(release, Principal(user=self.actor))

    def test_writer_rejects_malformed_foreign_fields_duplicates_and_utf8_overflow_before_aws(self):
        self.configure()
        _, _, valid = self.export_release()
        malformed = [dict(valid, injected='foreign'), dict(valid, registrations='foreign'),
            dict(valid, registrations=[]), dict(valid, registrations=valid['registrations'] * 2)]
        extra = dict(valid['registrations'][0], arbitrary_secret='foreign')
        malformed.append(dict(valid, registrations=[extra]))
        malformed.append(dict(valid, registrations=[dict(valid['registrations'][0], organization_id='not-uuid')]))
        malformed.append(dict(valid, registrations=[dict(valid['registrations'][0], resource_client_secret='界' * 2000)]))
        settings, factory = platform(), Mock(side_effect=AssertionError('No client before validation'))
        for value in malformed:
            with self.subTest(keys=list(value)), self.assertRaises(AWSAdapterError):
                store_credentials(settings, f'/{settings.prefix}/apps/{self.app.pk}/core-resource-client', value,
                                  client_factory=factory)
        factory.assert_not_called()
