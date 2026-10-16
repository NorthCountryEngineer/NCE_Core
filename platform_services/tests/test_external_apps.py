"""Real OAuth exchanges, live revocation, and isolated external-app registration."""
import base64
import hashlib
import io
import json
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from identity.models import Membership, Organization, Role, User
from platform_services.environments import create_environment
from platform_services.external_apps import external_public_configuration, external_resource_credentials, register_external_deployment
from platform_services.models import Deployment, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import install_core, install_package
from platform_services.validation import validate_manifest


class ExternalApplicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(slug='external-news', name='External News')
        cls.other = Organization.objects.create(slug='other-external', name='Other tenant')
        cls.admin = User.objects.create_user('external-admin@example.test', secrets.token_urlsafe(32))
        Membership.objects.create(organization=cls.organization, user=cls.admin, kind='administrator')
        cls.member = User.objects.create_user('external-editor@example.test', secrets.token_urlsafe(32))
        cls.membership = Membership.objects.create(organization=cls.organization, user=cls.member)
        install_core(cls.organization)
        cls.environment = create_environment(cls.organization, name='production', namespace='external-production',
            application_origin='https://axolotl.example.test')
        cls.other_environment = create_environment(cls.other, name='production', namespace='other-production',
            application_origin='https://other.example.test')
        cls.package_document = json.loads((Path(settings.BASE_DIR) / 'examples/axolotl-news-package.json').read_text())
        cls.package = MarketplacePackage(**cls.package_document)
        cls.package.full_clean()
        cls.package.save()
        cls.installation = install_package(cls.organization, cls.package, actor=cls.admin)
        cls.deployment = register_external_deployment(cls.installation, cls.environment, 'https://axolotl.example.test', cls.admin)
        cls.browser = cls.deployment.application
        cls.resource = cls.deployment.resource_application
        cls.credentials = external_resource_credentials(cls.deployment)
        cls.role = Role.objects.create(organization=cls.organization, slug='news-editor', name='News editor')
        cls.write = cls.installation.service.permissions.get(codename='posts-write')
        cls.publish = cls.installation.service.permissions.get(codename='posts-publish')
        cls.role.permissions.add(cls.write)
        cls.membership.roles.add(cls.role)
        cls.membership.access_tags.add(cls.environment.boundary_tag)

    def basic(self, credentials=None):
        value = credentials or self.credentials
        return {'HTTP_AUTHORIZATION': 'Basic ' + base64.b64encode(
            (value['client_id'] + ':' + value['client_secret']).encode()).decode()}

    def exchange(self):
        self.client.force_login(self.member)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        page = self.client.get('/oauth/authorize/', {'client_id': self.browser.client_id,
            'redirect_uri': self.browser.redirect_uris, 'response_type': 'code', 'scope': 'openid authorization',
            'state': 'opaque-random-state', 'nonce': 'unused-id-token-nonce', 'code_challenge': challenge,
            'code_challenge_method': 'S256'})
        self.assertEqual(page.status_code, 200, page.content.decode())
        consent = {field.name: field.value() or '' for field in page.context['form']}
        consent['allow'] = 'true'
        redirect = self.client.post('/oauth/authorize/', consent)
        self.assertEqual(redirect.status_code, 302, redirect.content.decode())
        query = parse_qs(urlparse(redirect['Location']).query)
        self.assertEqual(query['state'], ['opaque-random-state'])
        result = self.client.post('/oauth/token/', {'grant_type': 'authorization_code', 'code': query['code'][0],
            'client_id': self.browser.client_id, 'redirect_uri': self.browser.redirect_uris, 'code_verifier': verifier},
            HTTP_ORIGIN='https://axolotl.example.test')
        self.assertEqual(result.status_code, 200, result.content.decode())
        self.assertEqual(result['Access-Control-Allow-Origin'], 'https://axolotl.example.test')
        self.assertNotIn('Access-Control-Allow-Credentials', result)
        return result.json()

    def introspect(self, token, credentials=None):
        return self.client.post('/oauth/introspect/', {'token': token}, **self.basic(credentials))

    def test_registration_creates_bound_clients_without_cluster_or_workload(self):
        self.assertEqual(self.deployment.target, 'external')
        self.assertEqual(self.deployment.status, 'registered')
        self.assertIsNone(self.deployment.node_id)
        self.assertEqual(RuntimeNode.objects.filter(organization=self.organization).count(), 0)
        self.assertEqual(WorkloadCredential.objects.filter(installation=self.installation).count(), 0)
        self.assertEqual(self.browser.client_type, 'public')
        self.assertEqual(self.resource.client_type, 'confidential')
        self.assertEqual(self.browser.service_id, self.resource.service_id)
        self.assertEqual(self.browser.allowed_origins, 'https://axolotl.example.test')
        self.assertTrue(check_password(self.credentials['client_secret'], self.resource.client_secret))
        public = external_public_configuration(self.deployment)
        self.assertEqual(public['permissions'], ['posts-write', 'posts-publish'])
        self.assertNotIn('client_secret', json.dumps(public))
        self.assertNotIn(self.credentials['client_secret'], json.dumps(public))
        self.assertEqual(self.membership.roles.count(), 1)
        self.assertFalse(self.role.permissions.filter(codename='posts-publish').exists())

    def test_real_public_pkce_token_is_live_and_id_token_is_not_resource_authority(self):
        tokens = self.exchange()
        claims = self.introspect(tokens['access_token']).json()
        self.assertTrue(claims['active'])
        self.assertEqual(claims['iss'], settings.ISSUER)
        self.assertEqual(claims['client_id'], self.browser.client_id)
        self.assertEqual(claims['organization_id'], str(self.organization.pk))
        self.assertEqual(claims['service_id'], str(self.installation.service_id))
        self.assertEqual(claims['deployment_id'], str(self.deployment.pk))
        self.assertEqual(claims['environment'], 'production')
        self.assertEqual(claims['permissions'], ['posts-write'])
        self.assertEqual(self.introspect(tokens['id_token']).json(), {'active': False})
        self.role.permissions.remove(self.write)
        self.assertEqual(self.introspect(tokens['access_token']).json()['permissions'], [])
        self.membership.active = False
        self.membership.save(update_fields=['active'])
        self.assertEqual(self.introspect(tokens['access_token']).json(), {'active': False})

    def test_external_environment_and_package_revocation_is_immediate(self):
        tokens = self.exchange()
        # A successful machine-credential proof must not cache deployment state.
        self.assertTrue(self.introspect(tokens['access_token']).json()['active'])
        for resource, field in [(self.environment, 'active'), (self.installation, 'enabled'), (self.package, 'active')]:
            setattr(resource, field, False)
            resource.save(update_fields=[field])
            result = self.introspect(tokens['access_token'])
            self.assertIn(result.status_code, [200, 401])
            self.assertFalse(result.json().get('active', False))
            setattr(resource, field, True)
            resource.save(update_fields=[field])
            self.assertTrue(self.introspect(tokens['access_token']).json()['active'])

    def test_external_client_pair_cannot_introspect_another_environment(self):
        tokens = self.exchange()
        sandbox = create_environment(self.organization, name='sandbox', namespace='external-sandbox',
            application_origin='https://sandbox.example.test')
        other = register_external_deployment(self.installation, sandbox, 'https://sandbox.example.test', self.admin)
        self.assertEqual(self.introspect(tokens['access_token'], external_resource_credentials(other)).json(), {'active': False})

    def test_registration_is_idempotent_and_placement_is_immutable(self):
        same = register_external_deployment(self.installation, self.environment, self.deployment.endpoint, self.admin)
        self.assertEqual(same.pk, self.deployment.pk)
        self.assertEqual(external_resource_credentials(same), self.credentials)
        with self.assertRaises(ValidationError):
            register_external_deployment(self.installation, self.environment, 'https://other.example.test', self.admin)
        with self.assertRaises(ValidationError):
            register_external_deployment(self.installation, self.other_environment, 'https://other.example.test', self.admin)
        with self.assertRaises(ValidationError):
            register_external_deployment(self.installation, self.environment, 'http://localhost:5173', self.admin)
        self.deployment.resource_application = self.browser
        with self.assertRaises(ValidationError):
            self.deployment.full_clean()
        for field, value in [('endpoint', 'https://different.example.test'), ('environment', self.other_environment), ('target', 'external-node')]:
            self.deployment.refresh_from_db()
            setattr(self.deployment, field, value)
            with self.assertRaises(ValidationError):
                self.deployment.full_clean()
        for field, value in [('allowed_origins', 'https://different.example.test'), ('redirect_uris', 'https://different.example.test/oauth/callback'), ('client_type', 'confidential')]:
            self.browser.refresh_from_db()
            setattr(self.browser, field, value)
            with self.assertRaises(ValidationError):
                self.browser.full_clean()

    def test_cors_only_allows_exact_secretless_registered_origin(self):
        for path in ['/oauth/token/', '/oauth/revoke/']:
            allowed = self.client.options(path, HTTP_ORIGIN='https://axolotl.example.test',
                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST', HTTP_ACCESS_CONTROL_REQUEST_HEADERS='Content-Type')
            self.assertEqual(allowed.status_code, 204)
            self.assertEqual(allowed['Access-Control-Allow-Origin'], 'https://axolotl.example.test')
            self.assertNotIn('Access-Control-Allow-Credentials', allowed)
            for origin in ['null', 'https://axolotl.example.test.evil.test', 'https://other.example.test']:
                result = self.client.post(path, {'client_id': self.browser.client_id}, HTTP_ORIGIN=origin)
                self.assertEqual(result.status_code, 403)
                self.assertNotIn('Access-Control-Allow-Origin', result)
            secret = self.client.options(path, HTTP_ORIGIN='https://axolotl.example.test',
                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST', HTTP_ACCESS_CONTROL_REQUEST_HEADERS='Authorization')
            self.assertEqual(secret.status_code, 403)
        response = self.client.post('/oauth/token/', {'client_id': self.resource.client_id},
            HTTP_ORIGIN='https://axolotl.example.test', **self.basic())
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', response)

    def test_browser_revoke_with_public_client_invalidates_access(self):
        tokens = self.exchange()
        response = self.client.post('/oauth/revoke/', {'client_id': self.browser.client_id, 'token': tokens['access_token']},
            HTTP_ORIGIN='https://axolotl.example.test')
        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertEqual(response['Access-Control-Allow-Origin'], 'https://axolotl.example.test')
        self.assertEqual(self.introspect(tokens['access_token']).json(), {'active': False})

    def test_management_command_separates_public_config_and_private_file_without_stdout_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / 'package.json'
            package_path.write_text(json.dumps(self.package_document))
            public_path, secret_path = root / 'public.json', root / 'private.json'
            stdout, stderr = io.StringIO(), io.StringIO()
            args = {'organization': self.organization.slug, 'environment': 'production', 'package': str(package_path),
                'origin': self.deployment.endpoint, 'actor_email': self.admin.email,
                'public_config': str(public_path), 'credentials_file': str(secret_path), 'stdout': stdout, 'stderr': stderr}
            call_command('register_external_application', **args)
            self.assertEqual(json.loads(public_path.read_text()), external_public_configuration(self.deployment))
            self.assertEqual(json.loads(secret_path.read_text()), self.credentials)
            self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
            self.assertNotIn(self.credentials['client_secret'], stdout.getvalue() + stderr.getvalue())
            with self.assertRaises(CommandError):
                call_command('register_external_application', **args)

    def test_partial_credential_file_failure_removes_outputs_and_rolls_back_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {**self.package_document, 'slug': 'failed-file-news'}
            package_path = root / 'package.json'
            package_path.write_text(json.dumps(value))
            secret_path = root / 'private.json'
            with patch('platform_services.management.commands.register_external_application.json.dump', side_effect=OSError('disk write failed')):
                with self.assertRaises(CommandError):
                    call_command('register_external_application', organization=self.organization.slug, environment='production',
                        package=str(package_path), origin='https://failed-file.example.test', actor_email=self.admin.email,
                        credentials_file=str(secret_path), stdout=io.StringIO(), stderr=io.StringIO())
            self.assertFalse(secret_path.exists())
            self.assertFalse(MarketplacePackage.objects.filter(slug='failed-file-news').exists())
            self.assertFalse(Deployment.objects.filter(endpoint='https://failed-file.example.test').exists())

    def test_explicit_ssm_output_has_no_private_file_and_failure_rolls_registration_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package.json'
            value = {**self.package_document, 'slug': 'ssm-external-news'}
            path.write_text(json.dumps(value))
            args = {'organization': self.organization.slug, 'environment': 'production', 'package': str(path),
                'origin': 'https://ssm.example.test', 'actor_email': self.admin.email,
                'credentials_parameter': '/axolotl-news/core-resource-client', 'stdout': io.StringIO(), 'stderr': io.StringIO()}
            with patch('deployment.aws_ssm.put_secure_parameter', side_effect=RuntimeError('transfer failed')) as transfer:
                with self.assertRaises(CommandError):
                    call_command('register_external_application', **args)
                transfer.assert_called_once()
            self.assertFalse(MarketplacePackage.objects.filter(slug='ssm-external-news').exists())
            self.assertFalse(Deployment.objects.filter(endpoint='https://ssm.example.test').exists())
            with patch('deployment.aws_ssm.put_secure_parameter', return_value=1) as transfer:
                call_command('register_external_application', **args)
                name, private_json = transfer.call_args.args
                self.assertEqual(name, '/axolotl-news/core-resource-client')
                private = json.loads(private_json)
                self.assertEqual(set(private), {'client_id', 'client_secret'})
                self.assertNotIn(private['client_secret'], args['stdout'].getvalue() + args['stderr'].getvalue())
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_external_manifest_rejects_ambiguous_runtime_and_reserved_permissions(self):
        for manifest in [
            {'external_runtime': 'aws-amplify', 'image': 'x@sha256:' + 'a' * 64},
            {'external_runtime': 'unknown'},
            {'external_runtime': 'aws-amplify', 'permissions': [{'codename': 'admin', 'name': 'Admin'}]},
        ]:
            with self.assertRaises(ValidationError):
                validate_manifest(manifest)

    def test_admin_registration_ui_has_no_kubernetes_requirement_or_private_secret(self):
        self.client.force_login(self.admin)
        url = reverse('deploy_installation', args=[self.organization.slug, self.installation.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('node', page.context['form'].fields)
        response = self.client.post(url, {'environment': str(self.environment.pk), 'endpoint': self.deployment.endpoint})
        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertContains(response, self.browser.client_id)
        self.assertNotContains(response, self.credentials['client_secret'])

    def test_disabling_external_integration_revokes_tokens_without_managing_aws(self):
        tokens = self.exchange()
        self.client.force_login(self.admin)
        response = self.client.post(reverse('deployment_action', args=[self.organization.slug, self.deployment.pk]), {'action': 'stop'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.introspect(tokens['access_token']).json().get('active', False))
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.desired_state, 'stopped')
        self.assertEqual(self.deployment.status, 'disabled')
        self.assertIsNone(self.deployment.node_id)
        response = self.client.post(reverse('deployment_action', args=[self.organization.slug, self.deployment.pk]), {'action': 'start'})
        self.assertEqual(response.status_code, 302)
        self.deployment.refresh_from_db()
        self.assertEqual(self.deployment.desired_state, 'running')
        self.assertEqual(WorkloadCredential.objects.filter(installation=self.installation).count(), 0)
