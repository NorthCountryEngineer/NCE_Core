from datetime import timedelta
import copy
import json
import secrets
from unittest.mock import Mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase, Client, LiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from core_client import CoreClient, CoreServiceError
from integration_client import ApplicationIntegrations, IntegrationDenied
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, Role, User
from platform_services.agent_identity import issue_credential
from platform_services.environments import create_environment
from platform_services.integration_models import ApplicationIntegration, IntegrationConnection, IntegrationDefinition
from platform_services.integration_portal import SCHEMA
from platform_services.integration_validation import validate_configuration, validate_configuration_schema
from platform_services.integrations import authorize, publish_definition, replace_credential
from platform_services.models import AgentBinding, AgentDefinition, AgentTask, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential


class IntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Integration customer', slug='integration-customer')
        cls.foreign = Organization.objects.create(name='Other customer', slug='integration-other')
        install_core(cls.org)
        cls.admin = User.objects.create_user('integration-admin@example.test')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        cls.member = User.objects.create_user('integration-user@example.test')
        cls.membership = Membership.objects.create(organization=cls.org, user=cls.member)
        cls.environment = create_environment(cls.org, name='sandbox', namespace='integration-sandbox', application_origin='https://app.example.test')
        cls.membership.access_tags.add(cls.environment.boundary_tag)
        cls.production = create_environment(cls.org, name='production', namespace='integration-production', application_origin='https://prod.example.test')
        cls.package = MarketplacePackage.objects.create(slug='integration-consumer', name='Consumer application', kind='application', description='Consumer',
            manifest={'image': 'registry.example.test/app@sha256:' + 'a' * 64})
        cls.installation = install_package(cls.org, cls.package)
        cls.node = RuntimeNode.objects.create(organization=cls.org, name='Sandbox runtime', namespace=cls.environment.namespace,
            environment=cls.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        cls.deployment = create_deployment(cls.installation, cls.node, 'https://app.example.test')
        cls.credential, cls.token = issue_workload_credential(cls.installation, 'Application backend', environment=cls.environment)
        service = Installation.objects.get(organization=cls.org, package__slug='core-platform').service
        cls.definition = IntegrationDefinition(organization=cls.org, service=service, name='External data application', slug='external-data', configuration_schema=SCHEMA)
        cls.permission = publish_definition(cls.definition, Principal(user=cls.admin))
        cls.connection = IntegrationConnection.objects.create(organization=cls.org, definition=cls.definition, environment=cls.environment,
            configuration={'base_url': 'https://data.example.test/api'})
        cls.secret = secrets.token_urlsafe(48)
        replace_credential(cls.connection, cls.secret)
        cls.connection.save()
        cls.binding = ApplicationIntegration.objects.create(organization=cls.org, installation=cls.installation, definition=cls.definition,
            environment=cls.environment, mode='platform', connection=cls.connection, allow_inheritance=True, workload_access=True,
            allow_application_configuration=True)
        cls.role = Role.objects.create(organization=cls.org, slug='data-consumer', name='Data consumers')
        cls.role.permissions.add(cls.permission)

    def api(self, suffix='', *, values=None, token=None, user_token=None, method=None):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (token or self.token)}
        if user_token:
            headers['HTTP_X_CORE_USER_TOKEN'] = user_token
        return getattr(self.client, method or ('post' if values is not None else 'get'))(
            '/api/platform/v1/integrations/' + suffix, data=json.dumps(values or {}) if values is not None else {},
            content_type='application/json', **headers)

    def oauth(self, user=None, application=None, scope='openid authorization'):
        raw = secrets.token_urlsafe(48)
        token = AccessToken(application=application or self.deployment.application, user=user or self.admin,
            expires=timezone.now() + timedelta(minutes=5), scope=scope)
        set_token_value(token, raw)
        token.save()
        return raw, token

    def update_binding(self, **values):
        ApplicationIntegration.objects.filter(pk=self.binding.pk).update(**values)

    def test_configured_inheritance_is_scoped_and_never_exposes_saved_credentials(self):
        result = self.api('external-data/')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['configuration'], self.connection.configuration)
        self.assertEqual(result.json()['environment'], 'sandbox')
        self.assertTrue(result.json()['credential_configured'])
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertNotIn(self.secret, result.content.decode())
        self.assertEqual(len(self.api().json()['integrations']), 1)
        self.client.force_login(self.admin)
        portal = self.client.get(reverse('integration_settings', args=[self.org.slug]))
        self.assertContains(portal, 'External data application')
        self.assertNotContains(portal, self.secret)
        self.assertNotContains(portal, self.connection.encrypted_credential)

    def test_absent_and_revoked_inheritance_cannot_fall_back_to_application_settings(self):
        self.update_binding(configuration={'base_url': 'https://fallback.example.test'}, allow_inheritance=False)
        self.assertEqual(self.api('external-data/').status_code, 403)
        self.update_binding(allow_inheritance=True)
        IntegrationConnection.objects.filter(pk=self.connection.pk).update(active=False)
        self.assertEqual(self.api('external-data/').status_code, 403)
        self.assertEqual(self.api().json()['integrations'], [])

    def test_explicit_application_configuration_does_not_consume_unused_platform_connection(self):
        self.update_binding(mode='application', configuration={'base_url': 'https://app-owned.example.test'})
        IntegrationConnection.objects.filter(pk=self.connection.pk).update(active=False)
        result = self.api('external-data/')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['configuration']['base_url'], 'https://app-owned.example.test')
        self.assertFalse(result.json()['credential_configured'])

    def test_disabled_binding_and_unapproved_workload_are_denied(self):
        self.update_binding(workload_access=False)
        self.assertEqual(self.api('external-data/').status_code, 403)
        self.assertTrue(AuditEvent.objects.filter(target=str(self.binding.pk), outcome='denied',
            reason_code='application_consumption_required').exists())
        self.update_binding(workload_access=True, mode='disabled')
        self.assertEqual(self.api('external-data/').status_code, 403)

    def test_cross_environment_and_unscoped_workload_are_denied(self):
        _, production_token = issue_workload_credential(self.installation, 'Production', environment=self.production)
        self.assertEqual(self.api('external-data/', token=production_token).status_code, 404)
        _, legacy_token = issue_workload_credential(self.installation, 'Unplaced')
        self.assertEqual(self.api('external-data/', token=legacy_token).status_code, 403)
        self.installation.access_tags.add(self.production.boundary_tag)
        with self.assertRaises(PermissionDenied):
            authorize(Principal(credential=self.credential), ApplicationIntegration.objects.create(organization=self.org,
                installation=self.installation, definition=self.definition, environment=self.production, mode='application',
                configuration={'base_url': 'https://production.example.test'}, workload_access=True))

    def test_other_application_cannot_consume_this_binding(self):
        package = MarketplacePackage.objects.create(slug='other-integration-consumer', name='Other app', kind='application', description='Other', manifest=self.package.manifest)
        installation = install_package(self.org, package)
        _, token = issue_workload_credential(installation, 'Other application', environment=self.environment)
        self.assertEqual(self.api('external-data/', token=token).status_code, 404)
        with self.assertRaises(PermissionDenied):
            authorize(Principal(credential=WorkloadCredential.objects.get(installation=installation)), self.binding)

    def test_held_binding_observes_revocation_and_disabled_providers(self):
        principal = Principal(credential=self.credential)
        authorize(principal, self.binding)
        self.update_binding(workload_access=False)
        with self.assertRaises(PermissionDenied):
            authorize(principal, self.binding)
        self.update_binding(workload_access=True)
        WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=False)
        with self.assertRaises(PermissionDenied):
            authorize(principal, self.binding)

    def test_core_package_and_environment_revocation_are_live(self):
        Installation.objects.filter(organization=self.org, package__slug='core-platform').update(enabled=False)
        self.assertEqual(self.api('external-data/').status_code, 403)
        Installation.objects.filter(organization=self.org, package__slug='core-platform').update(enabled=True)
        type(self.environment).objects.filter(pk=self.environment.pk).update(active=False)
        self.assertEqual(self.api('external-data/').status_code, 403)

    def test_user_role_and_membership_revocations_apply_to_delegated_use(self):
        raw, _ = self.oauth(self.member)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 403)
        self.membership.roles.add(self.role)
        self.membership.access_tags.add(self.environment.boundary_tag)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 200,
            list(AuditEvent.objects.filter(outcome='denied').values('action', 'reason_code', 'tags')))
        self.role.permissions.clear()
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 403)
        self.role.permissions.add(self.permission)
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 401)

    def test_delegation_keeps_both_application_and_user_tag_requirements(self):
        raw, _ = self.oauth()
        tag = PermissionTag.objects.create(organization=self.org, slug='restricted-integration', name='Restricted', allow_workloads=True)
        self.connection.permission_tags.add(tag)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 403)
        self.installation.access_tags.add(tag)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 403)
        self.org.memberships.get(user=self.admin).access_tags.add(tag)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 200,
            list(AuditEvent.objects.filter(outcome='denied').values('action', 'reason_code', 'tags')))

    def test_delegation_rejects_wrong_service_environment_scope_and_expiry(self):
        node = RuntimeNode.objects.create(organization=self.org, environment=self.production, namespace=self.production.namespace,
            name='Production runtime', credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(self.installation, node, 'https://prod.example.test')
        raw, _ = self.oauth(application=deployment.application)
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 401)
        raw, token = self.oauth(scope='openid')
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 401)
        raw, token = self.oauth()
        token.expires = timezone.now() - timedelta(seconds=1)
        token.save()
        self.assertEqual(self.api('external-data/', user_token=raw).status_code, 401)

    def test_application_admin_can_configure_without_changing_platform_grants(self):
        raw, _ = self.oauth()
        result = self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'mode': 'application',
            'configuration': {'base_url': 'https://app-specific.example.test'}})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['revision'], 2)
        self.assertNotIn('encrypted_credential', result.json())
        self.assertEqual(self.api('external-data/').json()['configuration']['base_url'], 'https://app-specific.example.test')
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values={'revision': 2, 'credential_access': True}).status_code, 400)
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'mode': 'disabled'}).status_code, 409)

    def test_configuration_delegation_cannot_be_assumed_by_workloads_or_members(self):
        values = {'revision': 1, 'mode': 'disabled'}
        self.assertEqual(self.api('external-data/settings/', values=values).status_code, 403)
        raw, _ = self.oauth(self.member)
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values=values).status_code, 403)
        raw, _ = self.oauth()
        self.update_binding(allow_application_configuration=False)
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values=values).status_code, 403)
        self.assertEqual(self.api('external-data/settings/', user_token=raw).status_code, 403)

    def test_app_admin_cannot_change_central_connection_or_create_inheritance_grants(self):
        raw, _ = self.oauth()
        self.update_binding(allow_inheritance=False, mode='disabled')
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'mode': 'platform'}).status_code, 403)
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'connection': str(self.connection.pk)}).status_code, 400)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.configuration['base_url'], 'https://data.example.test/api')

    def test_vault_requires_explicit_backend_grant_and_never_records_secrets(self):
        self.assertEqual(self.api('external-data/credentials/', method='post').status_code, 403)
        self.update_binding(credential_access=True)
        result = self.api('external-data/credentials/', values={'revision': 1, 'connection_revision': 1})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['credential'], self.secret)
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertNotIn(self.secret, str(list(AuditEvent.objects.filter(organization=self.org).values())))
        self.update_binding(credential_access=False)
        self.assertEqual(self.api('external-data/credentials/', method='post').status_code, 403)

    def test_credential_change_is_encrypted_status_only_and_endpoint_reuse_requires_review(self):
        raw, _ = self.oauth()
        secret = secrets.token_urlsafe(48)
        result = self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'mode': 'application',
            'configuration': {'base_url': 'https://local.example.test/api'}, 'credential': secret})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertNotIn(secret, result.content.decode())
        self.binding.refresh_from_db()
        self.assertNotIn(secret, self.binding.encrypted_credential)
        encrypted = self.binding.encrypted_credential
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values={'revision': 2, 'credential': ''}).status_code, 200)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.encrypted_credential, encrypted)
        values = {'revision': 3, 'configuration': {'base_url': 'https://another.example.test/api'}}
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values=values).status_code, 400)
        values['clear_credential'] = True
        self.assertEqual(self.api('external-data/settings/', user_token=raw, values=values).status_code, 200)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.encrypted_credential, '')

    def test_changed_configuration_cannot_supply_credentials_to_an_old_endpoint_snapshot(self):
        self.update_binding(credential_access=True)
        IntegrationConnection.objects.filter(pk=self.connection.pk).update(revision=2,
            configuration={'base_url': 'https://changed.example.test'})
        result = self.api('external-data/credentials/', values={'revision': 1, 'connection_revision': 1})
        self.assertEqual(result.status_code, 409)
        self.assertNotIn(self.secret, result.content.decode())
        self.assertEqual(self.api('external-data/credentials/', values={'revision': True, 'connection_revision': 2}).status_code, 400)

    def test_cross_tenant_connection_placement_and_schema_identity_are_rejected(self):
        connection = IntegrationConnection(organization=self.foreign, definition=self.definition,
            environment=self.environment, configuration=self.connection.configuration)
        with self.assertRaises(ValidationError):
            connection.full_clean()
        self.binding.environment = self.production
        with self.assertRaises(ValidationError):
            self.binding.full_clean()
        self.definition.configuration_schema = {'type': 'object', 'additionalProperties': False, 'properties': {}}
        with self.assertRaises(ValidationError):
            self.definition.full_clean()

    def test_empty_configuration_and_full_length_display_names_are_supported(self):
        definition = IntegrationDefinition(organization=self.org, service=self.definition.service, name='A' * 160,
            slug='empty-config', configuration_schema={'type': 'object', 'additionalProperties': False, 'properties': {}})
        permission = publish_definition(definition, Principal(user=self.admin))
        self.assertEqual(len(permission.name), 160)
        connection = IntegrationConnection(organization=self.org, definition=definition, environment=self.environment, configuration={})
        connection.full_clean()
        connection.save()

    def test_invalid_configuration_cannot_echo_credentials_or_store_them_as_properties(self):
        secret = secrets.token_urlsafe(48)
        raw, _ = self.oauth()
        result = self.api('external-data/settings/', user_token=raw, values={'revision': 1, 'mode': 'application',
            'configuration': {'base_url': 'https://local.example.test', 'api_key': secret}})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn(secret, result.content.decode())
        self.update_binding(mode='disabled', configuration={'api_key': secret})
        self.binding.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.binding.full_clean()

    def test_agent_requires_live_service_role_and_cannot_retrieve_credentials_or_administer(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='integration-agent', name='Integration agent', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='sandbox')
        _, token = issue_credential(binding, Principal(user=self.admin))
        self.assertEqual(self.api('external-data/', token=token).status_code, 403)
        binding.roles.add(self.role)
        self.assertEqual(self.api('external-data/', token=token).status_code, 200)
        self.update_binding(credential_access=True)
        self.assertEqual(self.api('external-data/credentials/', token=token, method='post').status_code, 403)
        self.assertEqual(self.api('external-data/settings/', token=token, values={'revision': 1, 'mode': 'disabled'}).status_code, 403)
        binding.roles.clear()
        self.assertEqual(self.api('external-data/', token=token).status_code, 403)

    def test_agent_task_scope_requires_the_explicit_consumer_binding_id(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='scoped-integration-agent', name='Scoped integration agent', kind='inspect')
        task = AgentTask.objects.create(organization=self.org, installation=self.installation, environment='sandbox', name='Scoped task',
            description='Integration resource scope', approved_by=self.admin, expires_at=timezone.now() + timedelta(hours=1), resources={})
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='sandbox', task=task)
        binding.roles.add(self.role)
        _, token = issue_credential(binding, Principal(user=self.admin))
        self.assertEqual(self.api('external-data/', token=token).status_code, 403)
        task.resources = {'integrations': [str(self.binding.pk)]}
        task.full_clean()
        task.save()
        self.assertEqual(self.api('external-data/', token=token).status_code, 200)
        task.resources = {}
        task.save()
        self.assertEqual(self.api('external-data/', token=token).status_code, 403)

    def test_provider_installation_lifecycle_revokes_application_publication(self):
        definition = IntegrationDefinition(organization=self.org, service=self.installation.service, provider=self.installation,
            kind='application', slug='published-app', name='Published application', configuration_schema=SCHEMA)
        publish_definition(definition, Principal(user=self.admin))
        binding = ApplicationIntegration.objects.create(organization=self.org, installation=self.installation, definition=definition,
            environment=self.environment, mode='application', configuration={'base_url': 'https://app.example.test'}, workload_access=True)
        authorize(Principal(credential=self.credential), binding)
        Installation.objects.filter(pk=self.installation.pk).update(enabled=False)
        with self.assertRaises(PermissionDenied):
            authorize(Principal(credential=self.credential), binding)

    def test_portal_forms_create_catalog_connection_and_disabled_consumer_by_default(self):
        self.client.force_login(self.admin)
        result = self.client.post(reverse('integration_definition_add', args=[self.org.slug]), {'slug': 'another-api', 'name': 'Another API',
            'kind': 'api', 'service': str(self.definition.service_id), 'configuration_schema': json.dumps(SCHEMA), 'active': 'on'})
        self.assertEqual(result.status_code, 302, result.content)
        definition = IntegrationDefinition.objects.get(organization=self.org, slug='another-api')
        self.assertTrue(definition.service.permissions.filter(codename=definition.use_permission).exists())
        result = self.client.post(reverse('integration_connection_add', args=[self.org.slug]), {'definition': str(definition.pk),
            'environment': str(self.environment.pk), 'configuration': json.dumps({'base_url': 'https://another.example.test'}),
            'active': 'on', 'expected_revision': 1})
        self.assertEqual(result.status_code, 302, result.content)
        result = self.client.post(reverse('application_integration_add', args=[self.org.slug]), {'definition': str(definition.pk),
            'installation': str(self.installation.pk), 'environment': str(self.environment.pk), 'mode': 'disabled',
            'configuration': '{}', 'expected_revision': 1})
        self.assertEqual(result.status_code, 302, result.content)
        binding = ApplicationIntegration.objects.get(installation=self.installation, definition=definition)
        self.assertFalse(binding.workload_access)
        self.assertFalse(binding.allow_inheritance)
        self.assertFalse(binding.credential_access)

    def test_portal_preserves_tags_and_csrf_and_rejects_stale_edits(self):
        self.client.force_login(self.admin)
        url = reverse('integration_connection_edit', args=[self.org.slug, self.connection.pk])
        values = {'definition': str(self.definition.pk), 'environment': str(self.environment.pk),
            'configuration': json.dumps(self.connection.configuration), 'active': 'on', 'expected_revision': 1}
        result = self.client.post(url, values)
        self.assertEqual(result.status_code, 302, result.content)
        result = self.client.post(url, values)
        self.assertContains(result, 'Settings changed. Reload before saving.')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(url, values).status_code, 403)
        tag = PermissionTag.objects.create(organization=self.org, slug='human-only-integration', name='Human only', allow_workloads=False)
        self.connection.permission_tags.add(tag)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.api('external-data/').status_code, 403)


