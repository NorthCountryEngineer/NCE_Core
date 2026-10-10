from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import secrets
import tempfile
from unittest.mock import patch
import uuid

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core_client import CoreClient
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, Role, ServicePermission, User
from platform_services.agent_identity import issue_credential
from platform_services.catalog import seed_catalog
from platform_services.data import publish
from platform_services.models import AgentBinding, AgentDefinition, AgentTask, DataRecord, Dataset, MarketplacePackage, WorkloadCredential
from platform_services.services import install_core, install_package, issue_workload_credential
from core_fabric.models import FabricContract, FabricPeer, FabricPermit
from core_fabric.services import approve, consume, digest, fingerprint, revoke, terms, validate_terms
from core_fabric.transport import FabricUnavailable, credential_path, validate_origin


SOURCE = 'https://source.example.test'
RECIPIENT = 'https://recipient.example.test'
CAPS = ['fabric.peer', 'data.read', 'data.write']


class FabricConfigurationTests(SimpleTestCase):
    @override_settings(DEBUG=False, ISSUER=RECIPIENT, CORE_FABRIC_ORIGINS=[SOURCE])
    def test_exact_https_origin_without_credentials_paths_or_redirect_destinations(self):
        self.assertEqual(validate_origin(SOURCE), SOURCE)
        self.assertEqual(validate_origin(RECIPIENT), RECIPIENT)
        for issuer in ['http://source.example.test', SOURCE + '/', SOURCE + '/path', SOURCE + '?next=other',
                'https://unknown.example.test', 'https://name@source.example.test', 'https://source.example.test:bad']:
            with self.subTest(issuer=issuer), self.assertRaises(ValidationError):
                validate_origin(issuer)

    def test_credential_mount_rejects_cross_tenant_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            org = Organization(id=uuid.uuid4())
            folder = Path(directory)/str(org.pk)
            folder.mkdir()
            outside = Path(directory)/'outside'
            outside.write_text(secrets.token_urlsafe(32))
            link = folder/'link'
            link.symlink_to(outside)
            peer = FabricPeer(organization=org, credential_file=str(link))
            with override_settings(CORE_FABRIC_SECRET_ROOT=directory), self.assertRaises(ValidationError):
                credential_path(peer)

    def test_ai_tool_does_not_accept_model_proposed_routing_or_event_identity(self):
        client = CoreClient(SOURCE + '/api/platform/v1', secrets.token_urlsafe(32), str(uuid.uuid4()))
        for name, args in [('core_fabric_read', {'contract_id': str(uuid.uuid4())}),
                ('core_fabric_publish', {'data': {}, 'event_id': 'spoofed'}), ('core_fabric_approve', {})]:
            with self.subTest(name=name), patch.object(client, 'request') as request, self.assertRaises(ValueError):
                client.invoke_fabric_tool(name, args, contract_id=uuid.uuid4(), event_id='trusted-event')
            request.assert_not_called()


