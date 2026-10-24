"""Disposable database adoption; all external transports are forbidden."""
import io
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model, get_grant_model

from identity.models import (ApplicationAccountAccess, AuditEvent, AuthenticationPolicy, Membership, Organization,
    PermissionTag, ProtectedResource, Role, Service, ServicePermission, User)
from platform_services.application_adoption import apply_adoption, plan_adoption
from platform_services.cloud_models import CloudApplication, CloudHostingAssignment, CloudPlatform, CloudRelease
from platform_services.environments import create_environment
from platform_services.external_apps import register_external_deployment
from platform_services.models import ApplicationPin, AgentBinding, AgentAccount, Installation, MarketplacePackage
from platform_services.services import install_core, install_package
from identity.access import Principal, decision
from identity.policy import authorization_for


class ApplicationAdoptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.source = Organization.objects.create(slug='source-owner', name='Original owner')
        cls.target = Organization.objects.create(slug='target-owner', name='New owner')
        cls.actor = User.objects.create_user('operator@example.test')
        Membership.objects.create(organization=cls.source, user=cls.actor, kind='administrator')
        Membership.objects.create(organization=cls.target, user=cls.actor, kind='administrator')
        cls.unrelated = User.objects.create_user('unrelated@example.test')
        Membership.objects.create(organization=cls.source, user=cls.unrelated, kind='administrator')
        cls.member = User.objects.create_user('application-member@example.test')
        cls.membership = Membership.objects.create(organization=cls.source, user=cls.member)
        install_core(cls.source)
        install_core(cls.target)  # Legitimate baseline, not an empty-org gate.
        cls.oldenv = create_environment(cls.source, name='production', namespace='original-production',
            application_origin='https://application.example.test')
        cls.newenv = create_environment(cls.target, name='production', namespace='target-production',
            application_origin='https://application.example.test')
        package = json.loads((Path(settings.BASE_DIR) / 'examples/axolotl-news-package.json').read_text().replace(
            'posts-write', 'workspace-use'))
        cls.package = MarketplacePackage.objects.create(organization=cls.source, **package)
        cls.installation = install_package(cls.source, cls.package, actor=cls.actor)
        cls.deployment = register_external_deployment(cls.installation, cls.oldenv,
            'https://application.example.test', cls.actor)
        cls.platform = CloudPlatform.objects.create(organization=cls.source, name='Existing provider',
            allowed_repositories=['Example/application'], config={'environment_ids': [str(cls.oldenv.pk)],
                'archetypes': ['amplify-fargate']})
        cls.app = CloudApplication.objects.create(organization=cls.source, platform=cls.platform, environment=cls.oldenv,
            name='Existing application', slug='existing-application', repository='Example/application',
            commit='a' * 40, archetype='amplify-fargate', public_origin='https://application.example.test',
            installation=cls.installation)
        cls.permission = cls.installation.service.permissions.get(codename='workspace-use')
        cls.role = Role.objects.create(organization=cls.source, name='Application editor', slug='application-editor')
        cls.role.permissions.add(cls.permission)
        cls.membership.roles.add(cls.role)
        cls.membership.access_tags.add(cls.oldenv.boundary_tag)
        cls.account = ApplicationAccountAccess.objects.create(membership=cls.membership,
            deployment=cls.deployment, role_ids=['editor'])
        cls.pin = ApplicationPin.objects.create(membership=cls.membership, deployment=cls.deployment)
        cls.resource = ProtectedResource.objects.create(service=cls.installation.service,
            resource_type='record', external_id='opaque-record', name='Application record')
        cls.resource.permission_tags.add(cls.oldenv.boundary_tag)
        cls.history = CloudRelease.objects.create(application=cls.app, requested_by=cls.actor,
            snapshot={'organization_id': str(cls.source.pk), 'private_config': 'private-snapshot-value'},
            digest='b' * 64, status='succeeded', outputs={'origin': cls.app.public_origin})

    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        for name in ['start_run', 'store_credentials']:
            patcher = patch('platform_services.cloud_aws.' + name, side_effect=AssertionError('Cloud mutation forbidden'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self):
        return [self.app.pk, self.target.pk, self.newenv.pk, self.actor.pk]

    def plan(self):
        return plan_adoption(*self.args(), member_user_ids=[self.actor.pk])

    def apply(self):
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        return apply_adoption(*self.args(), member_user_ids=[self.actor.pk], expected_plan_hash=plan['plan_hash'])

    def test_plan_is_read_only_deterministic_and_contains_no_secrets(self):
        before = list(AuditEvent.objects.values_list('pk', flat=True))
        one, two = self.plan(), self.plan()
        self.assertEqual(one, two)
        self.assertEqual(one['blockers'], [])
        text = json.dumps(one)
        self.assertNotIn(self.deployment.encrypted_bundle, text)
        self.assertNotIn('private-snapshot-value', text)
        self.assertNotIn(self.deployment.resource_application.client_secret, text)
        self.app.refresh_from_db()
        self.assertEqual(self.app.organization_id, self.source.pk)
        self.assertEqual(list(AuditEvent.objects.values_list('pk', flat=True)), before)
        self.assertFalse(CloudHostingAssignment.objects.exists())

    def test_apply_preserves_identity_ciphertext_history_and_provider(self):
        ids = (self.app.pk, self.installation.pk, self.installation.service_id,
            self.deployment.pk, self.deployment.application_id, self.deployment.resource_application_id)
        ciphertext = self.deployment.encrypted_bundle
        secret = self.deployment.resource_application.client_secret
        history = self.history.snapshot.copy()
        provider_config = self.platform.config.copy()
        self.apply()
        for obj in [self.app, self.installation, self.deployment, self.platform, self.package, self.history]:
            obj.refresh_from_db()
        self.assertEqual(ids, (self.app.pk, self.installation.pk, self.installation.service_id,
            self.deployment.pk, self.deployment.application_id, self.deployment.resource_application_id))
        self.assertEqual(self.app.organization_id, self.target.pk)
        self.assertEqual(self.installation.service.organization_id, self.target.pk)
        self.assertEqual(self.deployment.environment_id, self.newenv.pk)
        self.assertEqual(self.deployment.parent.installation.organization_id, self.target.pk)
        self.assertEqual(self.deployment.encrypted_bundle, ciphertext)
        self.assertEqual(self.deployment.resource_application.client_secret, secret)
        self.assertEqual(self.history.snapshot, history)
        self.assertEqual(self.platform.organization_id, self.source.pk)
        self.assertEqual(self.platform.config, provider_config)
        self.assertEqual(self.package.organization_id, self.target.pk)
        self.assertEqual(self.app.public_origin, 'https://application.example.test')
        assignment = CloudHostingAssignment.objects.get(application=self.app)
        self.assertEqual(assignment.provider_environment_id, self.oldenv.pk)
        self.assertEqual(assignment.owner_environment_id, self.newenv.pk)
        self.assertEqual(AuditEvent.objects.filter(action__startswith='cloud.application_adopted_').count(), 2)

    def test_memberships_and_scoped_roles_pins_accounts_move_without_other_family_users(self):
        self.apply()
        self.assertFalse(Membership.objects.filter(organization=self.target, user=self.unrelated).exists())
        target_member = Membership.objects.get(organization=self.target, user=self.member)
        self.assertEqual(target_member.kind, 'member')
        self.assertTrue(target_member.roles.filter(permissions=self.permission).exists())
        self.assertTrue(target_member.access_tags.filter(pk=self.newenv.boundary_tag_id).exists())
        self.account.refresh_from_db()
        self.pin.refresh_from_db()
        self.assertEqual(self.account.membership_id, target_member.pk)
        self.assertEqual(self.pin.membership_id, target_member.pk)
        self.assertEqual(self.account.role_ids, ['editor'])
        deployment = type(self.deployment).objects.select_related('application', 'installation__service').get(pk=self.deployment.pk)
        grants = authorization_for(target_member, deployment.installation.service,
            Principal(user=self.member, application=deployment.application))
        self.assertIn('workspace-use', grants['permissions'])
        self.assertIn('editor', grants['roles'])
        # Old source membership cannot authorize the now-target-owned service.
        self.assertEqual(authorization_for(self.membership, deployment.installation.service)['permissions'], [])
        self.assertFalse(self.role.permissions.filter(pk=self.permission.pk).exists())
        self.resource.refresh_from_db()
        self.assertEqual(list(self.resource.permission_tags.all()), [self.newenv.boundary_tag])

    def test_exact_hash_rejects_changed_graph_without_partial_writes(self):
        plan = self.plan()
        self.deployment.encrypted_bundle = 'changed-encrypted-bundle'
        self.deployment.save(update_fields=['encrypted_bundle'])
        with self.assertRaisesMessage(ValidationError, 'state changed'):
            apply_adoption(*self.args(), member_user_ids=[self.actor.pk], expected_plan_hash=plan['plan_hash'])
        self.app.refresh_from_db()
        self.assertEqual(self.app.organization_id, self.source.pk)
        self.assertFalse(CloudHostingAssignment.objects.exists())

    def test_nonterminal_release_and_dependent_agent_are_reported_not_lost(self):
        self.history.status = 'running'
        self.history.save(update_fields=['status'])
        account = AgentAccount.objects.create(organization=self.source, name='Bound agent')
        AgentBinding.objects.create(account=account, installation=self.installation, environment='production')
        plan = self.plan()
        self.assertIn('unfinished_releases', [b['code'] for b in plan['blockers']])
        self.assertIn('unsupported_dependency', [b['code'] for b in plan['blockers']])
        with self.assertRaisesMessage(ValidationError, 'every reported'):
            apply_adoption(*self.args(), member_user_ids=[self.actor.pk], expected_plan_hash=plan['plan_hash'])

    def test_shared_private_package_and_policy_conflicts_are_blockers(self):
        other = Organization.objects.create(slug='third-owner', name='Third owner')
        Installation.objects.create(organization=other, package=self.package,
            service=Service.objects.create(organization=other, name='Conflicting shared install', slug='conflict'),
            version=self.package.version)
        self.newenv.boundary_tag.requires_mfa = True
        self.newenv.boundary_tag.save()
        codes = {b['code'] for b in self.plan()['blockers']}
        self.assertIn('private_package_shared_or_foreign', codes)
        self.assertIn('target_policy_tag_conflict', codes)

    def test_apply_failure_rolls_back_every_move_and_audit(self):
        with patch.object(CloudHostingAssignment, 'save', side_effect=ValidationError('synthetic failure')):
            with self.assertRaisesMessage(ValidationError, 'synthetic failure'):
                self.apply()
        self.app.refresh_from_db()
        self.installation.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.app.organization_id, self.source.pk)
        self.assertEqual(self.installation.organization_id, self.source.pk)
        self.assertEqual(self.account.membership_id, self.membership.pk)
        self.assertTrue(self.role.permissions.filter(pk=self.permission.pk).exists())
        self.assertFalse(CloudHostingAssignment.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action__startswith='cloud.application_adopted_').exists())

    def test_actor_needs_live_admin_both_and_inactive_superuser_is_denied(self):
        Membership.objects.filter(organization=self.target, user=self.actor).update(kind='member')
        with self.assertRaises(PermissionDenied):
            self.plan()
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        self.assertEqual({b['code'] for b in self.plan()['blockers']}, {
            'target_requires_existing_active_administrator', 'administrator_requires_explicit_target_access'})
        Membership.objects.filter(organization=self.target, user=self.actor).update(kind='administrator')
        self.assertEqual(self.plan()['blockers'], [])
        self.actor.is_active = False
        self.actor.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):
            self.plan()

    def test_command_defaults_to_plan_and_requires_exact_hash_for_apply(self):
        out = io.StringIO()
        options = {'application': str(self.app.pk), 'target_organization': str(self.target.pk),
            'target_environment': str(self.newenv.pk), 'actor': str(self.actor.pk),
            'member_user': [str(self.actor.pk)], 'stdout': out}
        call_command('adopt_external_application', **options)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan['plan_hash'], self.plan()['plan_hash'])
        self.assertFalse(CloudHostingAssignment.objects.exists())
        out = io.StringIO()
        call_command('adopt_external_application', **{**options, 'stdout': out, 'apply_plan_hash': plan['plan_hash']})
        self.assertEqual(json.loads(out.getvalue())['cloud_operations'], [])

    def test_cutover_revokes_only_moved_client_tokens_and_codes(self):
        access_model, refresh_model, grant_model = get_access_token_model(), get_refresh_token_model(), get_grant_model()
        expires = timezone.now() + timedelta(hours=1)
        token = access_model.objects.create(application=self.deployment.application, user=self.member,
            token='synthetic-moved-access', expires=expires, scope='openid authorization')
        refresh = refresh_model.objects.create(application=self.deployment.application, user=self.member,
            token='synthetic-moved-refresh', access_token=token)
        grant = grant_model.objects.create(application=self.deployment.application, user=self.member,
            code='synthetic-moved-code', expires=expires, redirect_uri=self.deployment.application.redirect_uris,
            scope='openid authorization')
        other_install = self.source.installations.get(package__slug='core-platform')
        from identity.models import Application
        other_client = Application.objects.create(service=other_install.service,
            name='Unrelated client', client_type='public', authorization_grant_type='authorization-code')
        other_token = access_model.objects.create(application=other_client, user=self.actor,
            token='synthetic-unrelated-access', expires=expires, scope='openid authorization')
        self.assertEqual(self.plan()['revokes_oauth_records'][access_model._meta.label_lower], 1)
        self.apply()
        self.assertFalse(access_model.objects.filter(pk=token.pk).exists())
        self.assertFalse(grant_model.objects.filter(pk=grant.pk).exists())
        refresh.refresh_from_db()
        self.assertIsNotNone(refresh.revoked)
        self.assertIsNone(refresh.access_token_id)
        self.assertTrue(access_model.objects.filter(pk=other_token.pk).exists())

    def test_source_authentication_and_inherited_policy_cannot_be_weakened(self):
        AuthenticationPolicy.objects.create(organization=self.source, require_mfa=True, max_session_age=3600)
        self.assertIn('target_authentication_policy_weaker', {b['code'] for b in self.plan()['blockers']})
        AuthenticationPolicy.objects.create(organization=self.target, require_mfa=True, max_session_age=3600)
        required = PermissionTag.objects.create(organization=self.source, slug='restricted-reference',
            name='Restricted reference', classification='restricted', requires_mfa=True)
        self.source.permission_tags.add(required)
        self.apply()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.permission_tags.filter(slug=f'adopt-{self.app.pk.hex}-{required.pk}', requires_mfa=True,
            organization=self.target).exists())

    def test_same_named_target_tag_does_not_grant_unrelated_target_resource_access(self):
        source_tag = PermissionTag.objects.create(organization=self.source, slug='same-purpose', name='Same purpose')
        target_tag = PermissionTag.objects.create(organization=self.target, slug='same-purpose', name='Same purpose')
        self.resource.permission_tags.add(source_tag)
        self.membership.access_tags.add(source_tag)
        target_membership = Membership.objects.create(organization=self.target, user=self.member)
        target_membership.access_tags.add(self.newenv.boundary_tag)
        unrelated_service = Service.objects.create(organization=self.target, slug='unrelated-service', name='Unrelated service')
        unrelated_resource = ProtectedResource.objects.create(service=unrelated_service, resource_type='record',
            external_id='unrelated-record', name='Unrelated target record')
        unrelated_resource.permission_tags.add(target_tag, self.newenv.boundary_tag)
        self.assertFalse(decision(Principal(user=self.member), unrelated_resource)[0])
        plan = self.plan()
        mapping = next(item for item in plan['policy_tag_mappings'] if item['source_tag_id'] == str(source_tag.pk))
        self.assertEqual(mapping['target_slug'], f'adopt-{self.app.pk.hex}-{source_tag.pk}')
        self.assertFalse(mapping['reuses_environment_boundary'])
        self.apply()
        target_membership.refresh_from_db()
        self.assertFalse(target_membership.access_tags.filter(pk=target_tag.pk).exists())
        self.assertTrue(target_membership.access_tags.filter(slug=mapping['target_slug'], name=source_tag.name).exists())
        unrelated_resource = ProtectedResource.objects.get(pk=unrelated_resource.pk)
        self.assertFalse(decision(Principal(user=self.member), unrelated_resource)[0])
        self.assertTrue(decision(Principal(user=self.member), ProtectedResource.objects.get(pk=self.resource.pk))[0])

    def test_existing_app_tag_namespace_is_never_reused(self):
        tag = PermissionTag.objects.create(organization=self.source, slug='app-private', name='App private')
        self.resource.permission_tags.add(tag)
        PermissionTag.objects.create(organization=self.target, slug=f'adopt-{self.app.pk.hex}-{tag.pk}', name=tag.name)
        self.assertIn('target_policy_tag_namespace_conflict', {item['code'] for item in self.plan()['blockers']})

    def test_tag_only_source_role_preserves_scoped_app_grant_without_unrelated_permissions(self):
        self.membership.kind = 'administrator'
        self.membership.save(update_fields=['kind'])
        self.membership.roles.remove(self.role)
        required = PermissionTag.objects.create(organization=self.source, slug='app-policy', name='Application policy')
        self.permission.permission_tags.add(required)
        unrelated_service = Service.objects.create(organization=self.source, slug='unrelated-source', name='Unrelated source')
        unrelated_permission = ServicePermission.objects.create(service=unrelated_service, codename='unrelated-read', name='Read unrelated')
        grant_role = Role.objects.create(organization=self.source, slug='tag-only-for-this-app', name='Policy grant and unrelated permission')
        grant_role.access_tags.add(required)
        grant_role.permissions.add(unrelated_permission)
        self.membership.roles.add(grant_role)
        before = authorization_for(self.membership, self.installation.service,
            Principal(user=self.member, application=self.deployment.application))
        self.assertIn('workspace-use', before['permissions'])
        self.apply()
        target_member = Membership.objects.get(organization=self.target, user=self.member)
        self.assertEqual(target_member.kind, 'member')
        self.assertTrue(target_member.access_tags.filter(slug=f'adopt-{self.app.pk.hex}-{required.pk}').exists())
        deployment = type(self.deployment).objects.select_related('application', 'installation__service').get(pk=self.deployment.pk)
        after = authorization_for(target_member, deployment.installation.service,
            Principal(user=self.member, application=deployment.application))
        self.assertIn('workspace-use', after['permissions'])
        self.assertFalse(target_member.roles.filter(permissions=unrelated_permission).exists())
        self.assertTrue(grant_role.permissions.filter(pk=unrelated_permission.pk).exists())

    def test_source_admin_needs_reviewed_target_or_application_access_not_implicit_promotion(self):
        self.membership.kind = 'administrator'
        self.membership.save(update_fields=['kind'])
        self.membership.roles.clear()
        self.account.delete()
        plan = self.plan()
        self.assertIn('administrator_requires_explicit_target_access', {b['code'] for b in plan['blockers']})
        self.assertFalse(Membership.objects.filter(organization=self.target, user=self.member).exists())

    def test_other_environment_boundary_is_not_downgraded_to_custom_policy_tag(self):
        development = create_environment(self.source, name='development', namespace='source-development',
            application_origin='https://development.example.test')
        self.permission.permission_tags.add(development.boundary_tag)
        plan = self.plan()
        blocker = next(item for item in plan['blockers'] if item['code'] == 'unsupported_environment_policy_tag')
        self.assertEqual(blocker['environment_ids'], [str(development.pk)])
        self.assertEqual(blocker['tag_ids'], [str(development.boundary_tag_id)])

    def test_role_used_by_another_agent_approval_is_not_silently_stripped(self):
        account = AgentAccount.objects.create(organization=self.source, name='Unrelated agent')
        binding = AgentBinding.objects.create(account=account,
            installation=self.source.installations.get(package__slug='core-agents'), environment='production')
        binding.roles.add(self.role)
        self.assertNotEqual(binding.installation_id, self.installation.pk)
        plan = self.plan()
        blocker = next(item for item in plan['blockers'] if item['code'] == 'unsupported_dependency'
            and item['resource'] == 'identity.role' and item['relation'] == 'agent_bindings')
        self.assertEqual(blocker['ids'], [str(binding.pk)])
        with self.assertRaises(ValidationError):
            apply_adoption(*self.args(), member_user_ids=[self.actor.pk], expected_plan_hash=plan['plan_hash'])
        self.app.refresh_from_db()
        self.assertEqual(self.app.organization_id, self.source.pk)
        self.assertTrue(self.role.permissions.filter(pk=self.permission.pk).exists())