class IntegrationContractTests(SimpleTestCase):
    def test_schemas_reject_nested_remote_and_secret_fields(self):
        for field in [{'type': 'object'}, {'type': 'string', '$ref': 'https://remote.example.test/schema'}]:
            schema = copy.deepcopy(SCHEMA)
            schema['properties']['base_url'] = field
            with self.assertRaises(ValidationError):
                validate_configuration_schema(schema)
        schema = copy.deepcopy(SCHEMA)
        schema['properties']['api_key'] = {'type': 'string'}
        with self.assertRaises(ValidationError):
            validate_configuration_schema(schema)
        embedded = 'https://' + secrets.token_urlsafe(16) + ':' + secrets.token_urlsafe(32) + '@api.example.test'
        for url in [embedded, 'https://api.example.test?key=' + secrets.token_urlsafe(32), 'http://api.example.test']:
            with self.assertRaises(ValidationError):
                validate_configuration({'base_url': url}, SCHEMA)

    def test_standalone_requires_local_rbac_and_preserves_local_configuration(self):
        authorize = Mock(return_value=True)
        credentials = Mock(return_value=secrets.token_urlsafe(48))
        app = ApplicationIntegrations(local_configurations={'data': {'base_url': 'https://local.example.test'}},
            authorize_local=authorize, local_credential=credentials)
        self.assertEqual(app.resolve('data', actor='verified-user')['mode'], 'standalone')
        self.assertEqual(app.credential('data', actor='verified-user'), credentials.return_value)
        authorize.return_value = False
        with self.assertRaises(IntegrationDenied):
            app.resolve('data', actor='verified-user')
        with self.assertRaises(IntegrationDenied):
            ApplicationIntegrations(local_configurations={'data': {}}).resolve('data')

    def test_explicit_platform_routes_never_fall_back_when_core_is_unavailable(self):
        core = Mock()
        core.resolve_integration.side_effect = CoreServiceError('Unavailable')
        app = ApplicationIntegrations(mode='hybrid', core=core, inherited=['data'], local_configurations={'data': {'base_url': 'https://fallback.example.test'}})
        with self.assertRaises(CoreServiceError):
            app.resolve('data', user_access_token='opaque-user-token')
        core.resolve_integration.assert_called_once_with('data', user_access_token='opaque-user-token')

    def test_standalone_reads_current_application_settings_for_each_operation(self):
        current = {'base_url': 'https://initial.example.test'}
        def configure(slug, values, actor):
            current.update(values)
            return {'configuration': dict(current)}
        app = ApplicationIntegrations(authorize_local=lambda *args: True, local_configurations=lambda *args: dict(current), configure_local=configure)
        before = app.resolve('data')
        app.configure('data', {'base_url': 'https://changed.example.test'})
        self.assertEqual(app.resolve('data')['configuration']['base_url'], 'https://changed.example.test')
        self.assertEqual(before['configuration']['base_url'], 'https://initial.example.test')

    def test_backend_credential_requires_the_resolved_configuration_snapshot(self):
        core = Mock()
        app = ApplicationIntegrations(mode='platform', core=core)
        with self.assertRaises(ValueError):
            app.credential('data')
        snapshot = {'revision': 3, 'connection_revision': 5}
        app.credential('data', integration=snapshot)
        core.integration_credential.assert_called_once_with('data', integration=snapshot, user_access_token=None)

    def test_configuration_uses_local_admin_policy_or_current_core_admin_token(self):
        setter = Mock(return_value={'revision': 2})
        app = ApplicationIntegrations(authorize_local=lambda slug, actor, action: actor == 'admin', configure_local=setter)
        self.assertEqual(app.configure('data', {'revision': 1}, actor='admin'), {'revision': 2})
        with self.assertRaises(IntegrationDenied):
            app.configure('data', {}, actor='member')
        core = Mock()
        platform = ApplicationIntegrations(mode='platform', core=core)
        with self.assertRaises(IntegrationDenied):
            platform.configure('data', {})
        platform.configure('data', {'revision': 1}, user_access_token='current-admin')
        core.configure_integration.assert_called_once_with('data', {'revision': 1}, user_access_token='current-admin')