@override_settings(ISSUER=RECIPIENT, CORE_FABRIC_ORIGINS=[SOURCE, RECIPIENT])
class FabricTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(CORE_FABRIC_SECRET_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        seed_catalog(apps, None)
        self.source = Organization.objects.create(name='Source', slug='source')
        self.recipient = Organization.objects.create(name='Recipient', slug='recipient')
        for org in [self.source, self.recipient]:
            install_core(org)
        self.source_admin = User.objects.create_user('source-admin@example.test')
        self.recipient_admin = User.objects.create_user('recipient-admin@example.test')
        Membership.objects.create(organization=self.source, user=self.source_admin, kind='administrator')
        Membership.objects.create(organization=self.recipient, user=self.recipient_admin, kind='administrator')
        self.bridge_source, self.credential_source, self.raw_source = self.application(self.source, 'incoming-recipient', CAPS)
        self.bridge_recipient, self.credential_recipient, self.raw_recipient = self.application(self.recipient, 'incoming-source', ['fabric.peer'])
        self.consumer, self.credential, self.raw = self.application(self.recipient, 'consumer', ['fabric.read', 'fabric.write'])
        self.dataset = Dataset.objects.create(organization=self.source, slug='project', name='Project data', schema={'type': 'object'})
        files = []
        for org, raw in [(self.source, self.raw_recipient), (self.recipient, self.raw_source)]:
            folder = Path(self.directory.name)/str(org.pk)
            folder.mkdir()
            filename = folder/'peer.credential'
            filename.write_text(raw)
            filename.chmod(0o600)
            files.append(str(filename))
        self.peer_source = FabricPeer.objects.create(organization=self.source, name='Recipient peer', issuer=RECIPIENT,
            remote_organization_id=self.recipient.pk, inbound_installation=self.bridge_source, credential_file=files[0])
        self.peer_recipient = FabricPeer.objects.create(organization=self.recipient, name='Source peer', issuer=SOURCE,
            remote_organization_id=self.source.pk, inbound_installation=self.bridge_recipient, credential_file=files[1])
        self.export = FabricContract.objects.create(organization=self.source, peer=self.peer_source, direction='export', name='Joint project',
            dataset=self.dataset, data_class='operational', purpose_code='joint-project', fields=['title', 'project'],
            filters={'project': 'joint'}, allow_write=True)
        self.imported = FabricContract.objects.create(organization=self.recipient, peer=self.peer_recipient, direction='import', name='Shared project',
            agreement_id=self.export.agreement_id, remote_dataset_id=self.dataset.pk, data_class='operational',
            purpose_code='joint-project', fields=['title', 'project'], filters={'project': 'joint'}, allow_write=True)
        self.imported.allowed_installations.add(self.consumer)
        self.principal = Principal(credential=self.credential, purpose='joint-project')
        self.side_effect = None
        self.side_effect_suffix = 'exports/'
        self.calls = 0
        for name in ['core_fabric.services.request_peer', 'core_fabric.api.request_peer']:
            mocked = patch(name, side_effect=self.route)
            mocked.start()
            self.addCleanup(mocked.stop)

    def application(self, org, slug, capabilities):
        package = MarketplacePackage.objects.create(slug=slug, name=slug, kind='application', description='Fabric test application',
            manifest={'image': 'registry.example.test/fabric@sha256:' + 'a'*64, 'dependencies': ['core-fabric'], 'capabilities': capabilities})
        installation = install_package(org, package, capabilities)
        credential, raw = issue_workload_credential(installation, 'Test application')
        return installation, credential, raw

    def route(self, peer, suffix, values):
        self.calls += 1
        token = credential_path(peer).read_text()
        from django.conf import settings
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + token, 'HTTP_X_CORE_FABRIC_ISSUER': settings.ISSUER,
            'HTTP_X_CORE_PURPOSE': values.get('purpose_code', '')}
        with override_settings(ISSUER=peer.issuer):
            result = Client().post('/api/platform/v1/fabric/' + suffix, json.dumps(values), content_type='application/json', **headers)
        if result.status_code in {401, 403, 404}:
            raise PermissionDenied
        if result.status_code == 409:
            from platform_services.data import IdempotencyConflict
            raise IdempotencyConflict
        if result.status_code not in {200, 201}:
            raise FabricUnavailable
        if self.side_effect and suffix.startswith(self.side_effect_suffix):
            self.side_effect()
        return result.json()

    def approve_both(self):
        with override_settings(ISSUER=SOURCE):
            self.export = approve(self.export, Principal(user=self.source_admin), fingerprint(self.export))
        self.imported = approve(self.imported, Principal(user=self.recipient_admin), fingerprint(self.imported))

    def read(self, after=0, limit=100):
        return consume(self.principal, self.imported, 'read', {'after': after, 'limit': limit})

    def api(self, path, values=None, raw=None):
        from django.conf import settings
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (raw or self.raw), 'HTTP_X_CORE_PURPOSE': 'joint-project',
            'HTTP_X_CORE_FABRIC_ISSUER': RECIPIENT if settings.ISSUER == SOURCE else SOURCE}
        if values is None:
            return self.client.get('/api/platform/v1/fabric/' + path, **headers)
        return self.client.post('/api/platform/v1/fabric/' + path, json.dumps(values), content_type='application/json', **headers)

    def test_both_approvals_are_required_and_core_installation_does_not_grant_data_access(self):
        self.assertFalse(self.source.installations.get(package__slug='core-fabric').granted_capabilities)
        with self.assertRaises(PermissionDenied):
            self.read()
        self.imported = approve(self.imported, Principal(user=self.recipient_admin), fingerprint(self.imported))
        with self.assertRaises(PermissionDenied):
            self.read()
        self.approve_both()
        self.assertEqual(self.read()['records'], [])

    def test_selected_fields_rows_and_pagination_without_local_data_copies(self):
        self.approve_both()
        publish(self.dataset, 'excluded', {'project': 'private', 'title': 'Private report', 'private': 'secret-data'})
        first, _ = publish(self.dataset, 'first', {'project': 'joint', 'title': 'Joint report', 'private': 'secret-data'})
        last, _ = publish(self.dataset, 'last', {'project': 'joint', 'title': 'Next report'})
        page = self.read(limit=1)
        self.assertEqual(page['records'][0]['data'], {'project': 'joint', 'title': 'Joint report'})
        self.assertEqual(page['next_cursor'], first.pk)
        self.assertEqual(self.read(after=page['next_cursor'])['next_cursor'], last.pk)
        self.assertFalse(DataRecord.objects.filter(dataset__organization=self.recipient).exists())
        self.assertFalse(FabricPermit.objects.exists())

    def test_writes_are_scoped_idempotent_and_attributed_by_verified_recipient(self):
        self.approve_both()
        values = {'event_id': 'joint-1', 'data': {'project': 'joint', 'title': 'Shared work'}}
        first = consume(self.principal, self.imported, 'write', values)
        self.assertTrue(first['created'])
        self.assertFalse(consume(self.principal, self.imported, 'write', values)['created'])
        record = self.dataset.records.get()
        self.assertEqual(record.federation['actor'], {'kind': 'application', 'id': str(self.consumer.pk)})
        self.assertEqual(record.federation['issuer'], RECIPIENT)
        self.assertFalse(record.synthetic)
        from platform_services.data import IdempotencyConflict
        values['data']['title'] = 'Changed'
        with self.assertRaises(IdempotencyConflict):
            consume(self.principal, self.imported, 'write', values)
        for data in [{'project': 'private', 'title': 'Denied'}, {'project': 'joint', 'private': 'Denied'}]:
            with self.assertRaises(ValidationError):
                consume(self.principal, self.imported, 'write', {'event_id': 'invalid', 'data': data})

    def test_revocation_from_either_organization_stops_reads_and_writes(self):
        self.approve_both()
        with override_settings(ISSUER=SOURCE):
            revoke(self.export, Principal(user=self.source_admin))
        with self.assertRaises(PermissionDenied):
            self.read()
        self.approve_both()
        revoke(self.imported, Principal(user=self.recipient_admin))
        self.imported.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            consume(self.principal, self.imported, 'write', {'event_id': 'denied', 'data': {'project': 'joint'}})
        self.assertFalse(self.dataset.records.exists())

    def test_modified_terms_invalidate_approval_and_both_hashes_must_match(self):
        self.approve_both()
        self.imported.fields = ['project']
        self.imported.save()
        self.assertFalse(self.imported.approved_hash)
        self.imported = approve(self.imported, Principal(user=self.recipient_admin), fingerprint(self.imported))
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_source_and_recipient_admin_demotion_or_expiration_closes_federation(self):
        self.approve_both()
        Membership.objects.filter(user=self.source_admin).update(kind='member')
        with self.assertRaises(PermissionDenied):
            self.read()
        Membership.objects.filter(user=self.source_admin).update(kind='administrator')
        FabricContract.objects.filter(pk=self.imported.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.imported.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_peer_credentials_do_not_allow_direct_source_reads_without_recipient_permit(self):
        self.approve_both()
        with override_settings(ISSUER=SOURCE):
            result = self.api(f'exports/{self.export.agreement_id}/read/', {'permit_id': str(uuid.uuid4()),
                'terms_hash': fingerprint(self.export), 'purpose_code': 'joint-project', 'request': {'after': 0, 'limit': 100}}, raw=self.raw_source)
        self.assertEqual(result.status_code, 403)

    def test_export_does_not_trust_spoofed_peer_identity_or_other_organization(self):
        self.approve_both()
        with override_settings(ISSUER=SOURCE):
            for raw in [self.raw, self.raw_recipient]:
                result = self.api(f'exports/{self.export.agreement_id}/read/', {}, raw=raw)
                self.assertIn(result.status_code, [403, 404])
        unapproved, credential, _ = self.application(self.recipient, 'other-consumer', ['fabric.read'])
        with self.assertRaises(PermissionDenied):
            consume(Principal(credential=credential, purpose='joint-project'), self.imported, 'read', {'after': 0, 'limit': 100})

    def test_only_human_administrators_can_approve_and_csrf_is_enforced(self):
        with self.assertRaises(PermissionDenied):
            approve(self.imported, self.principal, fingerprint(self.imported))
        member = User.objects.create_user('fabric-member@example.test')
        Membership.objects.create(organization=self.recipient, user=member)
        with self.assertRaises(PermissionDenied):
            approve(self.imported, Principal(user=member), fingerprint(self.imported))
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.recipient_admin)
        self.assertEqual(browser.post(reverse('fabric_approve', args=[self.recipient.slug, self.imported.pk]),
            {'terms_hash': fingerprint(self.imported)}).status_code, 403)
        self.client.force_login(self.recipient_admin)
        self.assertEqual(self.client.get(reverse('fabric', args=[self.recipient.slug])).status_code, 200)
        self.assertEqual(self.client.post(reverse('fabric_approve', args=[self.recipient.slug, self.imported.pk]),
            {'terms_hash': 'outdated'}).status_code, 400)
        self.assertEqual(self.client.post(reverse('fabric_approve', args=[self.recipient.slug, self.imported.pk]),
            {'terms_hash': fingerprint(self.imported)}).status_code, 302)

    def test_permission_tags_added_after_approval_still_apply_at_source_and_recipient(self):
        self.approve_both()
        tag = PermissionTag.objects.create(organization=self.source, slug='restricted', name='Restricted source', allow_workloads=True)
        self.dataset.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.read()
        self.bridge_source.access_tags.add(tag)
        self.assertEqual(self.read()['records'], [])
        tag = PermissionTag.objects.create(organization=self.recipient, slug='restricted', name='Restricted recipient', allow_workloads=True)
        self.imported.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_recipient_revocation_during_io_prevents_returning_data(self):
        self.approve_both()
        publish(self.dataset, 'joint', {'project': 'joint', 'title': 'Must not return'})
        self.side_effect = lambda: WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=False)
        with self.assertRaises(PermissionDenied):
            self.read()
        self.assertFalse(FabricPermit.objects.exists())

    def test_agent_uses_own_approval_and_uat_contract_resource_scope(self):
        self.approve_both()
        agent = AgentDefinition.objects.create(organization=self.recipient, name='Project analyst', slug='analyst', kind='inspect')
        role = Role.objects.create(organization=self.recipient, name='Fabric reader', slug='fabric-reader')
        role.permissions.add(ServicePermission.objects.get(service__installation__organization=self.recipient,
            service__installation__package__slug='core-fabric', codename='fabric-read'))
        task = AgentTask.objects.create(organization=self.recipient, name='Joint project UAT', description='Approved analysis',
            installation=self.consumer, environment='uat', approved_by=self.recipient_admin,
            expires_at=timezone.now()+timedelta(minutes=10), resources={'fabric_contracts': []})
        binding = AgentBinding.objects.create(account=agent.account, installation=self.consumer, environment='uat', task=task)
        binding.roles.add(role)
        self.imported.allowed_bindings.add(binding)
        principal = Principal(agent_binding=binding, purpose='joint-project')
        with self.assertRaises(PermissionDenied):
            consume(principal, self.imported, 'read', {'after': 0, 'limit': 100})
        task.resources = {'fabric_contracts': [str(self.imported.pk)]}
        task.full_clean()
        task.save()
        own, _ = issue_credential(binding, Principal(user=self.recipient_admin, purpose='joint-project'))
        principal.agent_credential = own
        self.assertEqual(consume(principal, self.imported, 'read', {'after': 0, 'limit': 100})['records'], [])
        task.expires_at = timezone.now()-timedelta(seconds=1)
        task.save()
        with self.assertRaises(PermissionDenied):
            consume(principal, self.imported, 'read', {'after': 0, 'limit': 100})

    def test_synthetic_records_and_training_provenance_survive_federated_read(self):
        Dataset.objects.filter(pk=self.dataset.pk).update(data_class='synthetic')
        self.dataset.refresh_from_db()
        self.export.data_class = self.imported.data_class = 'synthetic'
        self.export.save()
        self.imported.save()
        self.approve_both()
        training_id = uuid.uuid4()
        publish(self.dataset, 'training', {'project': 'joint'}, training_run_id=training_id)
        row = self.read()['records'][0]
        self.assertTrue(row['synthetic'])
        self.assertEqual(row['training_run_id'], str(training_id))
        Dataset.objects.filter(pk=self.dataset.pk).update(data_class='operational')
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_network_failure_fails_closed_without_storing_permits_or_copying_data(self):
        self.approve_both()
        with patch('core_fabric.services.request_peer', side_effect=FabricUnavailable):
            result = self.api(f'contracts/{self.imported.pk}/records/')
        self.assertEqual(result.status_code, 503)
        self.assertFalse(FabricPermit.objects.exists())
        self.assertFalse(DataRecord.objects.filter(dataset__organization=self.recipient).exists())

    def test_scopes_and_routing_reject_cross_tenant_data_and_unbounded_policy(self):
        invalid = deepcopy(terms(self.imported))
        for change in [{'fields': []}, {'fields': ['field.path']}, {'filters': {'field': {'operator': 'execute'}}}, {'allow_read': False, 'allow_write': False}]:
            values = {**invalid, **change}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_terms(values)
        self.imported.dataset = self.dataset
        with self.assertRaises(ValidationError):
            self.imported.full_clean()

    def test_audits_and_permits_do_not_contain_shared_record_bodies(self):
        self.approve_both()
        body = {'title': 'Private shared report body', 'project': 'joint'}
        consume(self.principal, self.imported, 'write', {'event_id': 'audit', 'data': body})
        self.assertNotIn(body['title'], json.dumps(list(AuditEvent.objects.values('action', 'target', 'reason_code', 'tags'))))
        self.assertFalse(FabricPermit.objects.exists())

    def test_read_only_contract_cannot_publish_even_when_application_has_write_grant(self):
        self.export.allow_write = self.imported.allow_write = False
        self.export.save()
        self.imported.save()
        self.approve_both()
        with self.assertRaises(PermissionDenied):
            consume(self.principal, self.imported, 'write', {'event_id': 'denied', 'data': {'project': 'joint'}})

    def test_recipient_permit_is_bound_to_hash_operation_actor_and_expiry(self):
        self.approve_both()
        permit = FabricPermit.objects.create(contract=self.imported, credential=self.credential, operation='read',
            request_hash=digest({'after': 0, 'limit': 1}), terms_hash=fingerprint(self.imported))
        values = {'agreement_id': str(self.imported.agreement_id), 'terms_hash': fingerprint(self.imported),
            'operation': 'read', 'request_hash': permit.request_hash, 'purpose_code': 'joint-project'}
        verified = self.api(f'permits/{permit.pk}/verify/', values, raw=self.raw_recipient)
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()['actor']['id'], str(self.consumer.pk))
        for change in [{'operation': 'write'}, {'request_hash': digest({'after': 0, 'limit': 2})}, {'agreement_id': str(uuid.uuid4())}]:
            denied = self.api(f'permits/{permit.pk}/verify/', {**values, **change}, raw=self.raw_recipient)
            self.assertEqual(denied.status_code, 403)
        self.consumer.granted_capabilities = []
        self.consumer.save()
        self.assertEqual(self.api(f'permits/{permit.pk}/verify/', values, raw=self.raw_recipient).status_code, 403)
        self.consumer.granted_capabilities = ['fabric.read', 'fabric.write']
        self.consumer.save()
        permit.expires_at = timezone.now()-timedelta(seconds=1)
        permit.save()
        self.assertEqual(self.api(f'permits/{permit.pk}/verify/', values, raw=self.raw_recipient).status_code, 403)

    def test_recipient_demotion_and_disabled_source_service_close_access(self):
        self.approve_both()
        Membership.objects.filter(user=self.recipient_admin).update(kind='member')
        with self.assertRaises(PermissionDenied):
            self.read()
        Membership.objects.filter(user=self.recipient_admin).update(kind='administrator')
        self.source.installations.filter(package__slug='core-data').update(enabled=False)
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_source_revocation_during_recipient_callback_prevents_side_effects(self):
        self.approve_both()
        self.side_effect_suffix = 'permits/'
        def remove_data_authority():
            self.bridge_source.granted_capabilities = ['fabric.peer', 'data.read']
            self.bridge_source.save()
        self.side_effect = remove_data_authority
        with self.assertRaises(PermissionDenied):
            consume(self.principal, self.imported, 'write', {'event_id': 'denied', 'data': {'project': 'joint'}})
        self.assertFalse(self.dataset.records.exists())
        self.assertFalse(FabricPermit.objects.exists())
        self.side_effect = lambda: FabricContract.objects.filter(pk=self.export.pk).update(approved_hash='')
        with self.assertRaises(PermissionDenied):
            self.read()

    def test_import_form_validates_reviewed_identity_and_preserves_matching_fingerprint(self):
        self.client.force_login(self.recipient_admin)
        with override_settings(ISSUER=SOURCE):
            offered = terms(self.export)
            expected = fingerprint(self.export)
        self.imported.delete()
        result = self.client.post(reverse('fabric_import', args=[self.recipient.slug]), {'name': 'Imported proposal',
            'peer': str(self.peer_recipient.pk), 'terms': canonical_json(offered), 'allowed_installations': [str(self.consumer.pk)]})
        self.assertEqual(result.status_code, 302, result.content)
        imported = self.recipient.fabric_contracts.get()
        self.assertEqual(fingerprint(imported), expected)
        self.assertFalse(imported.approved_hash)
        self.assertEqual(imported.allowed_installations.get(), self.consumer)


def canonical_json(value):
    return json.dumps(value, sort_keys=True)
