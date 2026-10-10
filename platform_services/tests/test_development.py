from copy import deepcopy
from io import StringIO
import json
import secrets
from unittest.mock import Mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, SimpleTestCase, Client
from django.test import TransactionTestCase
from django.db import connection, close_old_connections

from development_client import Development
from identity.access import Principal, decision
from identity.models import AuditEvent, PermissionTag, Role
from platform_services.ai_rules import RuleConflict, RuleDenied, save_policy
from platform_services.data import publish
from platform_services.development import (authorize_run, create_object, export_bundle, import_bundle, publish_revision,
    revision_document, run_document, run_revision, save_revision, share_object)
from platform_services.development_engine import fit, operate, predict, shape
from platform_services.documentation import checksum
from platform_services.models import (AIRulePolicy, AIUsageMeter, AIUsageReservation, AgentBinding, AgentDefinition, AgentTask,
    Dataset, DevelopmentObject, DevelopmentRevision, DevelopmentRun, Installation, Pipeline)
from platform_services.tests.test_ai_rules import fixtures

CAPS = ['development.read', 'development.write', 'development.run', 'data.read', 'agents.run']
TRANSFORM = {'operator': 'transform', 'inputs': {'rows': 'rows'}, 'config': {'fields': ['x', 'y'], 'rename': {}, 'where': {}}}
TRAIN = {'operator': 'linear_regression', 'inputs': {'rows': 'rows'},
    'config': {'features': ['x'], 'target': 'y', 'seed': 42, 'epochs': 200, 'ridge': 0}}


class DevelopmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures(cls)
        cls.owner = Installation.objects.get(organization=cls.org, package__slug='core-workflows')
        for installation in cls.installations:
            installation.granted_capabilities += CAPS
            installation.save(update_fields=['granted_capabilities'])
        cls.role.permissions.add(*cls.owner.service.permissions.filter(codename__in=[c.replace('.', '-') for c in CAPS]))
        cls.role.permissions.add(*Installation.objects.get(organization=cls.org, package__slug='core-data').service.permissions.filter(codename='data-read'))
        cls.schema = {'type': 'object', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}}, 'required': ['x', 'y'], 'additionalProperties': False}
        cls.dataset = Dataset.objects.create(organization=cls.org, slug='ml-source', name='ML source', schema=cls.schema, data_class='synthetic')
        cls.dataset.permission_tags.add(cls.environment.boundary_tag)
        for i in range(50):
            publish(cls.dataset, f'ml-sample-{i}', {'x': i, 'y': 3*i+2})

    def setUp(self):
        self.counter = 0

    def make(self, spec=TRANSFORM, kind='pipeline', owner=None, published=True, principal=None, source=None):
        self.counter += 1
        owner = owner or self.owner
        principal = principal or self.manager
        obj = create_object(self.org, principal, {'slug': f'object-{self.counter}', 'name': f'Object {self.counter}', 'kind': kind,
            'installation_id': str(owner.pk), 'environment_id': str(self.environment.pk), 'spec': deepcopy(spec)}, source)
        revision = obj.versions.get(number=1)
        if published:
            publish_revision(revision, self.manager)
            revision.refresh_from_db()
        return obj, revision

    def connection(self, owner=None):
        return self.make({'operator': 'dataset', 'inputs': {}, 'config': {'dataset_id': str(self.dataset.pk), 'schema_digest': checksum(self.schema), 'limit': 50}}, 'connection', owner)

    def graph(self, nodes, output, inputs=None, owner=None, published=True):
        return self.make({'operator': 'graph', 'inputs': inputs or {}, 'config': {'nodes': nodes, 'output': output}}, 'workflow', owner, published)

    @staticmethod
    def node(name, revision, inputs=None):
        return {'name': name, 'revision_id': str(revision.pk), 'digest': revision.digest, 'inputs': inputs or {}}

    def execute(self, revision, inputs=None, principal=None, source=None, key='experiment'):
        return run_revision(revision, principal or self.manager, {'request_key': key, 'inputs': inputs or {}}, source)

    def api(self, path, data=None, index=0, token=None):
        return self.client.generic('POST' if data is not None else 'GET', '/api/platform/v1/development/' + path,
            data=json.dumps(data) if data is not None else '', content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + (token or self.tokens[index]))

    def test_end_to_end_pinned_connection_training_and_prediction(self):
        _, conn = self.connection()
        _, model = self.make(TRAIN, 'model')
        _, inference = self.make({'operator': 'predict', 'inputs': {'rows': 'rows', 'model': 'model'}, 'config': {'field': 'prediction'}}, 'prediction')
        _, root = self.graph([self.node('predict', inference, {'rows': 'source', 'model': 'train'}),
            self.node('source', conn), self.node('train', model, {'rows': 'source'})], 'predict')
        run = self.execute(root)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(len(run.result['value']), 50)
        self.assertAlmostEqual(run.result['value'][10]['prediction'], 32, places=5)
        self.assertEqual(len(run.lineage), 4)
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.admin.pk)).used['query_rows'], 50)
        self.assertNotIn('records', export_bundle(root, self.manager))

    def test_model_holdout_metrics_and_reproducibility(self):
        records = [{'x': i, 'y': 3*i+2} for i in range(50)]
        result = fit(records, TRAIN['config'])
        self.assertEqual(result, fit(records, TRAIN['config']))
        self.assertEqual((result['metrics']['training_rows'], result['metrics']['test_rows']), (40, 10))
        self.assertAlmostEqual(result['coefficients'][0], 3, places=7)
        self.assertAlmostEqual(result['intercept'], 2, places=6)
        self.assertLess(result['metrics']['mse'], result['metrics']['baseline_mse'] * 1e-10)

    def test_revisions_are_immutable_and_compositions_keep_old_pin(self):
        obj, old = self.make()
        _, root = self.graph([self.node('step', old, {'rows': '$rows'})], 'step', {'rows': 'rows'})
        spec = deepcopy(TRANSFORM)
        spec['config']['fields'] = ['x']
        new = save_revision(obj, self.manager, 1, spec)
        publish_revision(new, self.manager)
        self.assertEqual(self.execute(root, {'rows': [{'x': 1, 'y': 2}]}).result['value'], [{'x': 1, 'y': 2}])
        old.spec = spec
        with self.assertRaises(ValidationError):
            old.save()
        with self.assertRaises(RuleConflict):
            save_revision(obj, self.manager, 1, spec)

    def test_cycle_dangling_and_typed_port_rejections(self):
        _, pipeline = self.make()
        _, model = self.make(TRAIN, 'model')
        cases = [
            ([self.node('a', pipeline, {'rows': 'b'}), self.node('b', pipeline, {'rows': 'a'})], 'a'),
            ([self.node('a', pipeline, {'rows': 'missing'})], 'a'),
            ([self.node('m', model, {'rows': '$rows'}), self.node('a', pipeline, {'rows': 'm'})], 'a'),
            ([self.node('a', pipeline, {'rows': '$missing'})], 'a'),
            ([self.node('a', pipeline, {'rows': '$rows'}), self.node('unused', pipeline, {'rows': '$rows'})], 'a'),
        ]
        for nodes, output in cases:
            with self.assertRaises(ValidationError):
                self.graph(nodes, output, {'rows': 'rows'})

    def test_tampered_pin_rejected(self):
        _, pipeline = self.make()
        node = self.node('a', pipeline, {'rows': '$rows'})
        node['digest'] = secrets.token_hex(32)
        with self.assertRaises(ValidationError):
            self.graph([node], 'a', {'rows': 'rows'})

    def test_unpublished_dependency_requires_separate_review(self):
        _, child = self.make(published=False)
        _, root = self.graph([self.node('step', child, {'rows': '$rows'})], 'step', {'rows': 'rows'}, published=False)
        self.assertFalse(revision_document(root, self.manager)['published'])
        with self.assertRaises(PermissionDenied):
            publish_revision(root, self.manager)
        publish_revision(child, self.manager)
        publish_revision(root, self.manager)
        self.assertEqual(self.execute(root, {'rows': [{'x': 1}]}).status, 'completed')

    def test_cross_application_workflow_sharing_does_not_grant_dependencies(self):
        child, step = self.make(owner=self.installations[1])
        root_obj, root = self.graph([self.node('step', step, {'rows': '$rows'})], 'step', {'rows': 'rows'}, owner=self.installations[0])
        with self.assertRaises(PermissionDenied):
            self.execute(root, {'rows': [{'x': 1}]}, self.sources[0])
        share_object(child, self.manager, [str(self.installations[0].pk)], True)
        run = self.execute(root, {'rows': [{'x': 1}]}, self.sources[0])
        self.assertEqual(run.result['value'], [{'x': 1}])
        share_object(child, self.manager, [], True)
        with self.assertRaises(PermissionDenied):
            run_document(run, self.sources[0])
        with self.assertRaises(PermissionDenied):
            self.execute(root, {'rows': [{'x': 1}]}, self.sources[0])

    def test_classification_follows_source_through_graph_and_run(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='ml-restricted', name='Restricted ML', allow_workloads=True)
        self.dataset.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.connection()
        self.org.memberships.get(user=self.admin).access_tags.add(tag)
        conn_obj, conn = self.connection()
        root_obj, root = self.graph([self.node('source', conn)], 'source', owner=self.installations[0])
        share_object(conn_obj, self.manager, [str(self.installations[0].pk)], True)
        self.assertFalse(decision(self.sources[0], root)[0])
        self.installations[0].access_tags.add(tag)
        run = self.execute(root, principal=self.sources[0])
        self.assertTrue(decision(self.sources[0], run)[0])
        self.installations[0].access_tags.remove(tag)
        with self.assertRaises(PermissionDenied):
            run_document(run, self.sources[0])

    def test_role_tag_policy_cannot_be_bypassed(self):
        _, revision = self.make()
        tag = PermissionTag.objects.create(organization=self.org, slug='role-control', name='Role control')
        self.role.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            revision_document(revision, self.reader)

    def test_cross_environment_and_cross_org_references_rejected(self):
        _, child = self.make()
        obj = child.development_object
        obj.environment = self.production
        with self.assertRaises(ValidationError):
            obj.full_clean()
        DevelopmentObject.objects.filter(pk=obj.pk).update(environment=self.production)
        with self.assertRaises(ValidationError):
            self.graph([self.node('a', child, {'rows': '$rows'})], 'a', {'rows': 'rows'})
        values = {'slug': 'wrong-tenant', 'name': 'Wrong tenant', 'kind': 'pipeline', 'installation_id': str(self.installations[0].pk),
            'environment_id': str(self.environment.pk), 'spec': TRANSFORM}
        with self.assertRaises((PermissionDenied, ValidationError)):
            create_object(self.other, self.manager, values)

    def test_foreign_consumer_and_owner_escalation_rejected(self):
        obj, _ = self.make(owner=self.installations[1])
        with self.assertRaises(PermissionDenied):
            save_revision(obj, self.sources[0], 1, TRANSFORM)
        with self.assertRaises(PermissionDenied):
            create_object(self.org, self.sources[0], {'slug': 'escalation', 'name': 'Escalation', 'kind': 'pipeline',
                'installation_id': str(self.installations[1].pk), 'environment_id': str(self.environment.pk), 'spec': TRANSFORM})

    def test_native_admin_cannot_move_production_data_into_sandbox_composition(self):
        self.dataset.permission_tags.set([self.production.boundary_tag])
        with self.assertRaises(PermissionDenied):
            self.connection()
        self.dataset.permission_tags.clear()
        with self.assertRaises(PermissionDenied):
            self.connection()
        self.dataset.permission_tags.set([self.environment.boundary_tag])
        _, revision = self.connection()
        self.dataset.permission_tags.add(self.production.boundary_tag)
        with self.assertRaises(PermissionDenied):
            self.execute(revision)
        self.dataset.permission_tags.remove(self.production.boundary_tag)
        revision.permission_tags.add(self.production.boundary_tag)
        with self.assertRaises(PermissionDenied):
            self.execute(revision)
        with self.assertRaises(PermissionDenied):
            publish_revision(revision, self.manager)

    def test_machine_can_propose_and_read_own_draft_but_cannot_publish_or_share(self):
        obj, revision = self.make(owner=self.installations[0], principal=self.sources[0], published=False)
        self.assertFalse(revision_document(revision, self.sources[0])['published'])
        for operation in [lambda: publish_revision(revision, self.sources[0]), lambda: share_object(obj, self.sources[0], [], True),
                lambda: self.execute(revision, {'rows': [{'x': 1}]}, self.sources[0])]:
            with self.assertRaises(PermissionDenied):
                operation()

    def test_withdrawal_disables_replays_and_saved_results(self):
        _, revision = self.make()
        run = self.execute(revision, {'rows': [{'x': 1}]})
        publish_revision(revision, self.manager, withdraw=True)
        with self.assertRaises(PermissionDenied):
            self.execute(revision, {'rows': [{'x': 1}]})
        # Review state is still visible to a native administrator, but execution is forbidden.
        self.assertFalse(revision_document(revision, self.manager)['published'])
        with self.assertRaises(PermissionDenied):
            run_document(run, self.manager)

    def test_dataset_and_pipeline_definition_drift_requires_review(self):
        _, revision = self.connection()
        self.dataset.schema = {'type': 'object'}
        self.dataset.save(update_fields=['schema'])
        with self.assertRaises(PermissionDenied):
            self.execute(revision)
        pipeline = Pipeline.objects.create(organization=self.org, slug='source-transform', name='Source transform', dataset=self.dataset,
            transforms=[{'operation': 'select', 'fields': ['x']}])
        _, adapter = self.make({'operator': 'pipeline', 'inputs': {'rows': 'rows'}, 'config': {'pipeline_id': pipeline.pk, 'transforms_digest': checksum(pipeline.transforms)}})
        self.assertEqual(self.execute(adapter, {'rows': [{'x': 1, 'y': 2}]}).result['value'], [{'x': 1}])
        pipeline.transforms = [{'operation': 'select', 'fields': ['y']}]
        pipeline.save(update_fields=['transforms'])
        with self.assertRaises(PermissionDenied):
            revision_document(adapter, self.manager)

    def test_replay_is_exactly_once_and_conflicting_inputs_rejected(self):
        _, revision = self.make()
        first = self.execute(revision, {'rows': [{'x': 1}]})
        used = AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.admin.pk)).used
        self.assertEqual(self.execute(revision, {'rows': [{'x': 1}]}).pk, first.pk)
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.admin.pk)).used, used)
        with self.assertRaises(RuleConflict):
            self.execute(revision, {'rows': [{'x': 2}]})
        self.assertEqual(DevelopmentRun.objects.count(), 1)

    def test_quota_denial_rolls_back_entire_experiment(self):
        _, conn = self.connection()
        _, model = self.make(TRAIN, 'model')
        _, root = self.graph([self.node('source', conn), self.node('fit', model, {'rows': 'source'})], 'fit')
        save_policy(self.org, self.manager, {'revision': 1, 'baseline': {'user_daily': {'requests': 1}}, 'overrides': []})
        with self.assertRaises(RuleDenied):
            self.execute(root)
        self.assertEqual(DevelopmentRun.objects.count(), 0)
        self.assertEqual(AIUsageReservation.objects.count(), 0)

    def test_query_limits_clamp_and_measure_rows(self):
        _, conn = self.connection()
        save_policy(self.org, self.manager, {'revision': 1, 'baseline': {'query': {'max_rows': 12}}, 'overrides': []})
        run = self.execute(conn)
        self.assertEqual(len(run.result['value']), 12)
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.admin.pk)).used['query_rows'], 12)

    def test_api_returns_common_quota_contract_and_preserves_denial_audit(self):
        _, revision = self.make(owner=self.installations[0])
        save_policy(self.org, self.manager, {'revision': 1, 'baseline': {'user_daily': {'requests': 0}}, 'overrides': []})
        result = self.api(f'revisions/{revision.pk}/runs/', {'request_key': 'denied', 'inputs': {'rows': [{'x': 1}]}})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.json()['error'], 'quota_exceeded')
        self.assertIn('Retry-After', result)
        self.assertEqual(DevelopmentRun.objects.count(), 0)
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action='development.quota_denied', outcome='denied').exists())

    def test_api_read_and_creation_respect_sharing_and_bound_credentials(self):
        obj, revision = self.make(owner=self.installations[1])
        self.assertEqual(self.api(f'revisions/{revision.pk}/').status_code, 403)
        share_object(obj, self.manager, [str(self.installations[0].pk)], True)
        result = self.api(f'revisions/{revision.pk}/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['digest'], revision.digest)
        self.assertEqual(self.api('objects/').json()['objects'][0]['revision_id'], str(revision.pk))
        self.assertEqual(self.api('objects/?limit=51').status_code, 400)
        self.assertEqual(self.api('objects/?after=invalid').status_code, 400)
        self.assertEqual(self.api('objects/', {'slug': 'proposed', 'name': 'Proposed', 'kind': 'pipeline',
            'installation_id': str(self.installations[0].pk), 'environment_id': str(self.environment.pk), 'spec': TRANSFORM}).status_code, 201)

    def test_output_privacy_and_account_separation(self):
        _, revision = self.make(owner=self.installations[0])
        self.assertEqual(self.api(f'revisions/{revision.pk}/runs/', {'request_key': 'own', 'inputs': {'rows': [{'x': 1}]}}).status_code, 201)
        run = DevelopmentRun.objects.get()
        self.assertEqual(self.api(f'runs/{run.pk}/').status_code, 200)
        self.assertEqual(self.api(f'runs/{run.pk}/', index=1).status_code, 403)

    def test_delegated_user_live_roles_and_cross_app_shared_quota(self):
        from datetime import timedelta
        from django.utils import timezone
        from oauth2_provider.models import AccessToken, set_token_value
        from platform_services.models import RuntimeNode, WorkloadCredential
        from platform_services.services import create_deployment
        def oauth(index):
            node = RuntimeNode.objects.create(organization=self.org, name=f'Development runtime {index}', namespace=self.environment.namespace,
                environment=self.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
            deployment = create_deployment(self.installations[index], node, 'https://rules.example.test')
            raw = secrets.token_urlsafe(48)
            token = AccessToken(application=deployment.application, user=self.user, expires=timezone.now()+timedelta(minutes=5), scope='openid authorization')
            set_token_value(token, raw)
            token.save()
            return raw
        obj, revision = self.make(owner=self.installations[0])
        share_object(obj, self.manager, [str(self.installations[1].pk)], True)
        tokens = [oauth(0), oauth(1)]
        save_policy(self.org, self.manager, {'revision': 1, 'baseline': {'user_daily': {'requests': 1}}, 'overrides': []})
        endpoint = f'/api/platform/v1/development/revisions/{revision.pk}/runs/'
        def execute(index, key, user_token=None):
            return self.client.post(endpoint, json.dumps({'request_key': key, 'inputs': {'rows': [{'x': 1}]}}), content_type='application/json',
                HTTP_AUTHORIZATION='Bearer ' + self.tokens[index], HTTP_X_CORE_USER_TOKEN=user_token or tokens[index])
        self.assertEqual(execute(1, 'wrong-app', tokens[0]).status_code, 401)
        self.assertEqual(execute(0, 'user-first').status_code, 201)
        self.assertEqual(DevelopmentRun.objects.get().subject, 'user:' + str(self.user.pk))
        self.assertEqual(execute(1, 'user-second').status_code, 429)
        self.membership.roles.clear()
        self.assertEqual(execute(0, 'user-first').status_code, 403)

    def test_reused_subgraphs_have_an_expanded_execution_bound(self):
        _, child = self.make()
        # A chain fits the node limit, but repeatedly expanded nested graphs cannot grow without bound.
        nodes = [self.node('first', child, {'rows': '$rows'})]
        for index in range(1, 24):
            nodes.append(self.node(f'n{index}', child, {'rows': 'first' if index == 1 else f'n{index-1}'}))
        _, inner = self.graph(nodes, 'n23', {'rows': 'rows'})
        with self.assertRaises(ValidationError):
            self.graph([self.node('one', inner, {'rows': '$rows'}), self.node('two', inner, {'rows': 'one'})], 'two', {'rows': 'rows'})

    def test_bundle_import_remaps_to_new_drafts_without_sharing_or_reviews(self):
        _, conn = self.connection()
        _, model = self.make(TRAIN, 'model')
        obj, root = self.graph([self.node('source', conn), self.node('train', model, {'rows': 'source'})], 'train')
        share_object(obj, self.manager, [str(self.installations[0].pk)], True)
        bundle = export_bundle(root, self.manager)
        payload = json.dumps(bundle)
        self.assertNotIn('consumers', payload)
        self.assertNotIn('permission_tags', payload)
        self.assertNotIn('reviewer', payload)
        self.assertNotIn('cw_', payload)
        self.assertNotIn('y": 149', payload)
        bindings = {key: str(self.dataset.pk) for key in bundle['resources']}
        imported, objects = import_bundle(self.org, self.manager, {'bundle': bundle, 'bindings': bindings,
            'installation_id': str(self.owner.pk), 'environment_id': str(self.environment.pk), 'prefix': 'imported'})
        self.assertEqual(len(objects), 3)
        self.assertNotEqual(imported.pk, obj.pk)
        for new in objects:
            self.assertFalse(new.versions.get().published)
            self.assertEqual(new.consumers.count(), 0)
            publish_revision(new.versions.get(), self.manager)
        self.assertEqual(self.execute(imported.versions.get()).result['type'], 'model')

    def test_bundle_integrity_and_unbound_resource_rejections_are_atomic(self):
        _, conn = self.connection()
        bundle = export_bundle(conn, self.manager)
        values = {'bundle': bundle, 'bindings': {}, 'installation_id': str(self.owner.pk), 'environment_id': str(self.environment.pk), 'prefix': 'bad'}
        before = DevelopmentObject.objects.count()
        with self.assertRaises(ValidationError):
            import_bundle(self.org, self.manager, values)
        changed = deepcopy(bundle)
        changed['objects'][0]['name'] = 'Tampered'
        with self.assertRaises(ValidationError):
            import_bundle(self.org, self.manager, {**values, 'bundle': changed})
        self.assertEqual(DevelopmentObject.objects.count(), before)

    def test_agent_steps_use_approved_account_and_observe_revocation(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='ml-selector', name='ML selector', kind='select', configuration={'fields': ['x']})
        agent.permission_tags.add(self.environment.boundary_tag)
        binding = AgentBinding.objects.create(account=agent.account, installation=self.owner, environment='sandbox')
        role = Role.objects.create(organization=self.org, slug='ml-agent', name='ML agent')
        role.permissions.add(Installation.objects.get(organization=self.org, package__slug='core-agents').service.permissions.get(codename='agents-run'))
        binding.roles.add(role)
        _, revision = self.make({'operator': 'agent', 'inputs': {'rows': 'rows'}, 'config': {'agent_id': agent.pk,
            'binding_id': str(binding.pk), 'definition_digest': checksum({'kind': agent.kind, 'configuration': agent.configuration})}}, 'agent')
        result = self.execute(revision, {'rows': [{'x': 1, 'y': 2}]})
        self.assertEqual(result.result['value'], [{'x': 1}])
        self.assertEqual(result.lineage[0]['agent_account_id'], str(agent.account_id))
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='agent:' + str(agent.account_id)).used['requests'], 1)
        binding.active = False
        binding.save(update_fields=['active'])
        with self.assertRaises(PermissionDenied):
            run_document(result, self.manager)

    def test_agent_cannot_receive_classified_input_without_its_own_grant(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='ml-inspector', name='ML inspector', kind='inspect', configuration={})
        agent.permission_tags.add(self.environment.boundary_tag)
        binding = AgentBinding.objects.create(account=agent.account, installation=self.owner, environment='sandbox')
        role = Role.objects.create(organization=self.org, slug='agent-input', name='Agent input')
        role.permissions.add(Installation.objects.get(organization=self.org, package__slug='core-agents').service.permissions.get(codename='agents-run'))
        binding.roles.add(role)
        _, revision = self.make({'operator': 'agent', 'inputs': {'rows': 'rows'}, 'config': {'agent_id': agent.pk,
            'binding_id': str(binding.pk), 'definition_digest': checksum({'kind': agent.kind, 'configuration': agent.configuration})}}, 'agent')
        tag = PermissionTag.objects.create(organization=self.org, slug='classified-experiment', name='Classified experiment', allow_workloads=True)
        revision.permission_tags.add(tag)
        self.org.memberships.get(user=self.admin).access_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.execute(revision, {'rows': [{'x': 1}]})
        self.assertEqual(DevelopmentRun.objects.count(), 0)

    def test_agent_task_scope_includes_each_development_object(self):
        from django.utils import timezone
        from datetime import timedelta
        agent = AgentDefinition.objects.create(organization=self.org, slug='task-inspector', name='Task inspector', kind='inspect', configuration={})
        agent.permission_tags.add(self.environment.boundary_tag)
        obj, _ = self.make()
        task = AgentTask.objects.create(organization=self.org, installation=self.owner, environment='sandbox', name='Experiment task', description='Approved development objects only',
            expires_at=timezone.now()+timedelta(hours=1), resources={'development_objects': []}, approved_by=self.admin)
        binding = AgentBinding.objects.create(account=agent.account, installation=self.owner, environment='sandbox', task=task)
        self.assertFalse(decision(Principal(agent_binding=binding), obj)[0])
        task.resources = {'development_objects': [str(obj.pk)]}
        task.full_clean()
        task.save()
        self.assertTrue(decision(Principal(agent_binding=binding), obj)[0])

    def test_agent_needs_data_role_for_records_received_from_a_dataset(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='data-inspector', name='Data inspector', kind='inspect', configuration={})
        agent.permission_tags.add(self.environment.boundary_tag)
        binding = AgentBinding.objects.create(account=agent.account, installation=self.owner, environment='sandbox')
        role = Role.objects.create(organization=self.org, slug='dataset-agent', name='Dataset agent')
        role.permissions.add(Installation.objects.get(organization=self.org, package__slug='core-agents').service.permissions.get(codename='agents-run'))
        binding.roles.add(role)
        _, conn = self.connection()
        _, adapter = self.make({'operator': 'agent', 'inputs': {'rows': 'rows'}, 'config': {'agent_id': agent.pk,
            'binding_id': str(binding.pk), 'definition_digest': checksum({'kind': agent.kind, 'configuration': agent.configuration})}}, 'agent')
        _, root = self.graph([self.node('source', conn), self.node('agent', adapter, {'rows': 'source'})], 'agent')
        with self.assertRaises(PermissionDenied):
            self.execute(root)
        role.permissions.add(Installation.objects.get(organization=self.org, package__slug='core-data').service.permissions.get(codename='data-read'))
        self.assertEqual(len(self.execute(root).result['value']), 50)

    def test_browser_create_review_and_run_and_csrf(self):
        self.client.force_login(self.admin)
        route = f'/organizations/{self.org.slug}/platform/development/'
        self.assertEqual(self.client.get(route).status_code, 200)
        response = self.client.post(route+'new/', {'slug': 'browser-model', 'name': 'Browser model', 'kind': 'model',
            'installation_id': self.owner.pk, 'environment_id': self.environment.pk, 'spec': json.dumps(TRAIN)})
        self.assertEqual(response.status_code, 302)
        obj = DevelopmentObject.objects.get(slug='browser-model')
        detail = route + f'objects/{obj.pk}/'
        self.assertEqual(self.client.get(detail).status_code, 200)
        for resource_type, resource in [('developmentobject', obj), ('developmentrevision', obj.versions.get())]:
            self.assertEqual(self.client.get(f'/organizations/{self.org.slug}/security/resources/{resource_type}/{resource.pk}/').status_code, 200)
        self.assertEqual(self.client.post(detail, {'action': 'publish', 'revision_id': obj.versions.get().pk}).status_code, 302)
        response = self.client.post(detail, {'action': 'run', 'revision_id': obj.versions.get().pk, 'request_key': 'browser',
            'inputs': json.dumps({'rows': [{'x': i, 'y': 3*i+2} for i in range(20)]})})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DevelopmentRun.objects.get().result['type'], 'model')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(detail, {'action': 'withdraw', 'revision_id': obj.versions.get().pk}).status_code, 403)

    def test_demo_command_is_idempotent_and_does_not_execute_or_grant(self):
        for _ in range(2):
            call_command('development_demo', organization=self.org.slug, administrator=self.admin.email, stdout=StringIO())
        self.assertEqual(DevelopmentObject.objects.count(), 3)
        self.assertEqual(DevelopmentRun.objects.count(), 0)
        self.assertEqual(sum(obj.consumers.count() for obj in DevelopmentObject.objects.all()), 0)
        root = DevelopmentObject.objects.get(slug='demo-workflow').versions.get()
        self.assertEqual(self.execute(root).result['type'], 'model')


