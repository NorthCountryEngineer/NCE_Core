"""Existing-host ownership boundaries on disposable local DBs; no provider I/O."""
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from identity.access import Principal, resource_chain
from identity.models import Membership, Organization, PermissionTag, User
from platform_services.cloud_deployments import (approve_release, authorized, create_application,
    prepare_release, process_one, verify_platform)
from platform_services.cloud_models import CloudApplication, CloudHostingAssignment, CloudPlatform, hosted_assignment
from platform_services.cloud_portal import ApplicationForm
from platform_services.environments import create_environment
from platform_services.services import install_core


class CloudHostingAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = Organization.objects.create(slug='provider', name='Provider')
        cls.owner = Organization.objects.create(slug='owner', name='Owner')
        cls.operator = User.objects.create_superuser('operator@example.test', 'local-tests-only')
        cls.owner_admin = User.objects.create_user('owner@example.test', 'local-tests-only')
        cls.provider_admin = User.objects.create_user('provider@example.test', 'local-tests-only')
        Membership.objects.create(organization=cls.owner, user=cls.owner_admin, kind='administrator')
        Membership.objects.create(organization=cls.provider, user=cls.provider_admin, kind='administrator')
        install_core(cls.provider)
        install_core(cls.owner)
        cls.provider_environment = create_environment(cls.provider, name='production', namespace='provider-prod',
            application_origin='https://hosted.example.test')
        cls.owner_environment = create_environment(cls.owner, name='production', namespace='owner-prod',
            application_origin='https://hosted.example.test')
        cls.platform = CloudPlatform.objects.create(organization=cls.provider, name='Provider hosting', mode='demo',
            prefix='nce-family', allowed_repositories=['Example/app', 'Example/private-other'],
            config={'environment_ids': [str(cls.provider_environment.pk)], 'archetypes': ['amplify-static']})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.operator))

    def setUp(self):
        self.app = create_application(self.provider, Principal(user=self.operator), platform=self.platform,
            environment=self.provider_environment, name='Hosted app', slug='hosted', repository='Example/app',
            branch='main', commit='a' * 40, archetype='amplify-static', public_origin='https://hosted.example.test')
        self.historical = prepare_release(self.app, Principal(user=self.operator))
        self.historical.status = 'succeeded'
        self.historical.save(update_fields=['status'])
        self.original_snapshot = self.historical.snapshot.copy()
        CloudApplication.objects.filter(pk=self.app.pk).update(organization=self.owner, environment=self.owner_environment)
        self.app.refresh_from_db()
        self.assignment = CloudHostingAssignment.objects.create(application=self.app, platform=self.platform,
            provider_organization=self.provider, owner_organization=self.owner,
            provider_environment=self.provider_environment, owner_environment=self.owner_environment,
            slug=self.app.slug, repository=self.app.repository, archetype=self.app.archetype,
            public_origin=self.app.public_origin, actor=self.operator)

    def url(self, name, pk=None, organization=None):
        kwargs = {'slug': (organization or self.owner).slug}
        if pk:
            kwargs['pk'] = pk
        return reverse(name, kwargs=kwargs)

    def test_owner_reads_app_and_completed_history_without_provider_access(self):
        self.client.force_login(self.owner_admin)
        page = self.client.get(self.url('cloud_application', self.app.pk))
        self.assertContains(page, 'Hosted app')
        self.assertContains(page, 'Hosting changes are managed by your infrastructure provider.')
        self.assertNotContains(page, 'Prepare release')
        history = self.client.get(self.url('cloud_release', self.historical.pk))
        self.assertContains(history, 'Release complete')
        self.assertNotContains(history, 'Example/private-other')
        self.assertNotContains(history, 'platform_config')
        self.assertEqual(self.client.get(self.url('cloud_application', self.app.pk, self.provider)).status_code, 403)
        self.assertEqual(self.client.get(self.url('cloud_platform_edit', self.platform.pk)).status_code, 404)

    def test_owner_cannot_mutate_provider_hosting_or_edit_source(self):
        self.client.force_login(self.owner_admin)
        for name, method in [('cloud_application_edit', 'get'), ('cloud_release_prepare', 'post')]:
            response = getattr(self.client, method)(self.url(name, self.app.pk))
            self.assertEqual(response.status_code, 403)
        with self.assertRaises(PermissionDenied):
            prepare_release(self.app, Principal(user=self.owner_admin))

    def test_operator_can_release_exact_adopted_app_and_form_does_not_offer_other_repositories(self):
        self.app.full_clean()
        release = prepare_release(self.app, Principal(user=self.operator))
        self.assertIn('hosting_assignment_digest', release.snapshot)
        self.assertEqual(release.snapshot['secret_parameter'], f'/nce-family/apps/{self.app.pk}/core-resource-client')
        form = ApplicationForm(organization=self.owner, principal=Principal(user=self.operator), instance=self.app)
        self.assertEqual(list(form.fields['repository'].choices), [('', 'Choose a repository'), ('Example/app', 'Example/app')])
        self.assertEqual(set(form.environment_values), {str(self.owner_environment.pk)})
        self.historical.refresh_from_db()
        self.assertEqual(self.historical.snapshot, self.original_snapshot)
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.organization_id, self.provider.pk)
        self.assertEqual(self.platform.config['environment_ids'], [str(self.provider_environment.pk)])

    def test_provider_membership_revocation_prevents_approval(self):
        membership = Membership.objects.create(organization=self.provider, user=self.owner_admin, kind='administrator')
        release = prepare_release(self.app, Principal(user=self.owner_admin))
        membership.active = False
        membership.save(update_fields=['active'])
        with patch('platform_services.cloud_aws.start_run') as start:
            with self.assertRaises(PermissionDenied):
                approve_release(release, Principal(user=self.owner_admin))
            start.assert_not_called()

    def test_assignment_revocation_is_seen_by_worker_holding_old_models(self):
        release = prepare_release(self.app, Principal(user=self.operator))
        approve_release(release, Principal(user=self.operator))
        self.assignment.active = False
        self.assignment.save(update_fields=['active'])
        self.assertIsNone(hosted_assignment(self.app))
        with patch('platform_services.cloud_aws.start_run') as start:
            self.assertTrue(process_one())
            start.assert_not_called()
        release.refresh_from_db()
        self.assertEqual(release.status, 'failed')

    def test_tampering_and_normal_cross_owner_creation_are_rejected(self):
        self.assignment.repository = 'Example/private-other'
        with self.assertRaises(ValidationError):
            self.assignment.save()
        with self.assertRaises((ValidationError, PermissionDenied)):
            create_application(self.owner, Principal(user=self.operator), platform=self.platform,
                environment=self.owner_environment, name='Unapproved', slug='other', repository='Example/app',
                branch='main', commit='a' * 40, archetype='amplify-static')
        CloudApplication.objects.filter(pk=self.app.pk).update(repository='Example/private-other')
        self.app.refresh_from_db()
        self.assertIsNone(hosted_assignment(self.app))
        with self.assertRaises(PermissionDenied):
            authorized(Principal(user=self.owner_admin), self.app, 'cloud.console_access')

    def test_provider_tags_do_not_become_owner_authority_but_still_gate_deployment(self):
        tag = PermissionTag.objects.create(organization=self.provider, slug='provider-mfa', name='Provider MFA', requires_mfa=True)
        self.platform.permission_tags.add(tag)
        self.assertNotIn(self.platform, resource_chain(self.app))
        authorized(Principal(user=self.owner_admin), self.app, 'cloud.console_access')
        with self.assertRaises(PermissionDenied):
            prepare_release(self.app, Principal(user=self.operator))

    def test_provider_environment_policy_still_gates_deployment(self):
        tag = PermissionTag.objects.create(organization=self.provider, slug='host-environment-mfa',
            name='Provider environment MFA', requires_mfa=True)
        self.provider_environment.permission_tags.add(tag)
        authorized(Principal(user=self.owner_admin), self.app, 'cloud.console_access')
        with self.assertRaises(PermissionDenied):
            prepare_release(self.app, Principal(user=self.operator))

    def test_environment_name_and_placement_changes_fail_closed(self):
        self.owner_environment.name = 'staging'
        type(self.owner_environment).objects.filter(pk=self.owner_environment.pk).update(name='staging')
        self.assertIsNone(hosted_assignment(self.app))
        with self.assertRaises(ValidationError):
            self.app.full_clean()
