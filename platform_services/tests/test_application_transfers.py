"""Disposable transfer protocol/state tests; all external transports are forbidden."""
import base64
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model

from identity.access import Principal
from identity.models import Application, AuthenticationPolicy, Membership, Organization, Service, User
from platform_services import application_transfers as service
from platform_services.environments import create_environment
from platform_services.external_apps import register_external_deployment
from platform_services.migration_protocol import DOMAIN, PROTOCOL, canonical_json, digest, verify_document
from platform_services.models import (ApplicationPlacementReference, ApplicationTransfer, Deployment,
    CloudApplication, CloudPlatform, CloudRelease, Installation, MarketplacePackage, RuntimeNode, TransferReceipt, WorkloadCredential)
from platform_services.services import credential_allows, install_package, issue_workload_credential, upgrade_installation
from .fixtures.migration_signature import FIXTURE


def public(key):
    return base64.urlsafe_b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode().rstrip('=')


def signed(key, purpose, payload):
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    document = {'protocol': PROTOCOL, 'purpose': purpose, 'signer_key_id': 'sha256:' + hashlib.sha256(raw).hexdigest(),
        'payload': payload}
    document['signature'] = base64.urlsafe_b64encode(key.sign(DOMAIN + canonical_json(document))).decode().rstrip('=')
    return document


class ApplicationTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(slug='transfer-owner', name='Source owner')
        cls.user = User.objects.create_user('transfer-owner@example.test')
        Membership.objects.create(organization=cls.organization, user=cls.user, kind='administrator')
        cls.member = User.objects.create_user('ordinary@example.test')
        Membership.objects.create(organization=cls.organization, user=cls.member)
        cls.package = MarketplacePackage.objects.create(slug='portable-application', name='Any application',
            description='Metadata-only external app', kind='application', manifest={'external_runtime': 'aws-amplify',
                'capabilities': [], 'callback_path': '/oauth/callback'})
        cls.installation = install_package(cls.organization, cls.package)
        cls.environment = create_environment(cls.organization, name='production', namespace='transfer-production',
            application_origin='https://source.example.test')
        cls.deployment = register_external_deployment(cls.installation, cls.environment, 'https://source.example.test')

    def setUp(self):
        self.source_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.receiver_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        self.principal = Principal(user=self.user)
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def offer(self):
        return service.create_offer(self.installation, self.principal)

    def claim_document(self, transfer):
        now = int(timezone.now().timestamp())
        return signed(self.receiver_key, 'receiver-descriptor', {'offer_id': str(transfer.pk),
            'source_ids': transfer.source_identity,
            'destination': {'origin': 'https://customer.example.test', 'mode': 'direct-sso',
                'public_key': public(self.receiver_key), 'adapter_id': 'any-app-v1'},
            'challenge': 'receiver-challenge', 'capabilities': service.CAPABILITIES,
            'issued_at': now, 'expires_at': now + 300})

    def pair(self):
        transfer, secret = self.offer()
        return service.claim_offer(transfer.pk, secret, self.claim_document(transfer))

    def plan_document(self):
        return {'source_adapter': {'adapter_id': 'any-app-v1', 'public_key': public(self.source_key), 'authority_hash': 'a' * 64},
            'capability_contract': {'adapter_id': 'any-app-v1', 'version': 1, 'capabilities': service.CAPABILITIES,
                'inventory_hash': 'b' * 64, 'unknown_extensions': 0},
            'snapshot_manifest_hash': 'c' * 64, 'identity_mapping_hash': 'd' * 64, 'policy_mapping_hash': 'e' * 64,
            'release_manifest_hash': 'f' * 64, 'retention_days': 30, 'guest_links': 'rotate',
            'sessions': 'invalidate', 'workers': 'paused',
            'snapshot_reference': {'reference_id': 'opaque-snapshot-1', 'ciphertext_hash': '1' * 64, 'key_custody': 'destination'},
            'data_counts': {'records': 12, 'blobs': 4}}

    def approve(self):
        transfer = service.prepare_plan(self.pair(), self.principal, self.plan_document())
        return service.approve_plan(transfer, self.principal, transfer.plan_hash)

    def receipt_document(self, transfer, stage):
        plan = transfer.plan
        evidence = {
            'snapshot': {'snapshot_manifest_hash': plan['snapshot_manifest_hash'], 'source_authority_hash': 'a' * 64,
                'local_admin_authorized': True, 'tenant_closure_verified': True, 'source_writes_fenced': True,
                'workers_paused': True, 'ambiguous_sends_blocked': True},
            'staged': {**{key: plan[key] for key in ['snapshot_manifest_hash', 'identity_mapping_hash', 'policy_mapping_hash',
                'release_manifest_hash']}, **{key: True for key in ['inert_import', 'release_mirrored', 'offline_release_verified',
                'independent_identity_ready', 'independent_admin_ready', 'credentials_rewrapped', 'no_source_keys',
                'workers_paused', 'guest_links_reviewed']}},
            'fenced': {'snapshot_manifest_hash': plan['snapshot_manifest_hash'], **{key: True for key in [
                'source_writes_fenced', 'workers_paused', 'ambiguous_sends_blocked', 'sessions_invalidated']}},
            'verified': {**{key: plan[key] for key in ['snapshot_manifest_hash', 'identity_mapping_hash', 'policy_mapping_hash',
                'release_manifest_hash']}, **{key: True for key in ['data_counts_match', 'blobs_verified', 'identity_mapping_verified',
                'policy_semantics_preserved', 'local_admin_login_verified', 'source_dependency_absent', 'restart_without_nce_verified']}},
            'activated': {'snapshot_manifest_hash': plan['snapshot_manifest_hash'], **{key: True for key in [
                'destination_writes_enabled', 'source_fence_confirmed', 'ambiguous_sends_blocked', 'independent_operation_verified']}},
        }[stage]
        payload = {'transfer_id': str(transfer.pk), 'plan_hash': transfer.plan_hash, 'sequence': transfer.accepted_sequence + 1,
            'previous_receipt_hash': transfer.accepted_receipt_hash, 'stage': stage,
            'fence_epoch': 7, 'evidence': evidence}
        return signed(self.source_key if stage in ['snapshot', 'fenced'] else self.receiver_key, 'migration-receipt', payload)

    def advance(self, transfer, stage):
        service.accept_receipt(transfer.pk, self.receipt_document(transfer, stage))
        receipt = transfer.receipts.get(sequence=transfer.accepted_sequence + 1)
        return service.advance_transfer(transfer, self.principal, receipt.digest)

    def activated(self):
        transfer = self.approve()
        for stage in service.STAGES:
            transfer = self.advance(transfer, stage)
        return transfer

    def test_pairing_hash_only_expiring_single_use(self):
        transfer, raw = self.offer()
        self.assertNotIn(raw, json.dumps(transfer.source_identity))
        self.assertEqual(transfer.pairing_secret_hash, hashlib.sha256(raw.encode()).hexdigest())
        service.claim_offer(transfer.pk, raw, self.claim_document(transfer))
        transfer.refresh_from_db()
        self.assertEqual(transfer.pairing_secret_hash, '')
        with self.assertRaises(ValidationError):
            service.claim_offer(transfer.pk, raw, self.claim_document(transfer))

    def test_pairing_expiry_and_wrong_secret_fail_closed(self):
        transfer, raw = self.offer()
        with self.assertRaises(ValidationError):
            service.claim_offer(transfer.pk, 'incorrect', self.claim_document(transfer))
        transfer.expires_at = timezone.now() - timedelta(seconds=1)
        transfer.save()
        with self.assertRaises(ValidationError):
            service.claim_offer(transfer.pk, raw, self.claim_document(transfer))

    def test_wrong_source_claim_signature_and_origin_are_rejected(self):
        transfer, raw = self.offer()
        for field, value in [('offer_id', 'wrong'), ('source_ids', {}), ('capabilities', [])]:
            document = self.claim_document(transfer)
            document['payload'][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                service.claim_offer(transfer.pk, raw, signed(self.receiver_key, 'receiver-descriptor', document['payload']))
        document = self.claim_document(transfer)
        document['signature'] = 'A' * 86
        with self.assertRaises(ValidationError):
            service.claim_offer(transfer.pk, raw, document)
        document = self.claim_document(transfer)
        document['payload']['destination']['origin'] = 'https://customer.example.test/private'
        with self.assertRaises(ValidationError):
            service.claim_offer(transfer.pk, raw, signed(self.receiver_key, 'receiver-descriptor', document['payload']))

    def test_only_human_source_admin_can_offer_approve_or_accept(self):
        with self.assertRaises(PermissionDenied):
            service.create_offer(self.installation, Principal(user=self.member))
        credential, _ = issue_workload_credential(self.installation, 'source integration')
        with self.assertRaises(PermissionDenied):
            service.create_offer(self.installation, Principal(user=self.user, credential=credential))

    def test_plan_rejects_customer_bytes_urls_keys_extensions_or_wrong_semantics(self):
        transfer = self.pair()
        for mutate in [lambda p: p.update(customer_records=[{'private': 'content'}]),
                lambda p: p['snapshot_reference'].update(url='https://remote.example.test/snapshot'),
                lambda p: p['snapshot_reference'].update(key_custody='nce'),
                lambda p: p['capability_contract'].update(unknown_extensions=1),
                lambda p: p.update(workers='running'), lambda p: p.update(sessions='preserve')]:
            document = self.plan_document()
            mutate(document)
            with self.assertRaises(ValidationError):
                service.prepare_plan(transfer, self.principal, document)

    def test_exact_plan_review_blocks_stale_source_or_hash(self):
        transfer = service.prepare_plan(self.pair(), self.principal, self.plan_document())
        with self.assertRaises(ValidationError):
            service.approve_plan(transfer, self.principal, '0' * 64)
        Deployment.objects.filter(pk=self.deployment.pk).update(revision=9)
        with self.assertRaises(ValidationError):
            service.approve_plan(transfer, self.principal, transfer.plan_hash)

    def test_approved_plan_is_immutable(self):
        transfer = self.approve()
        with self.assertRaises(ValidationError):
            service.prepare_plan(transfer, self.principal, self.plan_document())

    def test_receipt_requires_local_source_admin_signer_not_core_or_receiver(self):
        transfer = self.approve()
        document = self.receipt_document(transfer, 'snapshot')
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.receiver_key, 'migration-receipt', document['payload']))
        document['payload']['evidence']['local_admin_authorized'] = False
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.source_key, 'migration-receipt', document['payload']))

    def test_receipt_order_plan_and_evidence_are_bound(self):
        transfer = self.approve()
        for field, value in [('plan_hash', '0' * 64), ('sequence', 2), ('previous_receipt_hash', 'a' * 64),
                ('stage', 'activated'), ('fence_epoch', 0)]:
            document = self.receipt_document(transfer, 'snapshot')
            document['payload'][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                service.accept_receipt(transfer.pk, signed(self.source_key, 'migration-receipt', document['payload']))

    def test_receipt_is_idempotent_but_different_pending_receipt_never_overwrites(self):
        transfer = self.approve()
        document = self.receipt_document(transfer, 'snapshot')
        service.accept_receipt(transfer.pk, document)
        service.accept_receipt(transfer.pk, document)
        self.assertEqual(transfer.receipts.count(), 1)
        self.assertEqual(transfer.accepted_sequence, 0)
        with self.assertRaises(ValidationError):
            service.advance_transfer(transfer, self.principal, 'a' * 64)
        transfer = service.advance_transfer(transfer, self.principal, transfer.receipts.get().digest)
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, document)

    def test_policy_weakening_and_missing_independent_admin_block(self):
        transfer = self.advance(self.approve(), 'snapshot')
        document = self.receipt_document(transfer, 'staged')
        document['payload']['evidence']['independent_admin_ready'] = False
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.receiver_key, 'migration-receipt', document['payload']))
        transfer = self.advance(transfer, 'staged')
        transfer = self.advance(transfer, 'fenced')
        document = self.receipt_document(transfer, 'verified')
        document['payload']['evidence']['policy_semantics_preserved'] = False
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.receiver_key, 'migration-receipt', document['payload']))

    def test_source_fence_epoch_is_preserved_and_no_automatic_rollback(self):
        transfer = self.approve()
        for stage in ['snapshot', 'staged', 'fenced']:
            transfer = self.advance(transfer, stage)
        self.assertEqual(transfer.fence_epoch, 7)
        with self.assertRaises(ValidationError):
            service.cancel_transfer(transfer, self.principal)
        document = self.receipt_document(transfer, 'verified')
        document['payload']['fence_epoch'] = 8
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.receiver_key, 'migration-receipt', document['payload']))

    def test_handover_requires_every_stage_and_exact_final_review(self):
        transfer = self.approve()
        with self.assertRaises(ValidationError):
            service.handover(transfer, self.principal, transfer.plan_hash, '')

    def test_handover_preserves_ids_revokes_authority_and_cannot_be_reenabled(self):
        original = service.source_identity(self.installation)
        encrypted_history = self.deployment.encrypted_bundle
        credential, _ = issue_workload_credential(self.installation, 'old integration')
        transfer = self.activated()
        transfer = service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        self.installation.refresh_from_db()
        self.deployment.refresh_from_db()
        credential.refresh_from_db()
        self.assertEqual(service.source_identity(self.installation), original)
        self.assertEqual(transfer.state, 'handed_over')
        self.assertFalse(self.installation.enabled)
        self.assertFalse(credential.active)
        self.assertFalse(Application.objects.filter(service=self.installation.service, active=True).exists())
        self.assertFalse(credential_allows(credential, []))
        self.assertEqual(self.deployment.encrypted_bundle, encrypted_history)
        self.assertIsNone(self.deployment.node_id)
        self.assertEqual(ApplicationPlacementReference.objects.get().source_identity, original)
        with self.assertRaises(ValidationError):
            issue_workload_credential(self.installation, 'forbidden')
        with self.assertRaises(ValidationError):
            upgrade_installation(self.installation, [], self.user)
        with self.assertRaises(ValidationError):
            register_external_deployment(self.installation, self.environment, 'https://source.example.test')
        client = Application.objects.filter(service=self.installation.service).first()
        client.active = True
        client.service.active = True
        self.assertFalse(client.is_usable(None))

    def test_metadata_intake_api_never_fetches_destination_and_cannot_advance(self):
        transfer, secret = self.offer()
        url = reverse('application_transfer_claim', args=[transfer.pk])
        result = self.client.post(url, json.dumps({'secret': secret, 'envelope': self.claim_document(transfer)}),
            content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'paired')
        self.assertNotIn(secret, result.content.decode())
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_duplicate_json_fields_and_raw_customer_bytes_rejected_without_echo(self):
        transfer, _ = self.offer()
        result = self.client.post(reverse('application_transfer_claim', args=[transfer.pk]),
            '{"secret":"private-data","secret":"other","envelope":{}}', content_type='application/json')  # pragma: allowlist secret -- synthetic duplicate-key input, never a credential
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('private-data', result.content.decode())

    def test_ui_requires_source_admin_and_shows_exact_plan(self):
        transfer = self.approve()
        self.client.force_login(self.user)
        url = reverse('application_transfer', args=[self.organization.slug, self.installation.pk])
        result = self.client.get(url)
        self.assertContains(result, transfer.plan_hash)
        self.assertContains(result, 'source administrator acceptance')
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_protocol_rejects_floats_wrong_purpose_and_key_substitution(self):
        with self.assertRaises(ValueError):
            canonical_json({'float': 1.0})
        document = signed(self.source_key, 'migration-receipt', {'count': 1, 'note': 'interop'})
        self.assertEqual(verify_document(document, public(self.source_key), 'migration-receipt'), document['payload'])
        with self.assertRaises(ValueError):
            verify_document(document, public(self.receiver_key), 'migration-receipt')
        with self.assertRaises(ValueError):
            verify_document(document, public(self.source_key), 'export-grant')

    def test_actual_proposal_signed_fixture_verifies_with_core(self):
        fixture = FIXTURE
        self.assertEqual(verify_document(fixture['envelope'], fixture['public_key'], 'migration-receipt'),
            {'note': 'interop', 'count': 1})

    def test_expired_claimed_offer_does_not_allow_parallel_transfer(self):
        transfer = self.pair()
        transfer.expires_at = timezone.now() - timedelta(days=1)
        transfer.save()
        with self.assertRaises(ValidationError):
            self.offer()
        service.cancel_transfer(transfer, self.principal)
        self.offer()

    def test_identity_addition_after_pairing_requires_fresh_pairing(self):
        transfer = self.pair()
        Deployment.objects.create(installation=self.installation, name='Unreviewed additional instance', target='embedded')
        with self.assertRaises(ValidationError):
            service.prepare_plan(transfer, self.principal, self.plan_document())

    def test_source_authentication_policy_drift_invalidates_review_but_health_does_not(self):
        transfer = service.prepare_plan(self.pair(), self.principal, self.plan_document())
        Deployment.objects.filter(pk=self.deployment.pk).update(healthy=True, last_seen=timezone.now(), observed_state='running')
        service.approve_plan(transfer, self.principal, transfer.plan_hash)
        for stage in service.STAGES:
            transfer.refresh_from_db()
            transfer = self.advance(transfer, stage)
        AuthenticationPolicy.objects.create(organization=self.organization, require_mfa=True)
        self.assertNotEqual(service._fingerprint(self.installation), transfer.plan['source_fingerprint'])
        with self.assertRaises((ValidationError, PermissionDenied)):
            service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)

    def test_approved_transfer_blocks_new_source_credentials_and_deployment_changes(self):
        self.approve()
        with self.assertRaises(ValidationError):
            issue_workload_credential(self.installation, 'new source key')
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(reverse('installation_settings', args=[self.organization.slug, self.installation.pk])),
            reverse('application_transfer', args=[self.organization.slug, self.installation.pk]))
        self.assertEqual(self.client.post(reverse('deployment_action', args=[self.organization.slug, self.deployment.pk]),
            {'action': 'start'}).status_code, 403)

    def test_snapshot_requires_fence_before_export_and_blocks_automatic_cancellation(self):
        transfer = self.approve()
        document = self.receipt_document(transfer, 'snapshot')
        document['payload']['evidence']['source_writes_fenced'] = False
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, signed(self.source_key, 'migration-receipt', document['payload']))
        transfer = self.advance(transfer, 'snapshot')
        self.assertGreater(transfer.fence_epoch, 0)
        with self.assertRaises(ValidationError):
            service.cancel_transfer(transfer, self.principal)

    def test_oauth_revocation_is_scoped_and_retains_unrelated_application_tokens(self):
        source_client = Application.objects.get(pk=self.deployment.application_id)
        other_service = Service.objects.create(organization=self.organization, slug='unrelated', name='Unrelated')
        other_client = Application.objects.create(service=other_service, name='Unrelated client', client_type='public',
            authorization_grant_type='authorization-code', redirect_uris='https://unrelated.example.test/callback')
        tokens = get_access_token_model()
        source_token = tokens.objects.create(application=source_client, user=self.user, token='source-access',
            expires=timezone.now() + timedelta(hours=1), scope='openid')
        other_token = tokens.objects.create(application=other_client, user=self.user, token='unrelated-access',
            expires=timezone.now() + timedelta(hours=1), scope='openid')
        refresh = get_refresh_token_model().objects.create(application=source_client, user=self.user,
            token='source-refresh', access_token=source_token)
        transfer = self.activated()
        service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        refresh.refresh_from_db()
        self.assertIsNotNone(refresh.revoked)
        self.assertIsNone(refresh.access_token_id)
        self.assertFalse(tokens.objects.filter(pk=source_token.pk).exists())
        self.assertTrue(tokens.objects.filter(pk=other_token.pk).exists())

    def test_reference_guard_survives_active_flag_toggling_and_held_objects(self):
        credential, raw = issue_workload_credential(self.installation, 'original integration')
        transfer = self.activated()
        service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        Installation.objects.filter(pk=self.installation.pk).update(enabled=True)
        Service.objects.filter(pk=self.installation.service_id).update(active=True)
        WorkloadCredential.objects.filter(pk=credential.pk).update(active=True)
        Application.objects.filter(service_id=self.installation.service_id).update(active=True)
        self.assertFalse(credential_allows(credential, []))
        self.assertEqual(self.client.get('/api/platform/v1/integration/', HTTP_AUTHORIZATION='Bearer ' + raw).status_code, 401)
        from platform_services.external_apps import external_available, external_resource_credentials
        self.assertFalse(external_available(self.deployment))
        with self.assertRaises(ValidationError):
            external_resource_credentials(self.deployment)
        from platform_services.kubernetes import deployment_spec
        with self.assertRaises(ValidationError):
            deployment_spec(self.deployment)
        from core_fabric.models import FabricPeer
        from core_fabric.transport import request_peer
        peer = FabricPeer(organization=self.organization, inbound_installation=self.installation,
            issuer='https://independent.example.test', name='Held peer')
        with self.assertRaises(PermissionDenied):
            request_peer(peer, 'records/', {})
        from core_ai.models import QuerySource
        from core_ai.services import authorize
        source = QuerySource(organization=self.organization, installation=self.installation, environment=self.environment)
        with self.assertRaises(PermissionDenied):
            authorize(self.principal, source)

    def test_runtime_cannot_receive_or_report_independent_deployment_even_if_reassigned(self):
        raw = 'cr_test-independent-runtime'
        node = RuntimeNode.objects.create(organization=self.organization, namespace=self.environment.namespace,
            environment=self.environment, name='Shared runtime', credential_hash=WorkloadCredential.digest(raw))
        node.access_tags.add(self.environment.boundary_tag)
        transfer = self.activated()
        service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        Deployment.objects.filter(pk=self.deployment.pk).update(node=node, namespace=node.namespace)
        result = self.client.post('/api/platform/v1/runtime/sync/', json.dumps({'inventory': [],
            'isolation': {'organization_id': str(self.organization.pk), 'environment': 'production', 'default_deny': True},
            'reports': [{'deployment_id': str(self.deployment.pk), 'revision': self.deployment.revision,
                'healthy': True, 'state': 'running'}]}), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + raw)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['deployments'], [])
        self.deployment.refresh_from_db()
        self.assertFalse(self.deployment.healthy)

    def test_valid_https_internal_port_and_ipv6_claims_do_not_trigger_requests(self):
        transfer, secret = self.offer()
        document = self.claim_document(transfer)
        document['payload']['destination']['origin'] = 'https://[2001:db8::1]:8443'
        service.claim_offer(transfer.pk, secret, signed(self.receiver_key, 'receiver-descriptor', document['payload']))

    def test_source_link_landing_never_fetches_data_or_exposes_fragment(self):
        result = self.client.get('/application-transfer/receive/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(result['Referrer-Policy'], 'no-referrer')
        self.assertNotIn('secret=', result.content.decode())
        result = self.client.get('/application-transfer/clear-fragment.js')
        self.assertContains(result, 'history.replaceState')

    def test_source_authority_changes_block_receipt_intake_and_manual_stage_acceptance(self):
        transfer = self.approve()
        document = self.receipt_document(transfer, 'snapshot')
        service.accept_receipt(transfer.pk, document)
        receipt = transfer.receipts.get(sequence=1)
        Membership.objects.filter(organization=self.organization, user=self.member).update(active=False)
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, document)
        with self.assertRaises(ValidationError):
            service.advance_transfer(transfer, self.principal, receipt.digest)
        transfer.refresh_from_db()
        self.assertEqual(transfer.accepted_sequence, 0)
        self.assertIsNone(transfer.receipts.get().accepted_at)

    def test_inflight_release_and_later_cloud_controls_cannot_cross_transfer_boundary(self):
        platform = CloudPlatform.objects.create(organization=self.organization, name='Source platform',
            allowed_repositories=['Example/application'], config={'environment_ids': [str(self.environment.pk)]})
        app = CloudApplication.objects.create(organization=self.organization, platform=platform, environment=self.environment,
            name='Source application', slug='source-application', repository='Example/application', commit='a' * 40,
            archetype='amplify-fargate', public_origin='https://source.example.test', installation=self.installation)
        transfer = self.approve()
        release = CloudRelease.objects.create(application=app, requested_by=self.user, status='queued', digest='b' * 64)
        with self.assertRaises(ValidationError):
            service.accept_receipt(transfer.pk, self.receipt_document(transfer, 'snapshot'))
        release.status = 'cancelled'
        release.save()
        for stage in service.STAGES:
            transfer = self.advance(transfer, stage)
        service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        from platform_services.cloud_deployments import authorized
        with self.assertRaises(PermissionDenied):
            authorized(self.principal, app, 'cloud.release_prepare')
        app.refresh_from_db()
        self.assertFalse(app.active)

    def test_copy_back_receiver_claim_object_works_in_source_ui_without_remote_requests(self):
        transfer, secret = self.offer()
        self.client.force_login(self.user)
        result = self.client.post(reverse('application_transfer', args=[self.organization.slug, self.installation.pk]),
            {'action': 'claim', 'transfer_id': str(transfer.pk), 'document': json.dumps({
                'secret': secret, 'envelope': self.claim_document(transfer)})})
        self.assertEqual(result.status_code, 302)
        transfer.refresh_from_db()
        self.assertEqual(transfer.state, 'paired')
        self.assertEqual(transfer.pairing_secret_hash, '')

    def test_handover_retains_admin_history_and_catalog_location_without_new_member_authority(self):
        platform = CloudPlatform.objects.create(organization=self.organization, name='Source platform',
            allowed_repositories=['Example/application'], config={'environment_ids': [str(self.environment.pk)]})
        app = CloudApplication.objects.create(organization=self.organization, platform=platform, environment=self.environment,
            name='Source application', slug='source-application', repository='Example/application', commit='a' * 40,
            archetype='amplify-fargate', public_origin='https://source.example.test', installation=self.installation)
        transfer = self.activated()
        service.handover(transfer, self.principal, transfer.plan_hash, transfer.accepted_receipt_hash)
        url = reverse('application_transfer', args=[self.organization.slug, self.installation.pk])
        self.client.force_login(self.user)
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, 'reference only')
        self.assertContains(result, 'https://customer.example.test')
        self.assertContains(result, transfer.plan_hash)
        with patch('platform_services.cloud_lightsail.current_hosting', side_effect=AssertionError('No source runtime lookup')):
            result = self.client.get(reverse('platform_dashboard', args=[self.organization.slug]))
            detail = self.client.get(reverse('cloud_application', args=[self.organization.slug, app.pk]))
        with patch('platform_services.cloud_lightsail.hosting_fingerprint', side_effect=AssertionError('No source binding lookup')):
            listing = self.client.get(reverse('cloud_applications', args=[self.organization.slug]))
        self.assertContains(detail, 'https://customer.example.test')
        self.assertNotContains(detail, 'Current hosting:')
        self.assertContains(listing, 'Independent · reference only')
        self.assertContains(result, 'https://customer.example.test')
        self.assertContains(result, 'Reference only')
        self.assertNotContains(result, reverse('issue_workload_key', args=[self.organization.slug, self.installation.pk]))
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse('platform_dashboard', args=[self.organization.slug])).status_code, 403)
