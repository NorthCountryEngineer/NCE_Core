from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
from unittest.mock import Mock
import zipfile

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, LiveServerTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app_factory_client import AppFactoryTools
from identity.access import Principal, decision
from identity.models import Membership, Organization, PermissionTag, ProtectedResource, User
from platform_services.app_factory import (DEFAULT_SPEC, ROLES, approve_build, artifacts, create_project, digest,
    execute_step, release, source_archive, validate_specification)
from platform_services.catalog import seed_catalog
from platform_services.environments import create_environment
from platform_services.models import AgentBinding, AppProject, DeploymentEnvironment, Installation, RunStep, WorkflowRun
from platform_services.services import install_core, issue_workload_credential
from platform_services.workflows import process_one
import importlib


class AppFactoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_catalog(apps, None)
        importlib.import_module('platform_services.migrations.0020_app_factory_catalog').seed_factory(apps, None)
        cls.org = Organization.objects.create(name='Factory organization', slug='factory-test')
        cls.admin = User.objects.create_user(email='factory-admin@example.test')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        install_core(cls.org, cls.admin)
        cls.environment = create_environment(cls.org, name='sandbox', namespace='factory-sandbox', application_origin='https://factory.example.test')
        cls.principal = Principal(user=cls.admin)
        cls.project = create_project(cls.org, cls.environment, 'Review tracker', 'Track review requests and their status.', DEFAULT_SPEC, cls.principal)
        cls.factory = Installation.objects.get(organization=cls.org, package__slug='core-app-factory')

    def build(self):
        run = approve_build(self.project, self.principal)
        self.assertTrue(process_one())
        run.refresh_from_db()
        self.assertEqual(run.status, 'succeeded', run.error)
        self.project.refresh_from_db()
        return run

    def test_six_distinct_scoped_accounts_and_no_release_permissions(self):
        run = approve_build(self.project, self.principal)
        self.assertEqual(len(run.snapshot), 6)
        self.assertEqual(len({item['account_id'] for item in run.snapshot}), 6)
        for index, step in enumerate(run.snapshot):
            self.assertEqual(step['kind'], 'app_' + ROLES[index])
            binding = AgentBinding.objects.get(pk=step['binding_id'])
            self.assertEqual(binding.task.resources, {'app_projects': [str(self.project.pk)]})
            self.assertEqual(binding.environment, 'sandbox')
            self.assertEqual(set(binding.roles.values_list('permissions__codename', flat=True)), {'agents-run', 'appfactory-build'})
        self.assertEqual(approve_build(self.project, self.principal).pk, run.pk)

    def test_source_archive_and_generated_runtime_tests_execute(self):
        self.build()
        archive, source_hash = source_archive(self.project, self.principal)
        files, expected = artifacts(self.project, self.principal)
        self.assertEqual(source_hash, expected)
        self.assertEqual(source_hash, digest(files))
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                bundle.extractall(directory)
            result = subprocess.run([str(Path('.venv/bin/python').absolute()), '-m', 'unittest', 'test_server', '-v'],
                cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ran 13 tests', result.stderr)
            result = subprocess.run([str(Path('.venv/bin/python').absolute()), 'ci/source_digest.py'], cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), source_hash)
        self.assertIn('identity_client.py', files)
        self.assertIn('security-contract.json', files)
        self.assertIn('workflow_dispatch', files['.github/workflows/verify.yml'])

    def test_revoked_agent_prevents_build(self):
        run = approve_build(self.project, self.principal)
        AgentBinding.objects.filter(pk=run.snapshot[2]['binding_id']).update(active=False)
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.step_results.count(), 0)

    def test_revoked_human_admin_prevents_build(self):
        run = approve_build(self.project, self.principal)
        Membership.objects.filter(organization=self.org, user=self.admin).update(active=False)
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')

    def test_expired_task_prevents_export(self):
        run = self.build()
        run.task.expires_at = timezone.now() - timedelta(seconds=1)
        run.task.save(update_fields=['expires_at'])
        with self.assertRaises(PermissionDenied):
            source_archive(self.project, self.principal)

    def test_project_task_scope_denies_other_projects(self):
        run = approve_build(self.project, self.principal)
        other = create_project(self.org, self.environment, 'Other', 'Other project', DEFAULT_SPEC, self.principal)
        agent = Principal(agent_binding=AgentBinding.objects.get(pk=run.snapshot[0]['binding_id']))
        self.assertTrue(decision(agent, self.project)[0])
        self.assertFalse(decision(agent, other)[0])

    def test_production_and_cross_organization_development_denied(self):
        production = create_environment(self.org, name='production', namespace='factory-prod', application_origin='https://factory.example.test')
        with self.assertRaises(ValidationError):
            create_project(self.org, production, 'Production', 'Do not develop here', DEFAULT_SPEC, self.principal)
        other = Organization.objects.create(name='Other', slug='other-factory')
        with self.assertRaises((PermissionDenied, ValidationError)):
            create_project(other, self.environment, 'Other', 'Wrong organization', DEFAULT_SPEC, self.principal)

    def test_classification_that_excludes_agents_blocks_build_atomically(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='human-only', name='Human only', allow_workloads=False)
        self.project.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            approve_build(self.project, self.principal)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.build_id)
        self.assertEqual(self.org.agent_accounts.count(), 0)

    def test_post_approval_specification_change_denies_execution(self):
        run = approve_build(self.project, self.principal)
        AppProject.objects.filter(pk=self.project.pk).update(description='Changed after review')
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')

    def test_agent_cannot_inject_files_or_execute_tools_outside_approved_run(self):
        run = approve_build(self.project, self.principal)
        step = run.snapshot[0]
        agent = Principal(agent_binding=AgentBinding.objects.get(pk=step['binding_id']))
        with self.assertRaises(PermissionDenied):
            execute_step(self.org, step, {'project_id': str(self.project.pk), 'files': {'../../escape.py': 'malicious'}},
                agent, f'run:{run.pk}:step:0')
        with self.assertRaises(PermissionDenied):
            execute_step(self.org, step, {'project_id': str(self.project.pk)}, agent, 'run:another:step:0')

    def test_release_registers_private_package_live_permissions_and_collection_policies(self):
        self.build()
        _, source_hash = artifacts(self.project, self.principal)
        image = 'registry.example.test/review-tracker@sha256:' + secrets.token_hex(32)
        package = release(self.project, self.principal, image, source_hash, 'Reviewed local test run and matching image provenance')
        self.assertEqual(package.organization_id, self.org.pk)
        installation = Installation.objects.get(package=package)
        self.assertEqual(set(installation.service.permissions.values_list('codename', flat=True)), {'app-read', 'app-write', 'docs-read'})
        guide = installation.documentation_pages.get(slug='application-guide')
        self.assertEqual(guide.source.package_id, package.pk)
        self.assertEqual(guide.source.version, installation.version)
        self.assertIn('GET /api/records/items', guide.source.markdown)
        self.assertEqual(guide.source.markdown, artifacts(self.project, self.principal)[0]['APPLICATION.md'])
        collection = ProtectedResource.objects.get(service=installation.service, resource_type='app_collection', external_id='items')
        self.assertIn(self.environment.boundary_tag, collection.permission_tags.all())
        self.assertFalse(installation.deployments.exists())
        self.assertEqual(release(self.project, self.principal, image, source_hash, 'Same evidence').pk, package.pk)

    def test_release_requires_human_current_permissions_exact_digest_and_image(self):
        run = self.build()
        _, source_hash = artifacts(self.project, self.principal)
        image = 'registry.example.test/app@sha256:' + secrets.token_hex(32)
        agent = Principal(agent_binding=AgentBinding.objects.get(pk=run.snapshot[-1]['binding_id']))
        with self.assertRaises(PermissionDenied):
            release(self.project, agent, image, source_hash, 'Tests')
        for supplied_image, supplied_hash, evidence in [(image, 'wrong', 'Tests'), ('app:latest', source_hash, 'Tests'), (image, source_hash, '')]:
            with self.assertRaises(ValidationError):
                release(self.project, self.principal, supplied_image, supplied_hash, evidence)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.package_id)

    def test_tampered_artifact_fails_integrity_check(self):
        run = self.build()
        run.output['files']['server.py'] = 'tampered'
        run.save(update_fields=['output'])
        with self.assertRaises(ValidationError):
            artifacts(self.project, self.principal)

    def test_even_identical_specifications_cannot_substitute_another_projects_build(self):
        run = self.build()
        other = create_project(self.org, self.environment, self.project.name, self.project.description, DEFAULT_SPEC, self.principal)
        AppProject.objects.filter(pk=self.project.pk).update(build=None)
        AppProject.objects.filter(pk=other.pk).update(build=run)
        with self.assertRaises(ValidationError):
            artifacts(other, self.principal)

    def test_portal_approval_and_download_require_csrf_and_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('app_factory_project', args=[self.org.slug, self.project.pk]))
        self.assertContains(response, 'Approve specification')
        self.assertEqual(self.client.get(reverse('app_factory_approve', args=[self.org.slug, self.project.pk])).status_code, 405)
        from django.test import Client
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(reverse('app_factory_approve', args=[self.org.slug, self.project.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('app_factory_approve', args=[self.org.slug, self.project.pk])).status_code, 302)
        process_one()
        self.assertEqual(self.client.get(reverse('app_factory_download', args=[self.org.slug, self.project.pk])).status_code, 200)

    def test_ai_tools_are_provider_neutral_and_cannot_change_host_placement_or_approve(self):
        core = Mock(environment='sandbox')
        tools = AppFactoryTools(core, 'sandbox')
        values = {'name': 'Example', 'description': 'Describe app', 'specification': deepcopy(DEFAULT_SPEC)}
        tools.execute('core_app_create', values)
        self.assertEqual(core.request.call_args.kwargs['json']['environment'], 'sandbox')
        for tool_name, arguments in [('core_app_approve', values), ('core_app_create', {**values, 'environment': 'production'})]:
            with self.assertRaises(ValueError):
                tools.execute(tool_name, arguments)
        from jsonschema import Draft202012Validator
        for tool in tools.tools():
            Draft202012Validator.check_schema(tool['function']['parameters'])

    def test_workload_api_fixes_environment_and_authorization(self):
        self.factory.granted_capabilities = ['appfactory.create', 'appfactory.read']
        self.factory.save(update_fields=['granted_capabilities'])
        _, token = issue_workload_credential(self.factory, 'Factory consumer', self.environment)
        url = '/api/platform/v1/app-factory/projects/'
        values = {'name': 'AI draft', 'description': 'Created by AI', 'specification': DEFAULT_SPEC, 'environment': 'sandbox'}
        result = self.client.post(url, json.dumps(values), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(result.status_code, 201, result.content)
        project_id = result.json()['id']
        self.assertEqual(self.client.get(url + project_id + '/', HTTP_AUTHORIZATION='Bearer ' + token).status_code, 200)
        self.assertEqual(self.client.get(url + project_id + '/').status_code, 401)
        other_env = create_environment(self.org, name='development', namespace='factory-dev', application_origin='https://factory.example.test')
        result = self.client.post(url, json.dumps({**values, 'environment': other_env.name}), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(result.status_code, 403)

    def test_specifications_are_bounded_data_and_cannot_request_code_or_network(self):
        for value in [{}, {'resources': [], 'shell': 'execute'}, {'resources': [{'slug': '../escape', 'name': 'Escaped', 'fields': {'field': 'string'}}]},
                      {'resources': [{'slug': 'item', 'name': 'Items', 'fields': {'run': {'code': 'execute'}}}]}]:
            with self.assertRaises(ValidationError):
                validate_specification(value)


@override_settings(DEBUG=True, ALLOWED_HOSTS=['localhost', 'testserver'])
class GeneratedServiceLiveTests(LiveServerTestCase):
    def setUp(self):
        issuer = override_settings(ISSUER=self.live_server_url)
        issuer.enable()
        self.addCleanup(issuer.disable)

    def test_generated_service_checks_actual_core_tokens_resource_tags_and_revocation(self):
        from identity.models import Role
        from identity_client import ResourceClient
        from oauth2_provider.models import AccessToken, set_token_value
        from platform_services.models import RuntimeNode, WorkloadCredential
        from platform_services.services import create_deployment, issue_workload_credential
        from platform_services.secrets import decrypt_bundle
        import importlib.util
        seed_catalog(apps, None)
        importlib.import_module('platform_services.migrations.0020_app_factory_catalog').seed_factory(apps, None)
        organization = Organization.objects.create(name='Live app test', slug='live-generated-app')
        admin = User.objects.create_user(email='live-app-admin@example.test')
        Membership.objects.create(organization=organization, user=admin, kind='administrator')
        install_core(organization, admin)
        environment = create_environment(organization, name='sandbox', namespace='live-generated-app', application_origin='https://app.example.test')
        project = create_project(organization, environment, 'Live tracker', 'Track items', DEFAULT_SPEC, Principal(user=admin))
        approve_build(project, Principal(user=admin))
        process_one()
        files, source_hash = artifacts(project, Principal(user=admin))
        package = release(project, Principal(user=admin), 'registry.example.test/app@sha256:' + secrets.token_hex(32), source_hash, 'Disposable live test fixture')
        installation = Installation.objects.get(package=package)
        node = RuntimeNode.objects.create(organization=organization, environment=environment, namespace=environment.namespace,
            name='Disposable runtime', credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(installation, node, 'https://app.example.test', admin, desired_state='running')
        user = User.objects.create_user(email='live-app-member@example.test')
        membership = Membership.objects.create(organization=organization, user=user)
        membership.access_tags.add(environment.boundary_tag)
        role = Role.objects.create(organization=organization, slug='app-user', name='App user')
        role.permissions.set(installation.service.permissions.filter(codename__in=['app-read', 'app-write']))
        membership.roles.add(role)
        token = secrets.token_urlsafe(48)
        access = AccessToken(application=deployment.application, user=user, expires=timezone.now() + timedelta(minutes=5), scope='openid authorization')
        set_token_value(access, token)
        access.save()
        with tempfile.TemporaryDirectory() as directory:
            for name, contents in files.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            spec = importlib.util.spec_from_file_location('generated_live_service', Path(directory) / 'server.py')
            server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(server)
            server.DATABASE = directory + '/records.sqlite3'
            server.IDENTITY = ResourceClient(self.live_server_url, deployment.application.client_id,
                decrypt_bundle(deployment.encrypted_bundle)['oidc_client_secret'], str(organization.pk), str(installation.service_id),
                allow_local_http=True, environment='sandbox', deployment_id=str(deployment.pk))
            def request(method='GET', record=None, path='/api/records/items', query=''):
                payload = json.dumps(record).encode() if record else b''
                environ = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': query, 'HTTP_AUTHORIZATION': 'Bearer ' + token,
                    'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(payload)), 'wsgi.input': io.BytesIO(payload)}
                result = {}
                def start(status, headers):
                    result['status'] = int(status.split()[0])
                result['body'] = json.loads(b''.join(server.application(environ, start)))
                return result
            self.assertEqual(request('POST', {'title': 'Live item', 'status': 'review'})['status'], 201)
            self.assertEqual(len(request()['body']['records']), 1)
            self.assertEqual(request(path='/api/documentation')['status'], 403)
            from core_client import CoreClient
            from unittest.mock import patch
            backend, backend_raw = issue_workload_credential(installation, 'Live documentation backend', environment, deployment)
            installation.granted_capabilities = ['docs.read']
            installation.save()
            platform = Installation.objects.get(organization=organization, package__slug='core-platform')
            role.permissions.add(platform.service.permissions.get(codename='docs-read'), installation.service.permissions.get(codename='docs-read'))
            core_client = CoreClient(self.live_server_url + '/api/platform/v1', backend_raw, str(organization.pk), allow_local_http=True, environment='sandbox')
            with patch.object(server.CoreClient, 'from_environment', return_value=core_client):
                guide = installation.documentation_pages.get(slug='application-guide')
                result = request(path='/api/documentation', query='document_id=' + str(guide.pk))
                self.assertEqual(result['status'], 200, result)
                self.assertIn('Live app test', result['body']['markdown'])
                self.assertEqual(result['body']['environment'], 'sandbox')
                role.permissions.remove(platform.service.permissions.get(codename='docs-read'))
                denied = request(path='/api/documentation', query='document_id=' + str(guide.pk))
                self.assertNotEqual(denied['status'], 200)
                self.assertNotIn('markdown', denied['body'])
            role.permissions.clear()
            self.assertEqual(request()['status'], 403)
            role.permissions.add(installation.service.permissions.get(codename='app-read'))
            collection = ProtectedResource.objects.get(service=installation.service, external_id='items')
            collection.active = False
            collection.save(update_fields=['active'])
            self.assertEqual(request()['status'], 403)
