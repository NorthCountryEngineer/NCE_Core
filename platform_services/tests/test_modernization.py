import copy
import json
import secrets
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from identity.access import Principal, decision
from identity.models import Membership, Organization, User
from platform_services.environments import create_environment
from platform_services.inventory import update_inventory
from platform_services.kubernetes import deployment_spec
from platform_services.models import AgentDefinition, Dataset, Deployment, DeploymentConfiguration, MarketplacePackage, RuntimeApplication, RuntimeNode, WorkloadCredential
from platform_services.modernization import adopt_application
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential
from runtime_connector import KubernetesAdapter
from scripts.environment_bundle import bundle


class EnvironmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Modernization customer', slug='modern-customer')
        cls.other = Organization.objects.create(name='Other customer', slug='other-customer')
        cls.admin = User.objects.create_user('modern-admin@example.test')
        Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        cls.member = User.objects.create_user('modern-member@example.test')
        cls.membership = Membership.objects.create(organization=cls.org, user=cls.member)
        install_core(cls.org)
        cls.environments = {}
        for name in ['sandbox', 'development', 'production']:
            cls.environments[name] = create_environment(cls.org, name=name, namespace='modern-' + name,
                application_origin='https://' + name + '.example.test')
        cls.package = MarketplacePackage.objects.create(slug='environment-app', name='Environment app', kind='application', description='Environment application',
            manifest={'image': 'registry.example.test/app@sha256:' + 'a' * 64, 'capabilities': ['data.read', 'data.write', 'agents.run', 'chat.manage', 'chat.read', 'chat.send']})
        cls.installation = install_package(cls.org, cls.package, cls.package.manifest['capabilities'])
        cls.credential, cls.token = issue_workload_credential(cls.installation, 'Sandbox', environment=cls.environments['sandbox'])
        cls.datasets = {}
        for name, environment in cls.environments.items():
            dataset = Dataset.objects.create(organization=cls.org, slug=name, name=name)
            dataset.permission_tags.add(environment.boundary_tag)
            cls.datasets[name] = dataset
        cls.runtime_token = 'cr_' + secrets.token_urlsafe(48)
        cls.node = RuntimeNode.objects.create(organization=cls.org, name='Sandbox runtime', namespace=cls.environments['sandbox'].namespace,
            environment=cls.environments['sandbox'], credential_hash=WorkloadCredential.digest(cls.runtime_token), last_seen=timezone.now())
        DeploymentConfiguration.objects.create(organization=cls.org, environment='sandbox', namespace=cls.node.namespace,
            application_origin='https://sandbox.example.test')

    def api(self, path, values=None, token=None):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (token or self.token)}
        if values is None:
            return self.client.get('/api/platform/v1' + path, **headers)
        return self.client.post('/api/platform/v1' + path, json.dumps(values), content_type='application/json', **headers)

    def test_workload_isolation_cannot_be_overridden_by_tag_grants(self):
        self.assertEqual(self.api('/datasets/sandbox/records/').status_code, 200)
        self.installation.access_tags.add(self.environments['production'].boundary_tag)
        for name in ['development', 'production']:
            self.assertEqual(self.api('/datasets/' + name + '/records/').status_code, 403)
        dataset = Dataset.objects.create(organization=self.org, slug='unassigned', name='Unassigned')
        self.assertEqual(self.api('/datasets/unassigned/records/').status_code, 403)
        _, legacy = issue_workload_credential(self.installation, 'Legacy')
        self.assertEqual(self.api('/datasets/unassigned/records/', token=legacy).status_code, 403)
        self.assertEqual(self.api('/datasets/sandbox/records/', token=legacy).status_code, 403)

    def test_environment_disable_rechecks_held_credentials(self):
        principal = Principal(credential=self.credential)
        self.assertTrue(decision(principal, self.datasets['sandbox'])[0])
        environment = self.environments['sandbox']
        environment.active = False
        environment.save(update_fields=['active'])
        self.assertFalse(decision(principal, self.datasets['sandbox'])[0])
        self.assertEqual(self.api('/integration/').status_code, 403)

    def test_humans_need_grants_and_admins_manage_environment(self):
        principal = Principal(user=self.member)
        self.assertFalse(decision(principal, self.datasets['production'])[0])
        self.membership.access_tags.add(self.environments['production'].boundary_tag)
        self.assertTrue(decision(principal, self.datasets['production'])[0])
        self.assertTrue(decision(Principal(user=self.admin), self.datasets['production'])[0])

    def test_namespaces_are_distinct_and_identity_immutable(self):
        with self.assertRaises(ValidationError):
            create_environment(self.org, name='uat', namespace=self.node.namespace, application_origin='https://uat.example.test')
        environment = self.environments['sandbox']
        environment.namespace = 'other'
        with self.assertRaises(ValidationError):
            environment.full_clean()
        self.node.environment = self.environments['production']
        with self.assertRaises(ValidationError):
            self.node.full_clean()

    def test_deployments_get_environment_scoped_identity_and_labels(self):
        deployment = create_deployment(self.installation, self.node, 'https://sandbox.example.test')
        self.assertEqual(deployment.environment, self.environments['sandbox'])
        credential = deployment.credentials.get(active=True)
        self.assertEqual(credential.environment, deployment.environment)
        spec = deployment_spec(deployment)
        self.assertEqual(spec['environment'], 'sandbox')
        for manifest in spec['manifests']:
            self.assertEqual(manifest['metadata']['labels']['core.platform/environment'], 'sandbox')
        discovery = self.api('/integration/').json()
        self.assertEqual(discovery['environment'], 'sandbox')

    def test_production_does_not_inherit_sandbox_automatic_deployment(self):
        config = self.org.deployment_configuration
        config.auto_deploy = True
        config.save()
        node = RuntimeNode.objects.create(organization=self.org, name='Production runtime', namespace=self.environments['production'].namespace,
            environment=self.environments['production'], credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        self.client.force_login(self.admin)
        response = self.client.post(reverse('deploy_installation', args=[self.org.slug, self.installation.pk]),
            {'node': str(node.pk), 'endpoint': 'https://production.example.test'})
        self.assertEqual(response.status_code, 302)
        deployment = Deployment.objects.get(node=node)
        self.assertEqual(deployment.environment.name, 'production')
        self.assertEqual(deployment.desired_state, 'stopped')

    def test_workflow_environment_spoofing_is_denied(self):
        agent = AgentDefinition.objects.create(organization=self.org, name='Inspect', slug='inspect', kind='inspect')
        agent.permission_tags.add(self.environments['sandbox'].boundary_tag)
        result = self.api('/agents/inspect/runs/', {'input': {}, 'request_id': 'spoof', 'environment': 'production'})
        self.assertEqual(result.status_code, 403)

    def test_sandbox_cannot_exchange_production_agent_credentials(self):
        from platform_services.models import AgentBinding
        agent = AgentDefinition.objects.create(organization=self.org, name='Inspect', slug='inspect', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='production')
        result = self.api('/agent-credentials/exchange/', {'binding_id': str(binding.pk)})
        self.assertEqual(result.status_code, 403)

    def test_oauth_tokens_and_resource_checks_are_bound_to_application_environment(self):
        from datetime import timedelta
        from oauth2_provider.models import get_access_token_model
        from identity.views import token_claims
        sandbox = create_deployment(self.installation, self.node, 'https://sandbox.example.test')
        node = RuntimeNode.objects.create(organization=self.org, name='Production runtime', namespace=self.environments['production'].namespace,
            environment=self.environments['production'], credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        production = create_deployment(self.installation, node, 'https://production.example.test')
        raw = secrets.token_urlsafe(48)
        get_access_token_model().objects.create(user=self.admin, application=sandbox.application, token=raw,
            expires=timezone.now() + timedelta(minutes=5), scope='openid authorization')
        claims = token_claims(raw, sandbox.application)
        self.assertEqual(claims['environment'], 'sandbox')
        self.assertIsNone(token_claims(raw, production.application))
        principal = Principal(user=self.admin, application=sandbox.application)
        self.assertTrue(decision(principal, self.datasets['sandbox'])[0])
        self.assertFalse(decision(principal, self.datasets['production'])[0])
        unassigned = Dataset.objects.create(organization=self.org, slug='unassigned-oauth', name='Unassigned')
        self.assertFalse(decision(principal, unassigned)[0])
        self.environments['sandbox'].active = False
        self.environments['sandbox'].save()
        self.assertIsNone(token_claims(raw, sandbox.application))

    def test_environment_controls_apply_to_agent_accounts(self):
        from platform_services.models import AgentBinding
        from identity.models import PermissionTag
        agent = AgentDefinition.objects.create(organization=self.org, name='Inspect', slug='inspect', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='sandbox')
        principal = Principal(agent_binding=binding)
        self.assertTrue(decision(principal, binding)[0])
        restrictive = PermissionTag.objects.create(organization=self.org, slug='human-only', name='Human only')
        self.environments['sandbox'].permission_tags.add(restrictive)
        self.assertFalse(decision(principal, binding)[0])

    def test_agent_approval_without_registered_environment_is_denied(self):
        from platform_services.models import AgentBinding
        from platform_services.agent_identity import binding_valid
        agent = AgentDefinition.objects.create(organization=self.org, name='Inspect', slug='inspect', kind='inspect')
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='staging')
        self.assertFalse(binding_valid(binding))
        with self.assertRaises(ValidationError):
            binding.full_clean()
        dataset = Dataset.objects.create(organization=self.org, name='Unassigned', slug='unassigned-agent')
        self.assertFalse(decision(Principal(agent_binding=binding), dataset)[0])

    def test_chat_created_by_application_is_bound_to_its_environment(self):
        result = self.api('/chat/conversations/create/', {'title': 'Sandbox room'})
        self.assertEqual(result.status_code, 201, result.content)
        from core_chat.models import Conversation
        conversation = Conversation.objects.get(pk=result.json()['conversation_id'])
        self.assertIn(self.environments['sandbox'].boundary_tag, conversation.permission_tags.all())
        self.assertEqual(self.api('/chat/conversations/' + str(conversation.pk) + '/messages/').status_code, 200)
        _, production = issue_workload_credential(self.installation, 'Production', environment=self.environments['production'])
        self.assertEqual(self.api('/chat/conversations/' + str(conversation.pk) + '/messages/', token=production).status_code, 403)

    def test_delegated_chat_user_token_cannot_change_the_host_environment(self):
        from datetime import timedelta
        from oauth2_provider.models import get_access_token_model
        from core_chat.models import Conversation
        node = RuntimeNode.objects.create(organization=self.org, name='Production runtime', namespace=self.environments['production'].namespace,
            environment=self.environments['production'], credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(self.installation, node, 'https://production.example.test')
        raw = secrets.token_urlsafe(48)
        get_access_token_model().objects.create(user=self.admin, application=deployment.application, token=raw,
            expires=timezone.now() + timedelta(minutes=5), scope='openid authorization')
        response = self.client.post('/api/platform/v1/chat/conversations/create/', json.dumps({'title': 'Wrong environment'}),
            content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + self.token, HTTP_X_CORE_USER_TOKEN=raw)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Conversation.objects.filter(title='Wrong environment').exists())

    def inventory(self):
        uid = str(uuid.uuid4())
        update_inventory(self.node, [{'name': 'legacy-application', 'uid': uid, 'containers': [{'name': 'web', 'image': 'registry.example.test/app:owner-release'}]}])
        return self.node.applications.get()

    def adopt(self, inventory=None):
        return adopt_application(self.org, inventory or self.inventory(), 'web', 'legacy-app', 'Modernized app',
            'https://sandbox.example.test', ['data.read'], '/oauth/callback', Principal(user=self.admin))

    def test_adoption_creates_private_marketplace_entry_and_integration(self):
        inventory = self.inventory()
        deployment = self.adopt(inventory)
        self.assertEqual(deployment.target, 'kubernetes-adopted')
        self.assertEqual(deployment.resource_uid, inventory.resource_uid)
        self.assertEqual(deployment.installation.package.organization, self.org)
        self.assertEqual(deployment.installation.granted_capabilities, ['data.read'])
        spec = deployment_spec(deployment)
        self.assertEqual(spec['mode'], 'adopted')
        self.assertNotIn('manifests', spec)
        self.assertNotIn('image', spec['integration'])
        self.assertEqual(spec['container_name'], 'web')
        self.assertTrue(spec['integration']['volumeMounts'][0]['readOnly'])
        with self.assertRaises(ValidationError):
            self.adopt(inventory)
        with self.assertRaises(ValidationError):
            install_package(self.other, deployment.installation.package)
        with self.assertRaises(ValidationError):
            create_deployment(deployment.installation, self.node, 'https://new.example.test')

    def test_stale_inventory_and_foreign_container_are_denied(self):
        from datetime import timedelta
        inventory = self.inventory()
        inventory.observed_at = timezone.now() - timedelta(minutes=5)
        inventory.save()
        with self.assertRaises(ValidationError):
            self.adopt(inventory)
        inventory.observed_at = timezone.now()
        inventory.save()
        with self.assertRaises(ValidationError):
            adopt_application(self.org, inventory, 'other', 'app', 'App', 'https://sandbox.example.test', [], '/oauth/callback', Principal(user=self.admin))

    def test_runtime_requires_matching_isolation_and_inventory_has_no_secret_fields(self):
        values = {'reports': [], 'inventory': [{'name': 'app', 'uid': str(uuid.uuid4()), 'containers': [{'name': 'web', 'image': 'registry.example.test/app:latest'}]}]}
        self.assertEqual(self.api('/runtime/sync/', values, self.runtime_token).status_code, 403)
        values['isolation'] = {'organization_id': str(self.org.pk), 'environment': 'production', 'default_deny': True}
        self.assertEqual(self.api('/runtime/sync/', values, self.runtime_token).status_code, 403)
        values['isolation']['environment'] = 'sandbox'
        response = self.api('/runtime/sync/', values, self.runtime_token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['environment'], 'sandbox')
        self.assertEqual(self.node.applications.count(), 1)
        values['inventory'][0]['containers'][0]['env'] = []
        self.assertEqual(self.api('/runtime/sync/', values, self.runtime_token).status_code, 400)
        raw = 'cr_' + secrets.token_urlsafe(48)
        RuntimeNode.objects.create(organization=self.org, name='Unplaced legacy runtime', namespace='unplaced',
            credential_hash=WorkloadCredential.digest(raw))
        self.assertEqual(self.api('/runtime/sync/', {'reports': []}, raw).status_code, 403)

    def test_modernization_ui_permissions_and_csrf(self):
        self.client.force_login(self.member)
        path = reverse('modernize_application', args=[self.org.slug])
        self.assertEqual(self.client.get(path).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(path).status_code, 200)
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(path, {}).status_code, 403)
        self.assertEqual(self.client.get(reverse('platform_environments', args=[self.org.slug])).status_code, 200)


