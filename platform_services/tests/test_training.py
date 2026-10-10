from copy import deepcopy
from datetime import timedelta
import json
import secrets
from unittest.mock import patch
import uuid

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core_client import CoreClient, CoreServiceError
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, Role, ServicePermission, User
from platform_services.agent_identity import issue_credential
from platform_services.catalog import seed_catalog
from platform_services.models import (AgentBinding, AgentDefinition, AgentTask, DataRecord, Dataset, MarketplacePackage,
    Pipeline, TrainingEvent, TrainingFeedback, TrainingRun, TrainingScenario, Workflow, WorkflowRun)
from platform_services.services import install_core, install_package, issue_workload_credential
from platform_services.training import enqueue, process_one
from platform_services.training_plan import evaluate, generate, validate_plan
from platform_services.workflows import process_one as process_workflow


PLAN = {'seed': 42, 'cases': [
    {'name': 'nominal', 'count': 3, 'fields': {'subject': {'kind': 'sequence', 'prefix': 'Simulated report '}},
     'expected_ingest': 'accept', 'assertions': [{'field': 'title', 'operator': 'equals_input', 'input_field': 'subject'}]},
    {'name': 'invalid-title', 'count': 1, 'fields': {'subject': {'kind': 'constant', 'value': 123}}, 'expected_ingest': 'reject'},
]}
CAPABILITIES = ['training.run', 'training.read', 'training.manage', 'training.evaluate', 'data.read', 'data.write',
    'agents.run', 'workflows.run', 'workflows.read']


class TrainingPlanTests(SimpleTestCase):
    def test_every_generator_is_reproducible_and_bounded(self):
        plan = {'seed': 123, 'cases': [{'name': 'all-generators', 'count': 2, 'expected_ingest': 'accept', 'fields': {
            'constant': {'kind': 'constant', 'value': {'nested': True}}, 'choice': {'kind': 'choice', 'values': ['a', 'b']},
            'integer': {'kind': 'integer', 'min': 1, 'max': 3}, 'number': {'kind': 'number', 'min': 1.1, 'max': 1.5},
            'boolean': {'kind': 'boolean', 'probability': 1}, 'sequence': {'kind': 'sequence', 'prefix': 'sim-', 'start': 5},
            'uuid': {'kind': 'uuid'}, 'email': {'kind': 'email'},
            'timestamp': {'kind': 'timestamp', 'start': '2026-01-01T00:00:00Z', 'step_seconds': 60}}}]}
        validate_plan(plan)
        first = generate(plan, 0)[1]
        self.assertEqual(first, generate(deepcopy(plan), 0)[1])
        self.assertNotEqual(first['uuid'], generate(plan, 1)[1]['uuid'])
        self.assertEqual(first['sequence'], 'sim-5')
        self.assertTrue(first['email'].endswith('@example.test'))
        self.assertEqual(generate(plan, 1)[1]['timestamp'], '2026-01-01T00:01:00Z')
        self.assertTrue(first['boolean'])

    def test_plan_rejects_code_urls_unbounded_generation_and_nonfinite_values(self):
        invalid = []
        for changes in [{'kind': 'python', 'code': 'print(1)'}, {'kind': 'remote', 'url': 'https://example.test'},
                {'kind': 'integer', 'min': 9, 'max': 1}, {'kind': 'timestamp', 'start': '2026-01-01'},
                {'kind': 'constant', 'value': float('nan')}]:
            plan = deepcopy(PLAN)
            plan['cases'][0]['fields']['subject'] = changes
            invalid.append(plan)
        plan = deepcopy(PLAN)
        plan['cases'] = [{**deepcopy(plan['cases'][0]), 'name': str(index), 'count': 50} for index in range(5)]
        invalid.extend([plan, {**PLAN, 'credentials': 'model-selected'}, {**PLAN, 'seed': True}])
        for plan in invalid:
            with self.subTest(plan=repr(plan)[:80]), self.assertRaises(ValidationError):
                validate_plan(plan)

    def test_assertions_distinguish_boolean_integer_and_expected_rejection(self):
        case = {'expected_ingest': 'accept', 'assertions': [{'field': 'nested.value', 'operator': 'equals', 'value': True}]}
        self.assertFalse(evaluate(case, {}, {'nested': {'value': 1}}, True)['passed'])
        self.assertTrue(evaluate(case, {}, {'nested': {'value': True}}, True)['passed'])
        self.assertTrue(evaluate({'expected_ingest': 'reject'}, {}, None, False)['passed'])

    def test_model_cannot_choose_destinations_or_identity_in_training_tools(self):
        client = CoreClient('https://core.example.test/api/platform/v1', secrets.token_urlsafe(48), str(uuid.uuid4()))
        with patch.object(client, 'request') as request:
            for tool, arguments in [('core_training_create', {'goal': 'Test', 'plan': PLAN, 'pipeline': 'production'}),
                    ('core_training_run', {'scenario_id': str(uuid.uuid4())}),
                    ('core_training_feedback', {'verdict': 'pass', 'rationale': 'Done', 'actor_id': 'admin'})]:
                with self.assertRaises(ValueError):
                    client.invoke_training_tool(tool, arguments)
            request.assert_not_called()
        self.assertEqual(len(client.training_tool_definitions()), 5)


class TrainingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Training organization', slug='training-org')
        cls.other = Organization.objects.create(name='Other organization', slug='training-other')
        cls.admin = User.objects.create_user('training-admin@example.test')
        cls.member = User.objects.create_user('training-member@example.test')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        Membership.objects.create(organization=cls.org, user=cls.member)
        install_core(cls.org)
        install_core(cls.other)
        cls.package = MarketplacePackage.objects.create(slug='training-consumer', name='Training consumer', kind='application',
            description='Training consumer', manifest={'image': 'registry.example.test/training@sha256:' + 'a' * 64, 'capabilities': CAPABILITIES})
        cls.installation = install_package(cls.org, cls.package, CAPABILITIES)
        cls.foreign = install_package(cls.other, cls.package, CAPABILITIES)
        cls.credential, cls.raw = issue_workload_credential(cls.installation, 'Training consumer')
        _, cls.foreign_raw = issue_workload_credential(cls.foreign, 'Foreign consumer')
        cls.role = Role.objects.create(organization=cls.org, slug='training-agent', name='Training agent')
        cls.role.permissions.set(ServicePermission.objects.filter(service__organization=cls.org,
            service__installation__package__kind='core', codename__in=[name.replace('.', '-') for name in CAPABILITIES]))
        cls.agent = AgentDefinition.objects.create(organization=cls.org, slug='coordinator', name='Coordinator', kind='inspect')
        cls.binding = AgentBinding.objects.create(account=cls.agent.account, installation=cls.installation, environment='development')
        cls.binding.roles.add(cls.role)
        cls.agent_credential, cls.agent_raw = issue_credential(cls.binding, Principal(user=cls.admin))
        cls.dataset = Dataset.objects.create(organization=cls.org, slug='simulated-events', name='Simulated events', data_class='synthetic',
            schema={'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title'], 'additionalProperties': False})
        cls.output_dataset = Dataset.objects.create(organization=cls.org, slug='simulated-results', name='Simulated workflow output', data_class='synthetic')
        cls.pipeline = Pipeline.objects.create(organization=cls.org, slug='simulation-input', name='Simulation input', dataset=cls.dataset,
            transforms=[{'operation': 'rename', 'fields': {'subject': 'title'}}])
        cls.publisher = AgentDefinition.objects.create(organization=cls.org, slug='sim-publisher', name='Simulation publisher', kind='publish',
            configuration={'dataset': cls.output_dataset.slug})
        cls.publisher_binding = AgentBinding.objects.create(account=cls.publisher.account, installation=cls.installation, environment='development')
        cls.publisher_binding.roles.add(cls.role)
        cls.workflow = Workflow.objects.create(organization=cls.org, slug='simulation-workflow', name='Simulation workflow', steps=[cls.publisher.slug])
        cls.scenario = TrainingScenario.objects.create(organization=cls.org, slug='reports', name='Report training', goal='Test valid reports and reject invalid titles.',
            plan=deepcopy(PLAN), pipeline=cls.pipeline, workflow=cls.workflow, runner_binding=cls.binding)

    def api(self, path, data=None, raw=None, method='POST'):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (raw or self.raw)}
        if method == 'GET':
            return self.client.get('/api/platform/v1' + path, **headers)
        return self.client.post('/api/platform/v1' + path, json.dumps(data or {}), content_type='application/json', **headers)

    def queued(self, request_id='training-1', raw=None):
        result = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': request_id}, raw)
        self.assertEqual(result.status_code, 202, result.content)
        return TrainingRun.objects.get(pk=result.json()['run_id'])

    def finish(self, run):
        for _ in range(40):
            TrainingRun.objects.filter(pk=run.pk, status='waiting').update(lease_until=timezone.now() - timedelta(seconds=1))
            process_workflow()
            process_one()
            run.refresh_from_db()
            if run.status not in ['queued', 'running', 'waiting']:
                break
        self.assertEqual(run.status, 'completed', run.error)
        return run

    def test_simulated_data_flows_through_real_pipeline_workflow_and_consumer_api(self):
        run = self.finish(self.queued())
        self.assertEqual(run.result['total'], 4)
        self.assertTrue(run.result['all_passed'])
        self.assertEqual(run.result['cases']['invalid-title']['rejected'], 1)
        self.assertEqual(self.dataset.records.count(), 3)
        self.assertEqual(self.output_dataset.records.count(), 3)
        records = self.api('/datasets/simulated-events/records/', method='GET').json()['records']
        self.assertEqual(records[0]['data']['title'], 'Simulated report 0')
        self.assertTrue(records[0]['synthetic'])
        self.assertEqual(records[0]['training_run_id'], str(run.pk))
        self.assertTrue(all(record.synthetic and record.training_run_id == run.pk for record in self.output_dataset.records.all()))
        self.assertEqual(WorkflowRun.objects.filter(training_run=run, status='succeeded').count(), 3)
        self.assertEqual(set(run.workflow_runs.values_list('step_results__agent_account_id', flat=True)), {self.publisher.account_id})

    def test_seeded_runs_and_idempotent_submission_are_reproducible(self):
        first = self.queued()
        repeat = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': 'training-1'})
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()['run_id'], str(first.pk))
        self.finish(first)
        second = self.finish(self.queued('training-2'))
        self.assertEqual(list(first.events.values_list('input', flat=True)), list(second.events.values_list('input', flat=True)))
        self.assertEqual(first.result, second.result)

    def test_ai_account_authors_runs_reviews_and_iterates_scenarios(self):
        created = self.api('/training/scenarios/create/', {'slug': 'ai-plan', 'name': 'AI plan', 'goal': 'Exercise nominal and invalid records.',
            'plan': PLAN, 'pipeline': self.pipeline.slug, 'workflow': self.workflow.slug}, self.agent_raw)
        self.assertEqual(created.status_code, 201, created.content)
        scenario_id = created.json()['scenario']['id']
        result = self.api(f'/training/scenarios/{scenario_id}/runs/', {'request_id': 'ai-plan-run'}, self.agent_raw)
        self.assertEqual(result.status_code, 202, result.content)
        run = self.finish(TrainingRun.objects.get(pk=result.json()['run_id']))
        feedback = self.api(f'/training/runs/{run.pk}/feedback/', {'request_id': 'ai-review', 'verdict': 'pass',
            'rationale': 'Nominal data and expected rejections match the training goal.', 'suggestions': ['Add a boundary case.']}, self.agent_raw)
        self.assertEqual(feedback.status_code, 201)
        entry = TrainingFeedback.objects.get()
        self.assertEqual(entry.principal_kind, 'agent')
        self.assertEqual(entry.principal_id, self.agent.account_id)
        result = self.api(f'/training/runs/{run.pk}/', raw=self.agent_raw, method='GET').json()
        self.assertEqual(result['runner_agent_id'], str(self.agent.account_id))
        self.assertTrue(result['result']['all_passed'])
        changed = self.api(f'/training/scenarios/{scenario_id}/update/', {'revision': 1, 'goal': 'Add more boundary coverage.', 'plan': PLAN}, self.agent_raw)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['scenario']['revision'], 2)
        stale = self.api(f'/training/scenarios/{scenario_id}/update/', {'revision': 1, 'plan': PLAN}, self.agent_raw)
        self.assertEqual(stale.status_code, 409)

    def test_operational_destinations_and_external_side_effects_are_rejected(self):
        operational = Dataset.objects.create(organization=self.org, slug='operational', name='Operational data')
        pipeline = Pipeline.objects.create(organization=self.org, slug='operational', name='Operational pipeline', dataset=operational)
        self.scenario.pipeline = pipeline
        with self.assertRaises(ValidationError):
            self.scenario.full_clean()
        self.scenario.refresh_from_db()
        self.publisher.configuration = {'dataset': operational.slug}
        self.publisher.save()
        result = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': 'unsafe'})
        self.assertEqual(result.status_code, 400, result.content)
        self.assertFalse(TrainingRun.objects.exists())
        self.assertFalse(operational.records.exists())

    def test_production_and_cross_tenant_agent_routing_are_rejected(self):
        production = AgentBinding.objects.create(account=self.agent.account, installation=self.installation, environment='production')
        self.scenario.runner_binding = production
        with self.assertRaises(ValidationError):
            self.scenario.full_clean()
        self.scenario.runner_binding = self.binding
        self.scenario.organization = self.other
        with self.assertRaises(ValidationError):
            self.scenario.full_clean()
        response = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': 'foreign'}, self.foreign_raw)
        self.assertEqual(response.status_code, 404)

    def test_caller_cannot_borrow_another_applications_agent_approval(self):
        package = MarketplacePackage.objects.create(slug='second-consumer', name='Other app', kind='application', description='Other app',
            manifest={'image': 'registry.example.test/other@sha256:' + 'a' * 64, 'capabilities': CAPABILITIES})
        installation = install_package(self.org, package, CAPABILITIES)
        _, raw = issue_workload_credential(installation, 'Unapproved consumer')
        result = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': 'borrowed'}, raw)
        self.assertEqual(result.status_code, 403)
        self.assertFalse(TrainingRun.objects.exists())

    def test_revoking_runner_roles_stops_generation(self):
        run = self.queued()
        self.binding.roles.clear()
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(DataRecord.objects.exists())

    def test_revoked_submitter_and_agent_token_cannot_keep_training_access(self):
        run = self.queued(raw=self.agent_raw)
        self.agent_credential.active = False
        self.agent_credential.save()
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(DataRecord.objects.exists())
        self.assertEqual(self.api(f'/training/runs/{run.pk}/', raw=self.agent_raw, method='GET').status_code, 401)

    def test_cancellation_prevents_pending_workflow_side_effects(self):
        run = self.queued()
        process_one()
        self.assertEqual(run.events.get().workflow_run.status, 'queued')
        result = self.api(f'/training/runs/{run.pk}/cancel/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'cancelled')
        self.assertFalse(process_workflow())
        self.assertFalse(process_one())
        self.assertFalse(self.output_dataset.records.exists())
        self.assertEqual(self.dataset.records.count(), 1)

    def test_expiring_uat_task_limits_data_generation_and_workflow_targets(self):
        task = AgentTask.objects.create(organization=self.org, name='UAT training', description='Training-only data', installation=self.installation,
            environment='uat', expires_at=timezone.now() + timedelta(minutes=30), approved_by=self.admin,
            resources={'datasets': [str(self.dataset.pk), str(self.output_dataset.pk)]})
        binding = AgentBinding.objects.create(account=self.agent.account, installation=self.installation, environment='uat', task=task)
        binding.roles.add(self.role)
        publisher = AgentBinding.objects.create(account=self.publisher.account, installation=self.installation, environment='uat', task=task)
        publisher.roles.add(self.role)
        scenario = TrainingScenario.objects.create(organization=self.org, slug='uat', name='UAT', goal='UAT flow', plan=PLAN,
            pipeline=self.pipeline, workflow=self.workflow, runner_binding=binding)
        run, _ = enqueue(scenario, 'uat-training', Principal(user=self.admin))
        AgentTask.objects.filter(pk=task.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(DataRecord.objects.exists())

    def test_resource_tags_protect_results_and_halt_queued_work(self):
        run = self.queued()
        tag = PermissionTag.objects.create(organization=self.org, slug='sim-restricted', name='Restricted simulation', allow_workloads=True)
        self.output_dataset.permission_tags.add(tag)
        self.assertEqual(self.api(f'/training/runs/{run.pk}/', method='GET').status_code, 403)
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(DataRecord.objects.exists())

    def test_workflow_revocation_after_ingestion_prevents_publication(self):
        run = self.queued()
        process_one()
        self.publisher_binding.active = False
        self.publisher_binding.save()
        process_workflow()
        self.assertFalse(self.output_dataset.records.exists())
        self.assertEqual(run.workflow_runs.get().status, 'failed')

    def test_immutable_synthetic_classification_and_live_destination_checks(self):
        self.dataset.data_class = 'operational'
        with self.assertRaises(ValidationError):
            self.dataset.full_clean()
        self.dataset.refresh_from_db()
        run = self.queued()
        Dataset.objects.filter(pk=self.dataset.pk).update(data_class='operational')
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(DataRecord.objects.exists())

    def test_snapshot_preserves_plan_transform_and_tool_reproducibility(self):
        run = self.queued()
        self.scenario.plan = {'seed': 99, 'cases': [{**deepcopy(PLAN['cases'][0]), 'count': 1}]}
        self.scenario.revision = 2
        self.scenario.save()
        self.pipeline.transforms = [{'operation': 'select', 'fields': []}]
        self.pipeline.save()
        self.finish(run)
        self.assertEqual(run.snapshot['revision'], 1)
        self.assertEqual(run.result['total'], 4)
        self.assertEqual(self.dataset.records.first().data, {'title': 'Simulated report 0'})

    def test_expired_lease_resumes_without_duplicate_records_or_workflows(self):
        run = self.queued()
        process_one()
        TrainingRun.objects.filter(pk=run.pk).update(lease_owner=uuid.uuid4(), lease_until=timezone.now() - timedelta(seconds=1), status='running')
        self.finish(run)
        self.assertEqual(self.dataset.records.count(), 3)
        self.assertEqual(run.workflow_runs.count(), 3)
        self.assertEqual(run.events.count(), 4)

    def test_evaluation_failure_is_separate_from_operational_failure(self):
        self.scenario.plan = deepcopy(PLAN)
        self.scenario.plan['cases'][0]['assertions'] = [{'field': 'title', 'operator': 'equals', 'value': 'Incorrect expectation'}]
        self.scenario.save()
        run = self.finish(self.queued())
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result['failed'], 3)
        self.assertFalse(run.result['all_passed'])

    def test_feedback_is_idempotent_attributed_and_does_not_replace_reference_scores(self):
        run = self.finish(self.queued())
        values = {'request_id': 'review', 'verdict': 'fail', 'rationale': 'Try more edge cases.', 'suggestions': ['Add missing title cases.']}
        self.assertEqual(self.api(f'/training/runs/{run.pk}/feedback/', values, self.agent_raw).status_code, 201)
        self.assertEqual(self.api(f'/training/runs/{run.pk}/feedback/', values, self.agent_raw).status_code, 200)
        values['rationale'] = 'Changed review'
        self.assertEqual(self.api(f'/training/runs/{run.pk}/feedback/', values, self.agent_raw).status_code, 409)
        self.assertTrue(TrainingRun.objects.get(pk=run.pk).result['all_passed'])
        self.assertEqual(TrainingFeedback.objects.count(), 1)

    def test_generators_do_not_fetch_remote_schema_references(self):
        self.dataset.schema = {'$ref': 'https://internal.example.test/schema'}
        self.dataset.save()
        with patch('requests.get') as remote:
            result = self.api(f'/training/scenarios/{self.scenario.pk}/runs/', {'request_id': 'remote-schema'})
            self.assertEqual(result.status_code, 400)
            remote.assert_not_called()

    def test_ai_feedback_and_run_audits_exclude_generated_record_bodies(self):
        run = self.finish(self.queued())
        events = list(AuditEvent.objects.filter(action__startswith='training.').values())
        self.assertTrue(events)
        self.assertNotIn('Simulated report 0', str(events))
        generation = AuditEvent.objects.filter(action='training.event_generated')
        self.assertEqual(set(generation.values_list('principal_kind', flat=True)), {'agent'})
        self.assertEqual(set(generation.values_list('principal_id', flat=True)), {self.agent.account_id})

    def test_admin_panel_and_csrf_protection(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('training', args=[self.org.slug])).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('training', args=[self.org.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse('training_scenario_edit', args=[self.org.slug, self.scenario.pk])).status_code, 200)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(reverse('training_start', args=[self.org.slug, self.scenario.pk])).status_code, 403)
        run = self.queued()
        page = reverse('training_run', args=[self.org.slug, run.pk])
        self.assertEqual(self.client.get(page).status_code, 200)
        self.assertEqual(browser.post(page, {'verdict': 'pass', 'rationale': 'Spoofed'}).status_code, 403)
        response = self.client.post(page, {'request_id': 'human-review', 'verdict': 'review', 'rationale': 'Review the boundary cases.', 'suggestions': '[]'})
        self.assertEqual(response.status_code, 302, response.content)
        feedback = TrainingFeedback.objects.get()
        self.assertEqual(feedback.principal_kind, 'user')
        self.assertEqual(feedback.principal_id, self.admin.pk)


