from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import secrets
from unittest.mock import Mock

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, close_old_connections
from django.test import TestCase, TransactionTestCase, SimpleTestCase
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from ai_rules_client import AIRules
from core_client import CoreServiceError
from identity.access import Principal
from identity.models import Membership, Organization, PermissionTag, Role, User
from platform_services.ai_rules import (DEFAULTS, ZERO, RuleConflict, RuleDenied, acknowledge, digest, document,
    core_step, reserve, save_policy, transition, today, validate_clause)
from platform_services.agent_identity import issue_credential
from platform_services.catalog import seed_catalog
from platform_services.environments import create_environment
from platform_services.models import (AIRulePolicy, AIRuleRevision, AIUsageMeter, AIUsageReservation,
    AgentBinding, AgentDefinition, Dataset, DataRecord, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential)
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential

CAPABILITIES = ['rules.read', 'usage.reserve', 'usage.report']


def fixtures(cls):
    if not MarketplacePackage.objects.filter(slug='core-platform').exists():
        seed_catalog(apps, None)
    cls.org = Organization.objects.create(slug='rules-org', name='Rules organization')
    cls.other = Organization.objects.create(slug='rules-other', name='Other organization')
    install_core(cls.org)
    cls.admin = User.objects.create_user('rules-admin@example.test')
    cls.user = User.objects.create_user('rules-user@example.test')
    Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
    cls.membership = Membership.objects.create(organization=cls.org, user=cls.user)
    cls.manager, cls.reader = Principal(user=cls.admin), Principal(user=cls.user)
    cls.environment = create_environment(cls.org, name='sandbox', namespace='rules-sandbox', application_origin='https://rules.example.test')
    cls.production = create_environment(cls.org, name='production', namespace='rules-production', application_origin='https://rules-prod.example.test')
    cls.membership.access_tags.add(cls.environment.boundary_tag)
    cls.core = Installation.objects.get(organization=cls.org, package__slug='core-platform')
    cls.role = Role.objects.create(organization=cls.org, slug='rules-reader', name='Rules reader')
    cls.role.permissions.add(*cls.core.service.permissions.filter(codename__in=[name.replace('.', '-') for name in CAPABILITIES]))
    cls.membership.roles.add(cls.role)
    cls.sources, cls.tokens, cls.installations = [], [], []
    for index in range(2):
        package = MarketplacePackage.objects.create(organization=cls.org, slug=f'rules-app-{index}', name=f'Rules app {index}',
            kind='application', description='Rules-integrated application', manifest={'capabilities': CAPABILITIES, 'image': 'registry.example.test/rules@sha256:' + 'a' * 64})
        installation = install_package(cls.org, package, CAPABILITIES)
        credential, token = issue_workload_credential(installation, 'Rules backend', cls.environment)
        cls.sources.append(Principal(credential=credential))
        cls.tokens.append(token)
        cls.installations.append(installation)


def operation(key='request-one', **estimated):
    return {'request_key': key, 'operation': 'ai', 'estimated': {'requests': 1, **estimated}, 'query': None}


class RuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures(cls)

    def configure(self, baseline=None, overrides=None):
        policy, _ = AIRulePolicy.objects.get_or_create(organization=self.org)
        return save_policy(self.org, self.manager, {'revision': policy.revision, 'baseline': baseline or {}, 'overrides': overrides or []})

    def admit(self, values=None, source=0):
        return reserve(self.org, self.reader, values or operation(), source=self.sources[source])

    def finish(self, row, actual=None, source=0):
        transition(self.org, self.reader, row.pk, 'start', source=self.sources[source])
        return transition(self.org, self.reader, row.pk, 'settle', actual or {'requests': 1}, source=self.sources[source])

    def test_inheritance_can_only_tighten(self):
        policy = self.configure({'user_daily': {'tokens': 100}, 'query': {'max_rows': 50}}, [
            {'installation_id': str(self.installations[0].pk), 'environment': '', 'rules': {'user_daily': {'tokens': 1000}, 'query': {'max_rows': 100}}},
            {'installation_id': '', 'environment': 'sandbox', 'rules': {'user_daily': {'tokens': 20}, 'allowed_operations': ['query']}}])
        value = document(self.org, self.reader, source=self.sources[0])
        self.assertEqual(value['rules']['user_daily']['tokens'], 20)
        self.assertEqual(value['rules']['query']['max_rows'], 50)
        self.assertEqual(value['rules']['allowed_operations'], ['query'])
        with self.assertRaises(RuleDenied):
            self.admit()
        self.assertEqual(policy.history.get().snapshot['revision'], policy.revision)

    def test_user_quota_is_shared_across_applications(self):
        self.configure({'user_daily': {'requests': 1}})
        row = self.admit()
        with self.assertRaises(RuleDenied) as denied:
            self.admit(operation('second-app'), source=1)
        self.assertEqual((denied.exception.reason, denied.exception.metric), ('quota_exceeded', 'requests'))
        self.finish(row)
        value = document(self.org, self.reader, source=self.sources[1])
        self.assertEqual(value['usage']['remaining']['requests'], 0)
        self.assertEqual(value['usage']['used']['requests'], 1)
        self.assertIn('requests', value['usage']['warning_metrics'])
        self.assertNotIn('users', value['usage'])

    def test_organization_cap_applies_to_multiple_users(self):
        self.configure({'organization_daily': {'requests': 1}})
        self.admit()
        with self.assertRaises(RuleDenied):
            reserve(self.org, self.manager, operation('administrator'), source=self.sources[1])

    def test_reserve_settle_and_replay_do_not_double_count(self):
        row = self.admit(operation(tokens=100))
        self.assertEqual(self.admit(operation(tokens=100)).pk, row.pk)
        with self.assertRaises(RuleConflict):
            self.admit(operation(tokens=99))
        self.finish(row, {'requests': 1, 'tokens': 30})
        transition(self.org, self.reader, row.pk, 'settle', {'requests': 1, 'tokens': 30}, source=self.sources[0])
        meter = AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.user.pk))
        self.assertEqual((meter.used['tokens'], meter.held['tokens']), (30, 0))
        with self.assertRaises(RuleConflict):
            transition(self.org, self.reader, row.pk, 'settle', {'requests': 1, 'tokens': 31}, source=self.sources[0])

    def test_cancel_only_unstarted_and_start_only_once(self):
        row = self.admit()
        transition(self.org, self.reader, row.pk, 'cancel', source=self.sources[0])
        transition(self.org, self.reader, row.pk, 'cancel', source=self.sources[0])
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='*').held, ZERO)
        row = self.admit(operation('running'))
        transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])
        for action in ['start', 'cancel']:
            with self.assertRaises(RuleDenied):
                transition(self.org, self.reader, row.pk, action, source=self.sources[0])

    def test_expired_unknown_usage_does_not_release_money(self):
        self.configure({'paid_calls_allowed': True, 'user_daily': {'cost_micros': 100}, 'organization_daily': {'cost_micros': 100}})
        row = self.admit(operation(cost_micros=100))
        transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])
        AIUsageReservation.objects.filter(pk=row.pk).update(expires=timezone.now() - timedelta(days=1))
        with self.assertRaises(RuleDenied):
            self.admit(operation('more-spend', cost_micros=1))
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='*').held['cost_micros'], 100)

    def test_expired_unstarted_cannot_start_but_can_cancel(self):
        row = self.admit()
        AIUsageReservation.objects.filter(pk=row.pk).update(expires=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(RuleDenied):
            transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])
        transition(self.org, self.reader, row.pk, 'cancel', source=self.sources[0])

    def test_overruns_are_charged_in_full_and_pause_admission(self):
        row = self.admit(operation(tokens=1))
        outcome = self.finish(row, {'requests': 1, 'tokens': 10})
        self.assertEqual(outcome.state, 'overrun')
        with self.assertRaises(RuleDenied):
            self.admit(operation('next'))
        with self.assertRaises(PermissionDenied):
            acknowledge(self.org, self.reader, row.pk)
        acknowledge(self.org, self.manager, row.pk)
        self.admit(operation('next'))
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='*').used['tokens'], 10)

    def test_live_permission_revocation_and_policy_revision_before_start(self):
        row = self.admit()
        self.role.permissions.remove(self.core.service.permissions.get(codename='usage-report'))
        with self.assertRaises(PermissionDenied):
            transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])
        self.role.permissions.add(self.core.service.permissions.get(codename='usage-report'))
        self.configure({'user_daily': {'requests': 0}})
        with self.assertRaises(RuleDenied):
            transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])

    def test_reserved_objects_cannot_change_subject_or_consumer_or_tenant(self):
        row = self.admit()
        for organization, principal, source in [(self.org, self.manager, self.sources[0]),
                (self.org, self.reader, self.sources[1]), (self.other, self.manager, self.sources[0])]:
            with self.assertRaises(PermissionDenied):
                transition(organization, principal, row.pk, 'cancel', source=source)

    def test_tagged_policy_and_tagged_role_require_explicit_grants(self):
        policy = self.configure()
        tag = PermissionTag.objects.create(organization=self.org, slug='classified-rules', name='Classified rules', allow_workloads=True)
        policy.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.admit()
        policy.permission_tags.clear()
        self.role.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.admit()

    def test_admin_composition_authority_and_optimistic_revision(self):
        policy = self.configure()
        values = {'revision': policy.revision, 'baseline': {}, 'overrides': []}
        self.role.permissions.add(self.core.service.permissions.get(codename='rules-manage'))
        for principal in [self.reader, self.sources[0]]:
            with self.assertRaises(PermissionDenied):
                save_policy(self.org, principal, values)
        save_policy(self.org, self.manager, values)
        with self.assertRaises(RuleConflict):
            save_policy(self.org, self.manager, values)
        self.assertEqual(AIRuleRevision.objects.filter(policy=policy).count(), 2)

    def test_currency_and_strict_schema_and_limits(self):
        for clause in [{'currency': 'EUR'}, {'user_daily': {'tokens': True}}, {'user_daily': {'cost_micros': 1.1}},
                {'query': {'max_rows': 201}}, {'query': {'timeout_ms': 0}}, {'expression': 'execute'}, {'allowed_operations': ['unknown']}]:
            with self.subTest(clause=clause), self.assertRaises(ValidationError):
                validate_clause(clause)
        with self.assertRaises(ValidationError):
            self.admit(operation(requests=0))
        with self.assertRaises(ValidationError):
            self.configure(overrides=[{'installation_id': str(uuid), 'environment': '', 'rules': {}} for uuid in [self.other.pk]])

    def test_query_plan_admission_and_default_paid_denial(self):
        query = {'request_key': 'query', 'operation': 'query', 'estimated': {'requests': 1, 'query_rows': 5},
            'query': {'rows': 5, 'timeout_ms': 1000, 'paginated': True, 'indexed_filters': True}}
        self.admit(query)
        for field in ['paginated', 'indexed_filters']:
            invalid = deepcopy(query)
            invalid['request_key'] += field
            invalid['query'][field] = False
            with self.assertRaises(RuleDenied):
                self.admit(invalid)
        with self.assertRaises(RuleDenied):
            self.admit(operation('paid', cost_micros=1))

    def oauth(self, installation=0):
        node = RuntimeNode.objects.create(organization=self.org, name='Rules node', namespace=self.environment.namespace,
            environment=self.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(self.installations[installation], node, 'https://rules.example.test')
        raw = secrets.token_urlsafe(48)
        token = AccessToken(application=deployment.application, user=self.user, expires=timezone.now() + timedelta(minutes=5), scope='openid authorization')
        set_token_value(token, raw)
        token.save()
        return raw

    def test_api_bound_user_delegation_shared_quota_and_structured_denial(self):
        self.configure({'user_daily': {'requests': 1}})
        first_token, second_token = self.oauth(), self.oauth(1)
        def headers(index, user_token):
            return {'HTTP_AUTHORIZATION': 'Bearer ' + self.tokens[index], 'HTTP_X_CORE_USER_TOKEN': user_token}
        endpoint = '/api/platform/v1/ai-rules/'
        result = self.client.get(endpoint, **headers(0, first_token))
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['subject'], 'user:' + str(self.user.pk))
        self.assertEqual(self.client.get(endpoint, **headers(1, first_token)).status_code, 401)
        admitted = self.client.post(endpoint + 'reservations/', json.dumps(operation()), content_type='application/json', **headers(0, first_token))
        self.assertEqual(admitted.status_code, 201, admitted.content)
        denied = self.client.post(endpoint + 'reservations/', json.dumps(operation('other')), content_type='application/json', **headers(1, second_token))
        self.assertEqual(denied.status_code, 429, denied.content)
        self.assertEqual(denied.json()['error'], 'quota_exceeded')
        self.assertIn('Retry-After', denied)
        spoofed = self.client.post(endpoint + 'reservations/', json.dumps({**operation('spoofed'), 'subject': str(self.admin.pk)}),
            content_type='application/json', **headers(0, first_token))
        self.assertEqual(spoofed.status_code, 400)

    def test_agent_account_attribution_and_role_revocation(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='rules-agent', name='Rules agent', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installations[0], environment='sandbox')
        binding.roles.add(self.role)
        credential, token = issue_credential(binding, self.manager)
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + token}
        result = self.client.get('/api/platform/v1/ai-rules/', **headers)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['subject'], 'agent:' + str(agent.account.pk))
        binding.roles.clear()
        self.assertEqual(self.client.get('/api/platform/v1/ai-rules/', **headers).status_code, 403)

    def test_admin_portal_and_permission_tag_controls(self):
        self.client.force_login(self.admin)
        result = self.client.get('/organizations/rules-org/platform/ai-rules/')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertContains(result, 'Paid calls start disabled')
        self.assertEqual(json.loads(result.context['form']['baseline'].value()), DEFAULTS)
        self.assertEqual(json.loads(result.context['form']['overrides'].value()), [])
        edited = self.client.post('/organizations/rules-org/platform/ai-rules/', {'revision': 1,
            'baseline': json.dumps({'user_daily': {'requests': 50}}), 'overrides': '[]'})
        self.assertEqual(edited.status_code, 302, edited.context['form'].errors if edited.status_code == 200 else edited.status_code)
        self.assertEqual(AIRulePolicy.objects.get(organization=self.org).baseline['user_daily']['requests'], 50)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/organizations/rules-org/platform/ai-rules/').status_code, 403)

    def test_core_data_reads_enforce_central_query_caps_and_quota(self):
        data = Installation.objects.get(organization=self.org, package__slug='core-data')
        data.granted_capabilities = ['data.read']
        data.save()
        credential, token = issue_workload_credential(data, 'Data reader', self.environment)
        dataset = Dataset.objects.create(organization=self.org, slug='metered', name='Metered', schema={})
        dataset.permission_tags.add(self.environment.boundary_tag)
        DataRecord.objects.bulk_create([DataRecord(dataset=dataset, event_id=str(index), data={'index': index}) for index in range(3)])
        self.configure({'query': {'max_rows': 1}, 'user_daily': {'requests': 1}})
        result = self.client.get('/api/platform/v1/datasets/metered/records/', HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(len(result.json()['records']), 1)
        self.assertEqual(result.json()['effective_limit'], 1)
        denied = self.client.get('/api/platform/v1/datasets/metered/records/', HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(denied.status_code, 429, denied.content)
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='*').used['query_rows'], 1)

    def test_platform_step_budget_denies_before_effects(self):
        self.configure({'organization_daily': {'requests': 0}})
        callback = Mock()
        with self.assertRaises(RuleDenied):
            core_step(self.org, self.reader, 'blocked-step', callback)
        callback.assert_not_called()
        self.assertFalse(AIUsageReservation.objects.filter(organization=self.org).exists())

    def test_disabled_core_platform_fails_closed(self):
        self.core.enabled = False
        self.core.save(update_fields=['enabled'])
        with self.assertRaises(PermissionDenied):
            reserve(self.org, self.reader, operation(), source=self.sources[0], internal=True)


