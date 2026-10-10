from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from core_fabric.tests import test_fabric as fabric
from core_fabric.models import FabricPermit
from core_fabric.services import consume, revoke
from core_fabric.transport import FabricUnavailable
from identity.access import Principal
from identity.models import Membership, Role, ServicePermission, User
from platform_services.data import publish
from platform_services.environments import create_environment
from platform_services.models import Dataset, WorkloadCredential
from core_ai.models import QuerySource
from core_ai.services import catalog, query


@override_settings(ISSUER=fabric.RECIPIENT, CORE_FABRIC_ORIGINS=[fabric.SOURCE, fabric.RECIPIENT])
class FederatedQueryTests(TestCase):
    application = fabric.FabricTests.application
    approve_both = fabric.FabricTests.approve_both

    def route(self, peer, suffix, values, **kwargs):
        return fabric.FabricTests.route(self, peer, suffix, values)

    def setUp(self):
        fabric.FabricTests.setUp(self)
        self.environment = create_environment(self.recipient, name='sandbox', namespace='ai-federation', application_origin='https://app.example.test')
        WorkloadCredential.objects.filter(installation__organization=self.recipient).update(environment=self.environment)
        self.imported.permission_tags.add(self.environment.boundary_tag)
        self.user = User.objects.create_user('joint-query@example.test')
        self.member = Membership.objects.create(organization=self.recipient, user=self.user)
        self.member.access_tags.add(self.environment.boundary_tag)
        self.role = Role.objects.create(organization=self.recipient, slug='joint-readers', name='Joint readers')
        self.member.roles.add(self.role)
        self.role.permissions.add(*ServicePermission.objects.filter(service__organization=self.recipient,
            codename__in=['ai-query', 'fabric-read', 'data-read']))
        self.imported.allowed_memberships.add(self.member)
        self.source_scope = QuerySource.objects.create(organization=self.recipient, name='Joint reports', installation=self.consumer,
            environment=self.environment, contract=self.imported, fields=['title'], required_permissions=['fabric-read'])
        self.source_scope.allowed_roles.add(self.role)
        self.reader = Principal(user=self.user, purpose='joint-project')
        self.approve_both()
        publish(self.dataset, 'private', {'title': 'Private project', 'project': 'private', 'private': 'excluded'})
        publish(self.dataset, 'joint', {'title': 'Approved project', 'project': 'joint', 'private': 'excluded'})

    def run_query(self):
        return query(self.reader, self.recipient, {'sources': [str(self.source_scope.pk)]})

    def test_human_federated_query_uses_bilateral_live_permit_and_authority_citations(self):
        value = self.run_query()
        self.assertEqual(len(value['evidence']), 1)
        self.assertEqual(value['evidence'][0]['data'], {'title': 'Approved project'})
        self.assertEqual(value['evidence'][0]['authority_organization_id'], str(self.source.pk))
        self.assertEqual(value['evidence'][0]['agreement_id'], str(self.export.agreement_id))
        self.assertEqual(self.calls, 2)
        self.assertFalse(FabricPermit.objects.exists())
        self.assertFalse(self.recipient.datasets.exists())

    def test_member_read_grant_does_not_allow_writes(self):
        with self.assertRaises(PermissionDenied):
            consume(self.reader, self.imported, 'write', {'event_id': 'denied', 'data': {'title': 'Forbidden', 'project': 'joint'}})
        self.imported.allowed_memberships.clear()
        self.assertFalse(catalog(self.reader, self.recipient)['sources'])
        with self.assertRaises(PermissionDenied):
            self.run_query()

    def test_remote_approval_denial_returns_partial_without_remote_rows(self):
        with override_settings(ISSUER=fabric.SOURCE):
            revoke(self.export, Principal(user=self.source_admin))
        result = self.run_query()
        self.assertTrue(result['partial'])
        self.assertFalse(result['evidence'])
        self.assertEqual(result['sources'][0]['status'], 'access_changed')

    def test_remote_outage_keeps_local_results_and_exposes_explicit_partial_status(self):
        local = Dataset.objects.create(organization=self.recipient, slug='local', name='Local')
        local.permission_tags.add(self.environment.boundary_tag)
        source = QuerySource.objects.create(organization=self.recipient, name='Local reports', installation=self.consumer,
            environment=self.environment, dataset=local, fields=['title'], required_permissions=['fabric-read'])
        source.allowed_roles.add(self.role)
        publish(local, 'local', {'title': 'Local report'})
        with patch('core_fabric.services.request_peer', side_effect=FabricUnavailable):
            result = query(self.reader, self.recipient, {'sources': [str(source.pk), str(self.source_scope.pk)]})
        self.assertTrue(result['partial'])
        self.assertEqual([row['data'] for row in result['evidence']], [{'title': 'Local report'}])
        self.assertEqual(result['sources'][1]['status'], 'unavailable')

    def test_revoking_member_during_peer_call_discards_remote_results(self):
        self.side_effect = self.imported.allowed_memberships.clear
        value = self.run_query()
        self.assertFalse(value['evidence'])
        self.assertTrue(value['partial'])
        self.assertFalse(FabricPermit.objects.exists())