class LiveTrainingTests(LiveServerTestCase):
    def test_ai_tools_and_remote_consumer_use_real_account_pipeline_and_training_apis(self):
        seed_catalog(apps, None)
        org = Organization.objects.create(name='Live training', slug='live-training')
        install_core(org)
        admin = User.objects.create_user('live-training-admin@example.test')
        Membership.objects.create(organization=org, user=admin, kind='administrator')
        package = MarketplacePackage.objects.create(slug='live-training-app', name='Live training app', kind='application', description='Live consumer',
            manifest={'image': 'registry.example.test/training@sha256:' + 'a' * 64, 'capabilities': CAPABILITIES})
        installation = install_package(org, package, CAPABILITIES)
        _, raw = issue_workload_credential(installation, 'Live consumer')
        role = Role.objects.create(organization=org, slug='live-training', name='Live training')
        role.permissions.set(ServicePermission.objects.filter(service__organization=org, service__installation__package__kind='core',
            codename__in=[c.replace('.', '-') for c in CAPABILITIES]))
        agent = AgentDefinition.objects.create(organization=org, slug='live-coordinator', name='Coordinator', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=installation, environment='development')
        binding.roles.add(role)
        dataset = Dataset.objects.create(organization=org, slug='live-sim', name='Live synthetic', data_class='synthetic')
        Pipeline.objects.create(organization=org, slug='live-input', name='Live input', dataset=dataset)
        with override_settings(ISSUER=self.live_server_url):
            host = CoreClient(self.live_server_url + '/api/platform/v1', raw, str(org.pk), allow_local_http=True)
            own = host.agent_client(binding.pk, environment='development')
            plan = {'seed': 12, 'cases': [{'name': 'nominal', 'count': 2, 'fields': {'value': {'kind': 'integer', 'min': 1, 'max': 10}},
                'expected_ingest': 'accept', 'assertions': [{'field': 'value', 'operator': 'type', 'value': 'integer'}]}]}
            created = own.invoke_training_tool('core_training_create', {'goal': 'Exercise synthetic integer samples.', 'plan': plan},
                scenario_slug='live-plan', scenario_name='Live AI plan', pipeline='live-input')
            scenario = created['scenario']['id']
            started = own.invoke_training_tool('core_training_run', {}, scenario_id=scenario, request_id='live-training')
            call_command('core_worker', once=True)
            result = own.invoke_training_tool('core_training_result', {}, run_id=started['run_id'])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['result']['passed'], 2)
            self.assertTrue(host.records('live-sim')['records'][0]['synthetic'])
            episodes = list(own.training_episodes(started['run_id']))
            self.assertEqual(len(episodes), 2)
            self.assertTrue(episodes[0]['synthetic'])
            self.assertEqual(episodes[0]['runner_agent_id'], str(agent.account_id))
            feedback = own.invoke_training_tool('core_training_feedback', {'verdict': 'pass', 'rationale': 'Both generated samples passed.'},
                run_id=started['run_id'], request_id='live-feedback')
            self.assertTrue(feedback['created'])
            binding.active = False
            binding.save()
            with self.assertRaises(CoreServiceError):
                own.training_result(started['run_id'])
