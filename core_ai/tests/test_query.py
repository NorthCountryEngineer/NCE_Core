from copy import deepcopy
from datetime import timedelta
import json
import secrets
from unittest.mock import patch
import uuid

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from core_client import CoreClient
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, ProtectedResource, Role, ServicePermission, User
from platform_services.ai_rules import DEFAULTS, RuleDenied
from platform_services.catalog import seed_catalog
from platform_services.data import publish
from platform_services.environments import create_environment
from platform_services.models import AIRulePolicy, AIUsageMeter, AIUsageReservation, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential
from core_ai.models import QuerySource
from core_ai.services import catalog, query, validate_query


@override_settings(DEBUG=True)
class QueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_catalog(apps, None)
        cls.org = Organization.objects.create(name='Research', slug='research')
        install_core(cls.org)
        cls.admin = User.objects.create_user('ai-admin@example.test')
        cls.member = User.objects.create_user('ai-user@example.test')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        cls.membership = Membership.objects.create(organization=cls.org, user=cls.member)
        cls.environment = create_environment(cls.org, name='sandbox', namespace='ai-sandbox', application_origin='https://app.example.test')
        cls.membership.access_tags.add(cls.environment.boundary_tag)
        cls.role = Role.objects.create(organization=cls.org, slug='researcher', name='Researcher')
        cls.membership.roles.add(cls.role)
        permissions = ServicePermission.objects.filter(service__organization=cls.org, codename__in=['ai-query', 'data-read', 'fabric-read'])
        cls.role.permissions.add(*permissions)
        package = MarketplacePackage.objects.create(slug='research-app', name='Research Studio', kind='application', description='Query fixture',
            manifest={'image': 'registry.example.test/app@sha256:' + 'b' * 64, 'capabilities': ['ai.query', 'data.read', 'fabric.read']})
        cls.app = install_package(cls.org, package, package.manifest['capabilities'])
        cls.app_permission = ServicePermission.objects.create(service=cls.app.service, codename='read', name='Read')
        cls.role.permissions.add(cls.app_permission)
        from platform_services.models import Dataset
        cls.dataset = Dataset.objects.create(organization=cls.org, slug='reports', name='Reports')
        cls.dataset.permission_tags.add(cls.environment.boundary_tag)
        cls.source = QuerySource.objects.create(organization=cls.org, name='Reports', installation=cls.app,
            environment=cls.environment, dataset=cls.dataset, fields=['title', 'status'], filters={'project': 'shared'}, required_permissions=['read'])
        cls.source.allowed_roles.add(cls.role)
        cls.source.allowed_consumers.add(cls.app)
        cls.private, _ = publish(cls.dataset, 'private', {'title': 'Private report', 'status': 'draft', 'project': 'private'})
        cls.first, _ = publish(cls.dataset, 'first', {'title': 'Shared report', 'status': 'ready', 'project': 'shared', 'secret_field': 'excluded content'})
        cls.second, _ = publish(cls.dataset, 'second', {'title': '<script>alert(1)</script>', 'status': 'ready', 'project': 'shared'})
        cls.node = RuntimeNode.objects.create(organization=cls.org, name='Sandbox', namespace=cls.environment.namespace,
            environment=cls.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        cls.deployment = create_deployment(cls.app, cls.node, 'https://app.example.test')

    def setUp(self):
        self.reader = Principal(user=self.member)

    def run_query(self, **values):
        return query(self.reader, self.org, {'sources': [str(self.source.pk)], **values})

    def oauth(self, user=None):
        raw = secrets.token_urlsafe(48)
        token = AccessToken(application=self.deployment.application, user=user or self.member,
            scope='openid authorization', expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, raw)
        token.save()
        return raw, token

    def api(self, values=None, delegated=True, raw=None):
        if raw is None:
            _, raw = issue_workload_credential(self.app, 'Query adapter', self.environment, self.deployment)
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + raw}
        if delegated:
            headers['HTTP_X_CORE_USER_TOKEN'] = delegated if isinstance(delegated, str) else self.oauth()[0]
        return self.client.post('/api/platform/v1/ai/query/', json.dumps(values or {'sources': [str(self.source.pk)]}),
            content_type='application/json', **headers) if values is not None else self.client.get('/api/platform/v1/ai/sources/', **headers)

    def test_unified_projection_citations_literal_matching_and_no_provider_calls(self):
        with patch('requests.post') as outbound:
            value = self.run_query(text='shared')
        outbound.assert_not_called()
        self.assertEqual([row['record_id'] for row in value['evidence']], [self.first.pk])
        self.assertEqual(value['evidence'][0]['data'], {'title': 'Shared report', 'status': 'ready'})
        self.assertEqual(value['evidence'][0]['citation_id'], f'{self.source.pk}:{self.first.pk}')
        self.assertEqual(value['evidence'][0]['environment'], 'sandbox')
        self.assertFalse(value['provider']['generation_enabled'])
        self.assertIsNone(value['conversation']['answer'])
        self.assertEqual(value['usage']['cost_micros'], 0)
        self.assertEqual(value['usage']['inspected_rows'], 3)
        self.assertFalse(self.run_query(text='excluded content')['evidence'])
        self.assertFalse(self.run_query(text='private')['evidence'])

    def test_inspection_pagination_advances_over_denied_and_filtered_rows(self):
        first = self.run_query(limit=1)
        self.assertFalse(first['evidence'])
        self.assertEqual(first['next_cursors'][str(self.source.pk)], self.private.pk)
        self.assertTrue(first['has_more'])
        second = self.run_query(limit=1, cursors=first['next_cursors'])
        self.assertEqual(second['evidence'][0]['record_id'], self.first.pk)
        last = self.run_query(limit=1, cursors={str(self.source.pk): self.second.pk})
        self.assertFalse(last['has_more'])

    def test_multi_application_sources_share_one_row_budget_and_user_meter(self):
        source = QuerySource.objects.create(organization=self.org, name='Other reports', installation=self.app,
            environment=self.environment, dataset=self.dataset, fields=['title'], required_permissions=['read'])
        source.allowed_roles.add(self.role)
        value = query(self.reader, self.org, {'sources': [str(self.source.pk), str(source.pk)], 'limit': 4})
        self.assertEqual(len(value['sources']), 2)
        self.assertEqual(value['usage']['inspected_rows'], 4)
        meter = AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.member.pk))
        self.assertEqual(meter.used['requests'], 1)
        self.assertEqual(meter.used['query_rows'], 4)
        self.assertEqual(meter.held['query_rows'], 0)

    def test_no_new_role_grants_are_implied_by_marketplace_install(self):
        self.assertFalse(self.org.installations.get(package__slug='core-ai').granted_capabilities)
        self.assertEqual(self.org.installations.get(package__slug='core-ai').deployments.get().target, 'embedded')
        self.role.permissions.remove(ServicePermission.objects.get(service__organization=self.org, service__slug='core-ai', codename='ai-query'))
        with self.assertRaises(PermissionDenied):
            self.run_query()

    def test_user_needs_source_role_application_permission_and_data_capability(self):
        for revoke, restore in [
                (lambda: self.source.allowed_roles.clear(), lambda: self.source.allowed_roles.add(self.role)),
                (lambda: self.role.permissions.remove(self.app_permission), lambda: self.role.permissions.add(self.app_permission)),
                (lambda: self.role.permissions.remove(self.org.services.get(slug='core-data').permissions.get(codename='data-read')),
                 lambda: self.role.permissions.add(self.org.services.get(slug='core-data').permissions.get(codename='data-read')))]:
            revoke()
            self.assertEqual(catalog(self.reader, self.org)['sources'], [])
            with self.assertRaises(PermissionDenied):
                self.run_query()
            restore()

    def test_source_mfa_and_purpose_tags_cannot_be_bypassed(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='restricted-ai', name='Restricted', requires_mfa=True)
        self.source.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        self.assertFalse(catalog(self.reader, self.org)['sources'])
        tag.requires_mfa, tag.allowed_purposes = False, ['research']
        tag.save()
        with self.assertRaises(PermissionDenied):
            self.run_query()
        self.reader.purpose = 'research'
        self.assertEqual(len(self.run_query()['evidence']), 2)

    def test_record_policy_fails_closed_and_rechecks_live_tags(self):
        self.source.record_resource_type = 'report'
        self.source.save()
        self.assertFalse(self.run_query()['evidence'])
        allowed = ProtectedResource.objects.create(service=self.app.service, resource_type='report', external_id=str(self.first.pk), name='Mapped report')
        allowed.permission_tags.add(self.environment.boundary_tag)
        self.assertEqual([row['record_id'] for row in self.run_query()['evidence']], [self.first.pk])
        tag = PermissionTag.objects.create(organization=self.org, slug='row-restricted', name='Restricted row')
        allowed.permission_tags.add(tag)
        self.assertFalse(self.run_query()['evidence'])

    def test_foreign_source_identifier_and_unknown_source_reveal_no_metadata(self):
        outsider = Organization.objects.create(name='Other', slug='other')
        other = QuerySource.objects.create(organization=outsider, name='Hidden source', installation=self.app,
            environment=self.environment, dataset=self.dataset, fields=['title'], required_permissions=['read'])
        for pk in [other.pk, uuid.uuid4()]:
            with self.assertRaises(PermissionDenied):
                query(self.reader, self.org, {'sources': [str(pk)]})
        self.assertNotIn('Hidden source', json.dumps(catalog(self.reader, self.org)))

    def test_cross_tenant_source_configuration_and_unregistered_permission_rejected(self):
        self.source.required_permissions = ['not-registered']
        with self.assertRaises(ValidationError):
            self.source.full_clean()
        self.source.required_permissions = ['read']
        self.source.organization = Organization.objects.create(name='Other', slug='other')
        with self.assertRaises(ValidationError):
            self.source.full_clean()

    def test_shared_quotas_and_inspection_limit_are_enforced_before_fetch(self):
        policy = AIRulePolicy.objects.create(organization=self.org, baseline=deepcopy(DEFAULTS))
        policy.baseline['query']['max_rows'] = 2
        policy.baseline['user_daily']['requests'] = 1
        policy.save()
        self.assertEqual(self.run_query(limit=200)['usage']['inspected_rows'], 2)
        with patch('core_ai.services.fetch') as fetch, self.assertRaises(RuleDenied):
            self.run_query()
        fetch.assert_not_called()

    def test_empty_catalog_is_valid_and_does_not_invoke_a_provider(self):
        self.source.active = False
        self.source.save()
        result = query(self.reader, self.org, {})
        self.assertFalse(result['evidence'])
        self.assertFalse(AIUsageReservation.objects.exists())

    def test_delegated_backend_and_user_both_must_be_authorized(self):
        self.assertEqual(self.api().status_code, 200)
        result = self.api({'sources': [str(self.source.pk)]})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(len(result.json()['evidence']), 2)
        self.assertEqual(self.api(delegated=False).status_code, 403)
        self.source.allowed_consumers.clear()
        self.assertEqual(self.api().json()['sources'], [])
        self.assertEqual(self.api({'sources': [str(self.source.pk)]}).status_code, 403)

    def test_delegation_rejects_wrong_application_and_expired_tokens(self):
        raw, token = self.oauth()
        token.expires = timezone.now() - timedelta(seconds=1)
        token.save()
        self.assertEqual(self.api(delegated=raw).status_code, 401)

    def test_delegated_environment_cannot_expand_to_production(self):
        production = create_environment(self.org, name='production', namespace='ai-production', application_origin='https://prod.example.test')
        self.membership.access_tags.add(production.boundary_tag)
        self.source.environment = production
        self.source.save()
        self.assertEqual(self.api().json()['sources'], [])

    def test_workload_capability_revocation_prevents_query(self):
        self.app.granted_capabilities = ['data.read']
        self.app.save()
        self.assertEqual(self.api().status_code, 403)

    def test_user_revocation_during_retrieval_discards_results_and_settles_usage(self):
        from core_ai.services import fetch as real_fetch
        def revoke(*args):
            result = real_fetch(*args)
            User.objects.filter(pk=self.member.pk).update(is_active=False)
            return result
        with patch('core_ai.services.fetch', side_effect=revoke), self.assertRaises(PermissionDenied):
            self.run_query()
        row = AIUsageReservation.objects.get()
        self.assertEqual(row.state, 'settled')
        self.assertEqual(row.actual['query_rows'], 3)

    def test_policy_change_during_retrieval_discards_old_projection(self):
        from core_ai.services import fetch as real_fetch
        def change(*args):
            result = real_fetch(*args)
            QuerySource.objects.filter(pk=self.source.pk).update(fields=['status'])
            return result
        with patch('core_ai.services.fetch', side_effect=change):
            value = self.run_query()
        self.assertTrue(value['partial'])
        self.assertFalse(value['evidence'])
        self.assertEqual(value['sources'][0]['status'], 'access_changed')

    def test_oauth_revocation_during_io_discards_results(self):
        raw, token = self.oauth()
        from core_ai.services import fetch as real_fetch
        def revoke(*args):
            result = real_fetch(*args)
            token.delete()
            return result
        with patch('core_ai.services.fetch', side_effect=revoke):
            self.assertEqual(self.api({'sources': [str(self.source.pk)]}, delegated=raw).status_code, 403)
        self.assertEqual(AIUsageReservation.objects.get().state, 'settled')

    def test_portal_requires_login_membership_and_csrf_and_escapes_evidence(self):
        url = reverse('ai', args=[self.org.slug])
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(browser.get(url).status_code, 302)
        browser.force_login(self.member)
        page = browser.get(url)
        self.assertContains(page, 'Find evidence')
        values = {'text': '', 'limit': 100, 'sources': [str(self.source.pk)]}
        self.assertEqual(browser.post(url, values).status_code, 403)
        values['csrfmiddlewaretoken'] = browser.cookies['csrftoken'].value
        page = browser.post(url, values)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, 'excluded content')
        self.assertEqual(browser.get(reverse('ai_admin', args=[self.org.slug])).status_code, 403)
        browser.force_login(self.admin)
        self.assertContains(browser.get(reverse('ai_admin', args=[self.org.slug])), 'Add application query source')

    def test_queries_are_not_saved_in_audit_or_accounting(self):
        self.run_query(text='Shared report')
        self.assertNotIn('Shared report', json.dumps(list(AuditEvent.objects.values('target', 'reason_code'))))
        self.assertNotIn('Shared report', json.dumps(list(AIUsageReservation.objects.values('request_key', 'fingerprint', 'actual'))))

    def test_invalid_admin_form_is_a_validation_error_and_cannot_create_source(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('ai_source_add', args=[self.org.slug]), {'name': 'Invalid source',
            'dataset': str(self.dataset.pk), 'fields': '["title"]', 'required_permissions': '["read"]'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This field is required.')
        self.assertEqual(QuerySource.objects.count(), 1)

    def test_oversized_projected_record_retains_cursor_for_remediation(self):
        publish(self.dataset, 'oversize', {'title': 'x' * 40000, 'status': 'ready', 'project': 'shared'})
        value = self.run_query()
        self.assertTrue(value['partial'])
        self.assertFalse(value['evidence'])
        self.assertEqual(value['next_cursors'][str(self.source.pk)], 0)
        self.assertEqual(value['sources'][0]['status'], 'record_too_large')

    def test_insufficient_global_budget_and_more_than_ten_sources_are_rejected(self):
        for index in range(10):
            source = QuerySource.objects.create(organization=self.org, name=f'Source {index}', installation=self.app,
                environment=self.environment, dataset=self.dataset, fields=['title'], required_permissions=['read'])
            source.allowed_roles.add(self.role)
        with self.assertRaises(ValidationError):
            query(self.reader, self.org, {})
        other = QuerySource.objects.exclude(pk=self.source.pk).first()
        with self.assertRaises(ValidationError):
            query(self.reader, self.org, {'sources': [str(self.source.pk), str(other.pk)], 'limit': 1})

    def test_deadline_reports_partial_and_does_not_fetch_after_expiration(self):
        with patch('core_ai.services.monotonic', side_effect=[0, 11]), patch('core_ai.services.fetch') as fetch:
            value = self.run_query()
        fetch.assert_not_called()
        self.assertTrue(value['partial'])
        self.assertFalse(value['evidence'])
        self.assertEqual(value['sources'][0]['status'], 'timeout')
        self.assertEqual(AIUsageReservation.objects.get().state, 'settled')

    def test_source_disabled_after_fetch_discards_its_evidence(self):
        from core_ai.services import fetch as real_fetch
        def disable(*args):
            result = real_fetch(*args)
            QuerySource.objects.filter(pk=self.source.pk).update(active=False)
            return result
        with patch('core_ai.services.fetch', side_effect=disable):
            value = self.run_query()
        self.assertFalse(value['evidence'])
        self.assertTrue(value['partial'])

    def test_mislabeled_environment_and_unassigned_dataset_cannot_be_queried(self):
        self.dataset.permission_tags.clear()
        self.assertFalse(catalog(self.reader, self.org)['sources'])
        with self.assertRaises(PermissionDenied):
            self.run_query()

    def test_literal_search_matches_unicode_values_in_approved_fields(self):
        publish(self.dataset, 'unicode', {'title': 'Montréal résumé', 'status': 'ready', 'project': 'shared'})
        value = self.run_query(text='MONTRÉAL')
        self.assertEqual([row['data']['title'] for row in value['evidence']], ['Montréal résumé'])


class QuerySchemaTests(SimpleTestCase):
    def test_no_sql_urls_prompts_identity_filters_or_unbounded_cursors(self):
        for value in [{'url': 'https://arbitrary.test'}, {'sql': 'SELECT *'}, {'user_id': str(uuid.uuid4())},
                {'filters': {}}, {'limit': True}, {'limit': 201}, {'sources': [str(uuid.uuid4())] * 2},
                {'text': 'x' * 201}, {'cursors': {'unknown': 1}}, {'sources': 'all'}]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_query(value)

    def test_model_cannot_choose_authority_scope_or_budget(self):
        client = CoreClient('https://core.example.test/api/platform/v1', secrets.token_urlsafe(32), str(uuid.uuid4()))
        for arguments in [{'text': 'test', 'sources': []}, {'text': 'test', 'limit': 200}, {'user_id': 'other'}, {'text': None}]:
            with self.subTest(arguments=arguments), patch.object(client, 'request') as outbound, self.assertRaises(ValueError):
                client.invoke_ai_query_tool(arguments, sources=[uuid.uuid4()], user_access_token=secrets.token_urlsafe(32))
            outbound.assert_not_called()