class ConcurrentRuleTests(TransactionTestCase):
    def test_competing_applications_cannot_overbook_user_quota(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row-lock concurrency is validated against PostgreSQL; SQLite fails closed under contention.')
        fixtures(self)
        save_policy(self.org, self.manager, {'revision': 1, 'baseline': {'user_daily': {'requests': 1}}, 'overrides': []})
        import threading
        barrier = threading.Barrier(2)
        def contender(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                reserve(self.org, self.reader, operation('concurrent-' + str(index)), source=self.sources[index])
                return 'admitted'
            except RuleDenied:
                return 'denied'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(contender, range(2)))
        self.assertCountEqual(outcomes, ['admitted', 'denied'])
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='*').held['requests'], 1)

    def test_same_reservation_can_start_only_once_concurrently(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent start is validated on PostgreSQL.')
        fixtures(self)
        row = reserve(self.org, self.reader, operation(), source=self.sources[0])
        import threading
        barrier = threading.Barrier(2)
        def start(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                transition(self.org, self.reader, row.pk, 'start', source=self.sources[0])
                return 'started'
            except RuleDenied:
                return 'denied'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(start, range(2)))
        self.assertCountEqual(outcomes, ['started', 'denied'])


class RuleClientTests(SimpleTestCase):
    def adapter(self):
        core = Mock(organization_id='org', environment='sandbox')
        value = {'organization_id': 'org', 'environment': 'sandbox', 'schema_version': 1, 'currency': 'USD', 'rules': DEFAULTS}
        value['digest'] = digest(value)
        core.request.return_value = value
        return AIRules(core, user_access_token=secrets.token_urlsafe(32)), core

    def test_rule_tool_is_read_only_with_fixed_authority(self):
        adapter, core = self.adapter()
        self.assertEqual(adapter.call_tool('core_ai_rules', {})['currency'], 'USD')
        self.assertEqual(core.request.call_args.args, ('GET', '/ai-rules/'))
        with self.assertRaises(ValueError):
            adapter.call_tool('core_ai_rules', {'subject': 'administrator'})
        core.request.return_value['organization_id'] = 'other'
        with self.assertRaises(CoreServiceError):
            adapter.read()

    def test_paid_callback_requires_financial_approval_and_live_reservation(self):
        adapter, core = self.adapter()
        callback = Mock()
        with self.assertRaises(CoreServiceError):
            adapter.execute('paid', 'ai', {'requests': 1, 'cost_micros': 10}, callback)
        callback.assert_not_called()
        adapter.reserve = Mock(side_effect=CoreServiceError('Central budget denied'))
        with self.assertRaises(CoreServiceError):
            adapter.execute('paid', 'ai', {'requests': 1, 'cost_micros': 10}, callback, financial_approval=lambda cost, currency: True)
        callback.assert_not_called()

    def test_unknown_outcome_retains_hold_and_replay_never_reexecutes(self):
        adapter, core = self.adapter()
        adapter.reserve = Mock(return_value={'id': 'c2b971f1-6e18-4fd3-ad83-0868cc3a7f91', 'state': 'reserved', 'estimated': {'requests': 1}})
        adapter.update = Mock(return_value={'state': 'running'})
        callback = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            adapter.execute('unknown', 'ai', {'requests': 1}, callback)
        self.assertEqual(adapter.update.call_count, 1)
        adapter.reserve.return_value['state'] = 'running'
        with self.assertRaises(CoreServiceError):
            adapter.execute('unknown', 'ai', {'requests': 1}, callback)
        self.assertEqual(callback.call_count, 1)