class OperatorAndSDKTests(SimpleTestCase):
    def test_fusion_preserves_distinct_types_and_rejects_collision_or_overflow(self):
        result = operate('join', {'left_key': 'id', 'right_key': 'id', 'right_prefix': 'other'},
            {'left': [{'id': 1}, {'id': True}], 'right': [{'id': 1, 'score': 10}]})
        self.assertEqual(result, [{'id': 1, 'other_id': 1, 'other_score': 10}])
        with self.assertRaises(ValidationError):
            operate('union', {}, {'left': [{'x': 1}]*101, 'right': [{'x': 2}]*100})
        with self.assertRaises(ValidationError):
            operate('join', {'left_key': 'id', 'right_key': 'id', 'right_prefix': 'other'}, {'left': [{'id': 1, 'other_id': 9}], 'right': [{'id': 1}]})

    def test_arbitrary_code_and_remote_operator_rejected(self):
        for op in ['python', 'sql', 'http', 'provider_call', 'deploy', 'pickle']:
            with self.assertRaises(ValidationError):
                shape({'operator': op, 'inputs': {}, 'config': {'url': 'https://provider.example.test'}}, 'workflow')

    def test_invalid_nonfinite_nested_and_oversized_training_inputs_rejected(self):
        for data in [[], [{'x': True, 'y': 2}]*10, [{'x': float('nan'), 'y': 2}]*10,
                [{'x': {'value': 1}, 'y': 2}]*10, [{'x': 10**1000, 'y': 2}]*10, [{'x': 1, 'y': 2}]*201]:
            with self.assertRaises(ValidationError):
                fit(data, TRAIN['config'])

    def test_input_model_deserialization_is_numeric_only(self):
        with self.assertRaises(ValidationError):
            predict([{'x': 1}], {'format': 'pickle', 'payload': 'ignored'}, 'result')

    def test_sdk_pins_digest_and_host_selects_ai_tool_scope(self):
        revision = secrets.token_hex(16)
        from uuid import UUID
        revision = str(UUID(revision))
        core = Mock(organization_id='organization', environment='sandbox')
        core.request.return_value = {'organization_id': 'organization', 'environment': 'sandbox', 'revision_id': revision,
            'digest': checksum(TRANSFORM), 'spec': TRANSFORM, 'published': True}
        sdk = Development(core)
        self.assertEqual(sdk.revision(revision, digest=checksum(TRANSFORM))['spec'], TRANSFORM)
        with self.assertRaises(ValueError):
            sdk.revision(revision, digest=secrets.token_hex(32))
        with self.assertRaises(ValueError):
            sdk.invoke_tool('core_development_propose', {'spec': TRANSFORM, 'object_id': revision}, object_id=revision, expected_revision=1)
        sdk.invoke_tool('core_development_propose', {'spec': TRANSFORM}, object_id=revision, expected_revision=1)
        self.assertEqual(core.request.call_args.kwargs['json'], {'revision': 1, 'spec': TRANSFORM})


class IntegrationAdapterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from platform_services.tests.test_integrations import IntegrationTests
        IntegrationTests.setUpTestData.__func__(cls)
        cls.principal = Principal(user=cls.admin)
        cls.installation.granted_capabilities = CAPS
        cls.installation.save(update_fields=['granted_capabilities'])
        cls.source = Principal(credential=cls.credential)

    def test_connection_object_resolves_scoped_descriptor_without_credentials(self):
        obj = create_object(self.org, self.principal, {'slug': 'integration-object', 'name': 'Integration object', 'kind': 'connection',
            'installation_id': str(self.installation.pk), 'environment_id': str(self.environment.pk),
            'spec': {'operator': 'integration', 'inputs': {}, 'config': {'integration_id': str(self.binding.pk)}}})
        revision = obj.versions.get()
        publish_revision(revision, self.principal)
        run = run_revision(revision, self.source, {'request_key': 'integration-descriptor', 'inputs': {}})
        self.assertEqual(run.result['type'], 'connection')
        self.assertEqual(run.result['value']['configuration'], self.connection.configuration)
        self.assertNotIn(self.secret, json.dumps(run_document(run, self.source)))
        bundle = json.dumps(export_bundle(revision, self.source))
        self.assertNotIn(self.secret, bundle)
        self.assertNotIn(self.connection.encrypted_credential, bundle)
        self.assertNotIn('https://data.example.test/api', bundle)
        self.binding.allow_inheritance = False
        self.binding.save(update_fields=['allow_inheritance'])
        with self.assertRaises(PermissionDenied):
            run_document(run, self.source)


class ConcurrentDevelopmentTests(TransactionTestCase):
    def test_identical_concurrent_run_executes_once(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent admission requires the production PostgreSQL row locks.')
        from concurrent.futures import ThreadPoolExecutor
        fixtures(self)
        owner = Installation.objects.get(organization=self.org, package__slug='core-workflows')
        obj = create_object(self.org, self.manager, {'slug': 'concurrent-object', 'name': 'Concurrent object', 'kind': 'pipeline',
            'installation_id': str(owner.pk), 'environment_id': str(self.environment.pk), 'spec': TRANSFORM})
        revision = obj.versions.get()
        publish_revision(revision, self.manager)
        def execute():
            close_old_connections()
            try:
                actor = Principal(user=type(self.admin).objects.get(pk=self.admin.pk))
                return str(run_revision(revision, actor, {'request_key': 'concurrent-request', 'inputs': {'rows': [{'x': 1}]}}).pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: execute(), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(DevelopmentRun.objects.count(), 1)
        self.assertEqual(AIUsageMeter.objects.get(organization=self.org, subject='user:' + str(self.admin.pk)).used['requests'], 1)