class LiveIntegrationTests(LiveServerTestCase):
    def test_portable_client_consumes_real_api_and_observes_revocation(self):
        from django.apps import apps
        from platform_services.catalog import seed_catalog
        seed_catalog(apps, None)
        org = Organization.objects.create(name='Live integration', slug='live-integration')
        install_core(org)
        admin = User.objects.create_user('live-integration@example.test')
        Membership.objects.create(organization=org, user=admin, kind='administrator')
        environment = create_environment(org, name='sandbox', namespace='live-integration', application_origin='https://app.example.test')
        package = MarketplacePackage.objects.create(slug='live-integration-consumer', name='Live app', kind='application', description='Live consumer',
            manifest={'image': 'registry.example.test/app@sha256:' + 'a' * 64})
        installation = install_package(org, package)
        _, raw = issue_workload_credential(installation, 'Live client', environment=environment)
        definition = IntegrationDefinition(organization=org, service=org.services.get(slug='core-platform'), slug='live-data',
            name='Live data', configuration_schema=SCHEMA)
        publish_definition(definition, Principal(user=admin))
        connection = IntegrationConnection.objects.create(organization=org, definition=definition, environment=environment,
            configuration={'base_url': 'https://data.example.test/api'})
        secret = secrets.token_urlsafe(48)
        replace_credential(connection, secret)
        connection.save()
        binding = ApplicationIntegration.objects.create(organization=org, installation=installation, definition=definition, environment=environment,
            connection=connection, mode='platform', allow_inheritance=True, workload_access=True, credential_access=True)
        core = CoreClient(self.live_server_url + '/api/platform/v1', raw, str(org.pk), allow_local_http=True, environment='sandbox')
        routed = ApplicationIntegrations(mode='platform', core=core)
        self.assertEqual(core.integrations()[0]['slug'], 'live-data')
        snapshot = routed.resolve('live-data')
        self.assertEqual(snapshot['configuration'], connection.configuration)
        self.assertEqual(routed.credential('live-data', integration=snapshot), secret)
        binding.workload_access = False
        binding.save()
        with self.assertRaises(CoreServiceError):
            routed.resolve('live-data')
