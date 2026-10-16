"""Console security and the complete human-approved simulation flow."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from identity.access import Principal
from identity.models import Membership, Organization, PermissionTag, User
from platform_services.cloud_models import CloudApplication, CloudPlatform, CloudRelease
from platform_services.cloud_deployments import create_application, verify_platform
from platform_services.cloud_portal import ApplicationForm, PlatformForm
from platform_services.environments import create_environment
from platform_services.services import install_core


class CloudPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(slug='cloud-ui', name='Cloud UI')
        cls.other = Organization.objects.create(slug='cloud-ui-other', name='Other organization')
        cls.admin = User.objects.create_user('cloud-ui-admin@example.test', 'a-local-test-password')
        Membership.objects.create(organization=cls.organization, user=cls.admin, kind='administrator')
        cls.member = User.objects.create_user('cloud-ui-member@example.test', 'a-local-test-password')
        Membership.objects.create(organization=cls.organization, user=cls.member)
        install_core(cls.organization)
        cls.environment = create_environment(cls.organization, name='sandbox', namespace='cloud-ui-sandbox', application_origin='https://sandbox.example.test')
        cls.other_environment = create_environment(cls.other, name='sandbox', namespace='other-sandbox', application_origin='https://other.example.test')
        cls.platform = CloudPlatform.objects.create(organization=cls.organization, name='Local simulation', mode='demo', prefix='nce', allowed_repositories=['NorthCountryEngineer/Axolotl_News'], config={'environment_ids': [str(cls.environment.pk)], 'archetypes': ['amplify-static', 'amplify-serverless']})
        cls.platform = verify_platform(cls.platform, Principal(user=cls.admin))
        cls.application = create_application(cls.organization, Principal(user=cls.admin), platform=cls.platform, environment=cls.environment, name='Axolotl practice', slug='axolotl-practice', repository='NorthCountryEngineer/Axolotl_News', branch='main', commit='a' * 40, archetype='amplify-static', public_origin='')

    def setUp(self):
        self.client.force_login(self.admin)

    def url(self, name, pk=None, organization=None):
        kwargs = {'slug': (organization or self.organization).slug}
        if pk is not None:
            kwargs['pk'] = pk
        return reverse(name, kwargs=kwargs)

    def app_data(self, **overrides):
        data = {'name': 'New website', 'slug': 'new-website', 'platform': str(self.platform.pk), 'environment': str(self.environment.pk), 'repository': 'NorthCountryEngineer/Axolotl_News', 'branch': 'main', 'commit': 'b' * 40, 'archetype': 'amplify-static', 'public_origin': ''}
        return data | overrides

    def platform_data(self, **overrides):
        return {'name': 'Demo destination', 'mode': 'demo', 'prefix': 'nce', 'allowed_repositories': 'NorthCountryEngineer/Axolotl_News', 'environments': [str(self.environment.pk)], 'archetypes': ['amplify-static']} | overrides

    def test_human_approved_simulation_console_flow_has_no_provider_calls(self):
        with patch('platform_services.cloud_aws.verify') as aws_verify:
            response = self.client.get(self.url('cloud_application_add'))
            self.assertContains(response, 'Create application')
            self.assertContains(response, 'NorthCountryEngineer/Axolotl_News')
            created = self.client.post(self.url('cloud_application_add'), self.app_data())
            self.assertEqual(created.status_code, 302)
            app = CloudApplication.objects.get(slug='new-website')
            prepared = self.client.post(self.url('cloud_release_prepare', app.pk))
            self.assertEqual(prepared.status_code, 302)
            release = CloudRelease.objects.get(application=app)
            review = self.client.get(self.url('cloud_release', release.pk))
            self.assertContains(review, 'Start simulated deployment')
            self.assertContains(review, 'b' * 40)
            not_approved = self.client.post(self.url('cloud_release_approve', release.pk), {'digest': release.digest})
            self.assertEqual(not_approved.status_code, 400)
            release.refresh_from_db()
            self.assertEqual(release.status, 'draft')
            approved = self.client.post(self.url('cloud_release_approve', release.pk), {'digest': release.digest, 'confirm_deployment': 'on'})
            self.assertEqual(approved.status_code, 302)
            release.refresh_from_db()
            self.assertEqual(release.status, 'queued')
            aws_verify.assert_not_called()

    def test_only_verified_active_configured_destinations_and_repositories_are_offered(self):
        CloudPlatform.objects.create(organization=self.organization, name='Not verified', mode='demo', prefix='nce', allowed_repositories=['Other/Unapproved'], config={'environment_ids': [str(self.environment.pk)], 'archetypes': ['amplify-static']})
        foreign = CloudPlatform.objects.create(organization=self.other, name='Foreign platform', mode='demo', prefix='nce', allowed_repositories=['Other/Private'], config={'environment_ids': [str(self.other_environment.pk)], 'archetypes': ['amplify-static']})
        foreign.active = True
        foreign.save()
        response = self.client.get(self.url('cloud_application_add'))
        self.assertContains(response, 'Local simulation')
        self.assertNotContains(response, 'Not verified')
        self.assertNotContains(response, 'Other/Unapproved')
        self.assertNotContains(response, 'Foreign platform')
        self.assertNotContains(response, 'Other/Private')
        self.assertNotContains(response, str(self.other_environment.pk))

    def test_server_rejects_cross_platform_repo_environment_and_template_choices(self):
        for overrides in [{'repository': 'Other/Unapproved'}, {'environment': str(self.other_environment.pk)}, {'archetype': 'amplify-container'}, {'commit': 'main'}]:
            with self.subTest(overrides=overrides):
                response = self.client.post(self.url('cloud_application_add'), self.app_data(**overrides))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(CloudApplication.objects.filter(slug='new-website').exists())
        response = self.client.post(self.url('cloud_application_add'), self.app_data(public_origin='https://safe.example.test/?token=private'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CloudApplication.objects.filter(slug='new-website').exists())

    def test_choices_from_another_ready_platform_cannot_bypass_selected_platform_policy(self):
        staging = create_environment(self.organization, name='staging', namespace='cloud-ui-staging', application_origin='https://staging.example.test')
        second = CloudPlatform.objects.create(organization=self.organization, name='Second destination', mode='demo', prefix='nce', allowed_repositories=['Other/Approved'], config={'environment_ids': [str(staging.pk)], 'archetypes': ['amplify-serverless']})
        verify_platform(second, Principal(user=self.admin))
        self.platform.config['archetypes'] = ['amplify-static']
        self.platform.save()
        self.platform = verify_platform(self.platform, Principal(user=self.admin))
        for overrides in [{'repository': 'Other/Approved'}, {'environment': str(staging.pk)}, {'archetype': 'amplify-serverless'}]:
            with self.subTest(overrides=overrides):
                response = self.client.post(self.url('cloud_application_add'), self.app_data(**overrides))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(CloudApplication.objects.filter(slug='new-website').exists())

    def test_all_resource_mutations_require_post_and_csrf(self):
        prepared = self.client.post(self.url('cloud_release_prepare', self.application.pk))
        self.assertEqual(prepared.status_code, 302)
        release = CloudRelease.objects.get(application=self.application)
        actions = [('cloud_platform_verify', self.platform.pk), ('cloud_release_prepare', self.application.pk), ('cloud_release_approve', release.pk), ('cloud_release_cancel', release.pk)]
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.admin)
        for name, pk in actions:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(self.url(name, pk)).status_code, 405)
                self.assertEqual(secured.post(self.url(name, pk), {'confirm_deployment': 'on', 'digest': release.digest}).status_code, 403)

    def test_other_tenant_and_non_admin_cannot_access_cloud_console(self):
        self.assertEqual(self.client.get(self.url('cloud_application', self.application.pk, self.other)).status_code, 403)
        self.client.force_login(self.member)
        for name, pk in [('cloud_platforms', None), ('cloud_application_add', None), ('cloud_application', self.application.pk)]:
            self.assertEqual(self.client.get(self.url(name, pk)).status_code, 403)

    def test_tag_protected_platform_does_not_leak_into_choices_or_details(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug='private-destination', name='Private destination')
        self.platform.permission_tags.add(tag)
        self.assertEqual(self.client.get(self.url('cloud_platform_edit', self.platform.pk)).status_code, 403)
        response = self.client.get(self.url('cloud_application_add'))
        self.assertNotContains(response, 'Local simulation')
        self.assertContains(response, 'Configure a destination first')

    def test_platform_edit_invalidates_verification_and_external_id_is_write_only(self):
        self.platform.external_id = 'private-role-external-id'
        self.platform.save()
        verify_platform(self.platform, Principal(user=self.admin))
        page = self.client.get(self.url('cloud_platform_edit', self.platform.pk))
        self.assertNotContains(page, 'private-role-external-id')
        saved = self.client.post(self.url('cloud_platform_edit', self.platform.pk), self.platform_data(name='Changed destination'))
        self.assertEqual(saved.status_code, 302)
        self.platform.refresh_from_db()
        self.assertFalse(self.platform.active)
        self.assertIsNone(self.platform.verified_at)
        self.assertEqual(self.platform.external_id, 'private-role-external-id')

    def test_ci_credential_is_masked_preserved_replaced_and_explicitly_removable(self):
        token = 'github_pat_' + 'x' * 40
        self.platform.ci_github_token = token
        self.platform.save()
        page = self.client.get(self.url('cloud_platform_edit', self.platform.pk))
        self.assertNotContains(page, token)
        self.assertContains(page, 'A credential is currently configured.')
        saved = self.client.post(self.url('cloud_platform_edit', self.platform.pk), self.platform_data())
        self.assertEqual(saved.status_code, 302)
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.ci_github_token, token)
        replacement = 'github_pat_' + 'y' * 40
        saved = self.client.post(self.url('cloud_platform_edit', self.platform.pk), self.platform_data(ci_github_token=replacement))
        self.assertEqual(saved.status_code, 302)
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.ci_github_token, replacement)
        self.assertNotIn(replacement, self.platform.encrypted_ci_github_token)
        invalid = self.client.post(self.url('cloud_platform_edit', self.platform.pk), self.platform_data(ci_github_token=replacement, clear_ci_github_token='on'))
        self.assertEqual(invalid.status_code, 200)
        self.assertNotContains(invalid, replacement)
        self.assertContains(invalid, 'Choose either replacement or removal')
        saved = self.client.post(self.url('cloud_platform_edit', self.platform.pk), self.platform_data(clear_ci_github_token='on'))
        self.assertEqual(saved.status_code, 302)
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.encrypted_ci_github_token, '')
        self.assertEqual(self.platform.ci_github_token, '')

    def test_verify_provider_failure_never_renders_raw_credentials_or_traceback(self):
        with patch('platform_services.cloud_portal.deployments.verify_platform', side_effect=ValidationError('secret-provider-token-123456')):
            response = self.client.post(self.url('cloud_platform_verify', self.platform.pk), follow=True)
        self.assertContains(response, 'Verification failed')
        self.assertNotContains(response, 'secret-provider-token-123456')
        self.assertNotContains(response, 'Traceback')

    def test_review_never_renders_provider_state_or_secret_outputs(self):
        self.client.post(self.url('cloud_release_prepare', self.application.pk))
        release = CloudRelease.objects.get(application=self.application)
        release.provider_state = {'credentials': 'private-provider-state', 'signed_url': 'https://secret.example.test/?token=private'}
        release.outputs = {'client_secret': 'private-client-secret', 'access_key': 'private-access-key', 'application_url': 'https://public.example.test', 'api_url': 'https://signed.example.test/?token=private-signed-output'}
        release.save()
        response = self.client.get(self.url('cloud_release', release.pk))
        self.assertContains(response, 'https://public.example.test')
        for private in ['private-provider-state', 'private-client-secret', 'private-access-key', 'secret.example.test', 'private-signed-output', 'signed.example.test']:
            self.assertNotContains(response, private)

    def test_approval_requires_current_review_fingerprint(self):
        self.client.post(self.url('cloud_release_prepare', self.application.pk))
        release = CloudRelease.objects.get(application=self.application)
        response = self.client.post(self.url('cloud_release_approve', release.pk), {'digest': 'c' * 64, 'confirm_deployment': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'The review changed', status_code=400)
        release.refresh_from_db()
        self.assertEqual(release.status, 'draft')

    def test_application_source_can_change_but_destination_fields_cannot_be_forged(self):
        data = self.app_data(name='Renamed application', slug='malicious-new-slug', platform=str(self.platform.pk), environment=str(self.other_environment.pk), commit='c' * 40)
        response = self.client.post(self.url('cloud_application_edit', self.application.pk), data)
        self.assertEqual(response.status_code, 302)
        self.application.refresh_from_db()
        self.assertEqual(self.application.name, 'Renamed application')
        self.assertEqual(self.application.commit, 'c' * 40)
        self.assertEqual(self.application.slug, 'axolotl-practice')
        self.assertEqual(self.application.environment_id, self.environment.pk)

    def test_aws_platform_requires_onboarding_outputs_without_accepting_access_keys(self):
        form = PlatformForm(self.platform_data(mode='aws'), organization=self.organization, principal=Principal(user=self.admin))
        self.assertFalse(form.is_valid())
        self.assertIn('role_arn', form.errors)
        self.assertIn('permissions_boundary_arn', form.errors)
        self.assertNotIn('aws_access_key_id', form.fields)
        self.assertNotIn('aws_secret_access_key', form.fields)
        self.assertNotIn('github_token', form.fields)