class MigrationBackfillTests(TestCase):
    def test_existing_placement_and_credentials_are_preserved_and_custom_tags_not_repurposed(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from identity.models import PermissionTag
        org = Organization.objects.create(name='Existing organization', slug='existing-org')
        install_core(org)
        DeploymentConfiguration.objects.create(organization=org, environment='staging', namespace='existing-staging',
            application_origin='https://staging.example.test', auto_deploy=True)
        custom = PermissionTag.objects.create(organization=org, slug='environment-staging', name='Existing custom policy', requires_mfa=True)
        node = RuntimeNode.objects.create(organization=org, name='Existing runtime', namespace='existing-staging',
            credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        package = MarketplacePackage.objects.create(slug='existing-app', name='Existing app', kind='application', description='Existing app',
            manifest={'image': 'registry.example.test/app@sha256:' + 'a' * 64})
        installation = install_package(org, package)
        deployment = create_deployment(installation, node, 'https://staging.example.test')
        credential = installation.credentials.get(name='deployment:' + str(deployment.pk))
        original_hash, original_bundle = credential.token_hash, deployment.encrypted_bundle
        manual, _ = issue_workload_credential(installation, 'Existing manual key')
        importlib.import_module('platform_services.migrations.0016_backfill_environment_placement').backfill(
            apps, SimpleNamespace(connection=connection))
        environment = org.environments.get(name='staging')
        self.assertTrue(environment.auto_deploy)
        self.assertNotEqual(environment.boundary_tag_id, custom.pk)
        custom.refresh_from_db()
        self.assertTrue(custom.requires_mfa)
        node.refresh_from_db()
        deployment.refresh_from_db()
        credential.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(node.environment, environment)
        self.assertEqual(deployment.environment, environment)
        self.assertEqual(credential.environment, environment)
        self.assertEqual(credential.deployment, deployment)
        self.assertEqual(credential.token_hash, original_hash)
        self.assertEqual(deployment.encrypted_bundle, original_bundle)
        self.assertIsNone(manual.environment_id)
        self.assertFalse(decision(Principal(credential=manual), manual)[0])


class AdoptionConnectorTests(SimpleTestCase):
    def setUp(self):
        self.adapter = KubernetesAdapter('sandbox')
        self.labels = {'core.platform/organization': str(uuid.uuid4()), 'core.platform/deployment': str(uuid.uuid4()), 'core.platform/environment': 'sandbox'}
        self.name = 'core-' + uuid.uuid4().hex
        self.existing = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'legacy', 'namespace': 'sandbox', 'uid': str(uuid.uuid4()), 'resourceVersion': '42'},
            'spec': {'replicas': 3, 'selector': {'matchLabels': {'app': 'legacy'}}, 'template': {'metadata': {'labels': {'app': 'legacy'}},
                'spec': {'containers': [{'name': 'web', 'image': 'registry.example.test/app:release', 'env': [{'name': 'BUSINESS_SETTING', 'value': 'preserve'}],
                    'resources': {'limits': {'memory': '1Gi'}}, 'volumeMounts': [{'name': 'business', 'mountPath': '/data'}]},
                    {'name': 'sidecar', 'image': 'registry.example.test/sidecar:release'}], 'volumes': [{'name': 'business', 'emptyDir': {}}]}}},
            'status': {'replicas': 3, 'updatedReplicas': 3, 'readyReplicas': 3, 'availableReplicas': 3, 'observedGeneration': 1}}
        self.spec = {'mode': 'adopted', 'namespace': 'sandbox', 'organization_id': self.labels['core.platform/organization'],
            'deployment_id': self.labels['core.platform/deployment'], 'resource_name': 'legacy', 'resource_uid': self.existing['metadata']['uid'],
            'container_name': 'web', 'revision': 1, 'desired_state': 'running', 'labels': self.labels,
            'secret': {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': self.name, 'namespace': 'sandbox', 'labels': self.labels}, 'data': {}},
            'integration': {'name': 'web', 'env': [{'name': 'CORE_ENVIRONMENT', 'value': 'sandbox'}],
                'volumeMounts': [{'name': self.name, 'mountPath': '/run/core/integration', 'readOnly': True}]},
            'volume': {'name': self.name, 'secret': {'secretName': self.name}}}

    def patch_value(self):
        with patch.object(self.adapter, 'run', side_effect=[self.existing, {}, {}, {}, self.existing]) as run:
            report = self.adapter.reconcile(self.spec)
        self.assertNotEqual(report['state'], 'failed')
        mutations = [call for call in run.call_args_list if call.args[0][0] in {'apply', 'patch', 'scale'}]
        self.assertEqual([call.args[0][0] for call in mutations], ['apply', 'patch'])
        self.assertEqual(mutations[0].args[1]['kind'], 'Secret')
        return mutations[1].args[1]

    def test_strategic_patch_has_uid_version_preconditions_and_preserves_owner_fields(self):
        value = self.patch_value()
        self.assertEqual(value['metadata']['uid'], self.existing['metadata']['uid'])
        self.assertEqual(value['metadata']['resourceVersion'], '42')
        self.assertNotIn('replicas', value['spec'])
        self.assertNotIn('selector', value['spec'])
        self.assertNotIn('image', value['spec']['template']['spec']['containers'][0])
        # Exercise kubectl's actual strategic-merge engine locally, with no cluster calls.
        executable = Path('/tmp/nce-kubernetes-tools/kubectl')
        if executable.exists():
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'deployment.json'
                path.write_text(json.dumps(self.existing))
                result = subprocess.run([str(executable), 'patch', '--local', '-f', str(path), '--type', 'strategic', '--patch-file', '/dev/stdin', '-o', 'json'],
                    input=json.dumps(value), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, 'kubectl strategic patch failed')
                patched = json.loads(result.stdout)
                self.assertEqual(patched['spec']['replicas'], 3)
                containers = patched['spec']['template']['spec']['containers']
                self.assertEqual(len(containers), 2)
                web = next(item for item in containers if item['name'] == 'web')
                self.assertEqual(web['image'], self.existing['spec']['template']['spec']['containers'][0]['image'])
                self.assertIn({'name': 'BUSINESS_SETTING', 'value': 'preserve'}, web['env'])
                self.assertEqual(web['resources']['limits']['memory'], '1Gi')
                self.assertIn({'name': 'business', 'mountPath': '/data'}, web['volumeMounts'])

    def test_replacement_foreign_ownership_and_configuration_collision_fail_before_mutation(self):
        for mutation in ['uid', 'organization', 'environment', 'deployment', 'pod_environment', 'env', 'hostNetwork']:
            existing = copy.deepcopy(self.existing)
            if mutation == 'uid':
                existing['metadata']['uid'] = str(uuid.uuid4())
            elif mutation in {'organization', 'environment', 'deployment'}:
                existing['metadata']['labels'] = {'core.platform/' + mutation: 'other'}
            elif mutation == 'env':
                existing['spec']['template']['spec']['containers'][0]['env'].append({'name': 'CORE_ISSUER', 'value': 'https://other.example.test'})
            elif mutation == 'pod_environment':
                existing['spec']['template']['metadata']['labels']['core.platform/environment'] = 'production'
            else:
                existing['spec']['template']['spec']['hostNetwork'] = True
            with patch.object(self.adapter, 'run', return_value=existing) as run:
                self.assertEqual(self.adapter.reconcile(self.spec)['state'], 'failed', mutation)
                self.assertEqual(run.call_count, 1, mutation)

    def test_foreign_secret_denied_and_stopped_integration_does_not_scale_application(self):
        foreign = {'metadata': {'labels': {'core.platform/deployment': 'other'}}}
        with patch.object(self.adapter, 'run', side_effect=[self.existing, foreign]) as run:
            self.assertEqual(self.adapter.reconcile(self.spec)['state'], 'failed')
            self.assertEqual(run.call_count, 2)
        with patch.object(self.adapter, 'run', return_value=self.existing) as run:
            self.assertEqual(self.adapter.reconcile({**self.spec, 'desired_state': 'stopped'})['state'], 'stopped')
            self.assertEqual(run.call_count, 1)

    def test_network_bundle_has_explicit_boundaries_and_no_cross_namespace_allow(self):
        value = bundle('sandbox', str(uuid.uuid4()), 'sandbox', '192.0.2.10/32')
        namespace, deny, allow = value['items']
        self.assertEqual(namespace['metadata']['labels']['core.platform/environment'], 'sandbox')
        self.assertEqual(deny['spec']['podSelector'], {})
        self.assertEqual(deny['spec']['ingress'], [])
        self.assertEqual(deny['spec']['egress'], [])
        self.assertIn({'to': [{'ipBlock': {'cidr': '192.0.2.10/32'}}], 'ports': [{'protocol': 'TCP', 'port': 443}]}, allow['spec']['egress'])
        with self.assertRaises(ValueError):
            bundle('sandbox', str(uuid.uuid4()), 'sandbox', '0.0.0.0/0')
