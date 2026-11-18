import hashlib
import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
from datetime import timedelta
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model

from identity.access import Principal
from identity.models import Application, AuthenticationEvidence, Membership, OAuthEvidence, Organization, PermissionTag, Role
from platform_services import edge
from platform_services.edge_models import EdgeCredential, EdgeDevice, EdgeEnrollment, EdgeJob, EdgeRelease


def archive(version='1.0.0', extra=None, manifest=None):
    data = io.BytesIO()
    manifest = manifest or {'schema_version': 1, 'application': 'nce-robotics', 'version': version, 'entrypoint': 'app/main.py'}
    with tarfile.open(fileobj=data, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
        for name, content in [('nce-release.json', json.dumps(manifest).encode()), ('app/main.py', b'print("ready")\n')]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        if extra:
            tar.addfile(extra, io.BytesIO(b'x' * extra.size) if extra.isfile() else None)
    return data.getvalue()


@override_settings(SECURE_SSL_REDIRECT=False, ISSUER='https://core.example.test', CORE_IOS_TEAM_ID='', CORE_IOS_BUNDLE_ID='ai.nnytech.NCERobotics')
class EdgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name='Robotics', slug='robotics')
        cls.other_org = Organization.objects.create(name='Other', slug='other')
        cls.admin = __import__('identity.models', fromlist=['User']).User.objects.create_user('edge-admin@example.test')
        cls.reader = __import__('identity.models', fromlist=['User']).User.objects.create_user('edge-reader@example.test')
        Membership.objects.create(organization=cls.organization, user=cls.admin, kind='administrator')
        cls.reader_member = Membership.objects.create(organization=cls.organization, user=cls.reader)
        Membership.objects.create(organization=cls.other_org, user=cls.admin, kind='administrator')
        cls.installation, cls.app = edge.register_application(cls.organization, cls.admin)
        cls.other_installation, cls.other_app = edge.register_application(cls.other_org, cls.admin)
        role = Role.objects.create(organization=cls.organization, slug='robot-reader', name='Robot reader')
        role.permissions.add(cls.installation.service.permissions.get(codename='device-read'))
        cls.reader_member.roles.add(role)
        cls.admin_evidence = AuthenticationEvidence.objects.create(user=cls.admin)
        cls.reader_evidence = AuthenticationEvidence.objects.create(user=cls.reader)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(CORE_EDGE_ARTIFACT_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.principal = Principal(user=self.admin, evidence=self.admin_evidence, application=self.app)
        self.reader_principal = Principal(user=self.reader, evidence=self.reader_evidence, application=self.app)
        self.user_token = self.oauth(self.admin, self.app, self.admin_evidence)
        self.reader_token = self.oauth(self.reader, self.app, self.reader_evidence)
        self.device, _, self.code = edge.create_enrollment(self.installation, self.principal, 'JudeyBot')
        self.device, self.credential, self.agent_token = edge.enroll(self.code, 'JudeyBot', '0.1.0', {'hostname': 'JudeyBot'})

    def oauth(self, user, app, evidence):
        raw = 'oauth-' + str(uuid4())
        get_access_token_model().objects.create(user=user, application=app, token=raw,
            scope='openid authorization', expires=timezone.now()+timedelta(minutes=5))
        OAuthEvidence.objects.create(token_hash=hashlib.sha256(raw.encode()).hexdigest(), kind='access', evidence=evidence)
        return raw

    def api(self, path, data=None, token=None, method='POST', secure=True):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (token or self.user_token)}
        if method == 'GET':
            return self.client.get('/api/edge/v1' + path, secure=secure, **headers)
        return self.client.post('/api/edge/v1' + path, json.dumps(data or {}), content_type='application/json', secure=secure, **headers)

    def make_job(self, kind='status.refresh', arguments=None, principal=None, approve=False):
        job, _ = edge.request_job(self.device, principal or self.principal, kind, arguments or {}, uuid4())
        if approve:
            edge.approve_job(job, self.principal)
        return job

    def started(self, kind='status.refresh', arguments=None):
        job = self.make_job(kind, arguments, approve=kind in edge.MUTATIONS)
        delivered = edge.claim_job(self.credential)
        edge.start_job(job, self.credential, delivered['lease_token'])
        return job, delivered['lease_token']

    def test_command_capability_arguments_and_short_lifetime(self):
        self.assertNotIn('app.command', edge.device_values(self.device, {'admin'})['allowed_actions'])
        with self.assertRaises(ValidationError):
            self.make_job('app.command', {'command': 'hostname'})
        self.device.report = {'capabilities': ['app.command']}
        self.device.save(update_fields=['report'])
        job = self.make_job('app.command', {'command': 'hostname'}, principal=self.reader_principal)
        self.assertEqual(job.status, 'queued')
        self.assertLessEqual(job.expires_at, timezone.now() + timedelta(minutes=2))
        for args in ({'command': ''}, {'command': 'x' * 241}, {'command': 'status\nreboot'},
                     {'command': 'status', 'shell': True}, {'command': ['status']}):
            with self.subTest(arguments=args), self.assertRaises(ValidationError):
                self.make_job('app.command', args)
        delivered = edge.claim_job(self.credential)
        self.assertEqual(delivered['arguments'], {'command': 'hostname'})
        edge.start_job(job, self.credential, delivered['lease_token'])
        edge.complete_job(job, self.credential, delivered['lease_token'], 'succeeded', {
            'command_id': str(job.pk), 'source': 'device', 'output': 'actual-reported-hostname'})
        job.refresh_from_db()
        self.assertEqual(job.result['source'], 'device')

    def test_console_is_tenant_scoped_csrf_protected_and_escapes_output(self):
        self.device.report = {'capabilities': ['app.command']}
        self.device.save(update_fields=['report'])
        self.client.force_login(self.admin)
        url = reverse('edge_console', args=[self.organization.slug, self.device.pk])
        response = self.client.get(url)
        self.assertContains(response, 'Send to JudeyBot')
        post_url = reverse('edge_job_request', args=[self.organization.slug, self.device.pk])
        response = self.client.post(post_url, {'kind': 'app.command', 'command': 'echo <script>alert(1)</script>', 'request_id': str(uuid4())})
        self.assertRedirects(response, url)
        job = self.device.jobs.get(kind='app.command')
        delivered = edge.claim_job(self.credential)
        edge.start_job(job, self.credential, delivered['lease_token'])
        edge.complete_job(job, self.credential, delivered['lease_token'], 'succeeded', {
            'command_id': str(job.pk), 'source': 'device', 'output': '<script>alert(1)</script>'})
        page = self.client.get(url)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)</script>')
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(post_url, {'kind': 'app.command', 'command': 'status'}).status_code, 403)
        self.assertEqual(self.client.get(reverse('edge_console', args=[self.other_org.slug, self.device.pk])).status_code, 404)
        self.device.active = False
        self.device.save(update_fields=['active'])
        self.assertNotContains(self.client.get(url), 'Send to JudeyBot')

    def test_command_rechecks_authority_at_start(self):
        self.device.report = {'capabilities': ['app.command']}
        self.device.save(update_fields=['report'])
        job = self.make_job('app.command', {'command': 'status'}, principal=self.reader_principal)
        delivered = edge.claim_job(self.credential)
        self.reader_member.active = False
        self.reader_member.save(update_fields=['active'])
        with self.assertRaises(PermissionDenied):
            edge.start_job(job, self.credential, delivered['lease_token'])

    def test_registration_is_idempotent_public_and_not_aws_or_kubernetes(self):
        installation, app = edge.register_application(self.organization, self.admin)
        self.assertEqual(installation.pk, self.installation.pk)
        self.assertEqual(app.pk, self.app.pk)
        self.assertEqual(app.client_type, 'public')
        self.assertTrue(app.redirect_uris.endswith('/apps/nce-robotics/oauth/callback'))
        self.assertFalse(installation.deployments.exists())
        self.assertEqual(installation.manifest['external_runtime'], 'edge-device')

    def test_human_context_device_list_and_cross_tenant_isolation(self):
        response = self.api('/context/', method='GET')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['service_id'], str(self.installation.service_id))
        listing = self.api('/devices/', method='GET').json()['devices']
        self.assertEqual([row['id'] for row in listing], [str(self.device.pk)])
        self.assertIn('os.reboot', listing[0]['allowed_actions'])
        other = EdgeDevice.objects.create(installation=self.other_installation, name='Other')
        self.assertEqual(self.api(f'/devices/{other.pk}/', method='GET').status_code, 404)
        self.assertEqual(self.api('/context/', token=self.agent_token, method='GET').status_code, 401)

    def test_role_removal_and_consent_revocation_deny_native_access(self):
        response = self.api('/devices/', method='GET', token=self.reader_token)
        self.assertEqual(response.status_code, 200)
        self.reader_member.roles.clear()
        self.assertEqual(self.api('/devices/', method='GET', token=self.reader_token).status_code, 403)
        self.admin_evidence.revoked = True
        self.admin_evidence.save(update_fields=['revoked'])
        self.assertEqual(self.api('/devices/', method='GET').status_code, 401)

    def test_enrollment_one_use_expiry_name_and_no_raw_secret_storage(self):
        self.assertNotEqual(self.credential.token_hash, self.agent_token)
        self.assertNotIn(self.code, repr(list(EdgeEnrollment.objects.values())))
        with self.assertRaises(PermissionDenied):
            edge.enroll(self.code, 'JudeyBot', '0.1.0', {})
        _, enrollment, code = edge.create_enrollment(self.installation, self.principal, 'New')
        enrollment.expires_at = timezone.now()-timedelta(seconds=1)
        enrollment.save()
        self.assertEqual(self.api('/agent/enroll/', {'code': code}, token='invalid').status_code, 401)

    def test_device_revocation_rechecked_after_authentication(self):
        self.make_job()
        stale = edge.authenticate_device(self.agent_token)
        edge.revoke_device(self.device, self.principal)
        self.assertEqual(self.api('/agent/heartbeat/', {'agent_version': '0.1.0', 'report': {}}, self.agent_token).status_code, 401)
        with self.assertRaises(PermissionDenied):
            edge.claim_job(stale)

    def test_mutations_need_separate_approval_and_idempotent_request(self):
        request_id = str(uuid4())
        values = {'kind': 'os.reboot', 'arguments': {}, 'request_id': request_id}
        first = self.api(f'/devices/{self.device.pk}/jobs/', values)
        self.assertEqual(first.status_code, 201, first.content)
        job_id = first.json()['job']['id']
        self.assertEqual(first.json()['job']['status'], 'awaiting_approval')
        second = self.api(f'/devices/{self.device.pk}/jobs/', values)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['job']['id'], job_id)
        self.assertIsNone(edge.claim_job(self.credential))
        values['kind'] = 'status.refresh'
        self.assertEqual(self.api(f'/devices/{self.device.pk}/jobs/', values).status_code, 409)
        self.assertEqual(self.api(f'/jobs/{job_id}/approve/', {}).status_code, 200)
        delivered = edge.claim_job(self.credential)
        self.assertEqual(delivered['id'], job_id)

    def test_reader_cannot_request_mutations_or_approve(self):
        self.assertEqual(self.api(f'/devices/{self.device.pk}/jobs/',
            {'kind': 'os.reboot', 'arguments': {}, 'request_id': str(uuid4())}, self.reader_token).status_code, 403)
        job = self.make_job('os.reboot')
        self.assertEqual(self.api(f'/jobs/{job.pk}/approve/', {}, self.reader_token).status_code, 403)
        self.assertEqual(self.api(f'/devices/{self.device.pk}/jobs/',
            {'kind': 'os.update.check', 'arguments': {}, 'request_id': str(uuid4())}, self.reader_token).status_code, 403)

    def test_live_request_and_approval_authority_at_claim_and_start(self):
        job = self.make_job('os.reboot', approve=True)
        self.app.active = False
        self.app.save(update_fields=['active'])
        self.assertIsNone(edge.claim_job(self.credential))
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.app.active = True
        self.app.save(update_fields=['active'])
        job = self.make_job('os.reboot', approve=True)
        delivered = edge.claim_job(self.credential)
        self.admin_evidence.revoked = True
        self.admin_evidence.save(update_fields=['revoked'])
        with self.assertRaises(PermissionDenied):
            edge.start_job(job, self.credential, delivered['lease_token'])

    def test_other_device_cannot_start_result_or_fetch_artifact(self):
        other, _, code = edge.create_enrollment(self.installation, self.principal, 'Other robot')
        _, credential, token = edge.enroll(code, 'Other robot', '0.1.0', {})
        job, lease = self.started()
        self.assertEqual(self.api(f'/agent/jobs/{job.pk}/start/', {'lease_token': lease}, token).status_code, 404)
        with self.assertRaises(PermissionDenied):
            edge.complete_job(job, credential, lease, 'succeeded', {})

    def test_result_is_idempotent_and_conflicting_result_rejected(self):
        job, lease = self.started()
        data = {'lease_token': lease, 'status': 'succeeded', 'result': {'status': 'ready'}}
        self.assertEqual(self.api(f'/agent/jobs/{job.pk}/result/', data, self.agent_token).status_code, 200)
        self.assertEqual(self.api(f'/agent/jobs/{job.pk}/result/', data, self.agent_token).status_code, 200)
        data['result'] = {'status': 'other'}
        self.assertEqual(self.api(f'/agent/jobs/{job.pk}/result/', data, self.agent_token).status_code, 409)
        listing = self.api(f'/devices/{self.device.pk}/jobs/', method='GET').json()
        self.assertNotIn(lease, json.dumps(listing))

    def test_expired_mutation_is_uncertain_and_never_requeued(self):
        job = self.make_job('os.reboot', approve=True)
        delivered = edge.claim_job(self.credential)
        EdgeJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(edge.claim_job(self.credential))
        job.refresh_from_db()
        self.assertEqual(job.status, 'uncertain')
        with self.assertRaises(edge.Conflict):
            edge.start_job(job, self.credential, delivered['lease_token'])
        self.assertIsNone(edge.claim_job(self.credential))

    def test_expired_read_is_failed_without_reissuing_same_identifier(self):
        job, _ = self.started()
        EdgeJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        edge.expire_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIsNone(edge.claim_job(self.credential))

    def test_reboot_acknowledgement_survives_normal_lease_expiry(self):
        job, lease = self.started('os.reboot')
        edge.complete_job(job, self.credential, lease, 'rebooting', {'boot_id': 'old'})
        EdgeJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(edge.claim_job(self.credential))
        edge.complete_job(job, self.credential, lease, 'succeeded', {'boot_id': 'new'})
        job.refresh_from_db()
        self.assertEqual(job.status, 'succeeded')

    def test_heartbeat_renews_lease_and_returns_cancellation(self):
        job, lease = self.started()
        edge.cancel_job(job, self.principal)
        ids = edge.heartbeat(self.credential, '0.1.0', {'hostname': 'JudeyBot'}, job.pk, lease)
        self.assertEqual(ids, [str(job.pk)])
        with self.assertRaises(PermissionDenied):
            edge.heartbeat(self.credential, '0.1.0', {}, job.pk, 'wrong-' * 8)
        with self.assertRaises(ValidationError):
            edge.heartbeat(self.credential, '0.1.0', {'password': str(uuid4())})

    def test_os_apply_requires_exact_recent_plan(self):
        with self.assertRaises(ValidationError):
            self.make_job('os.update.apply', {'plan_id': str(uuid4()), 'sha256': 'a' * 64})
        check, lease = self.started('os.update.check')
        plan = {'plan_id': str(uuid4()), 'sha256': 'a' * 64, 'packages': [{'name': 'example', 'version': '1'}]}
        edge.complete_job(check, self.credential, lease, 'succeeded', {'plan': plan})
        job = self.make_job('os.update.apply', {key: plan[key] for key in ['plan_id', 'sha256']})
        self.assertEqual(job.status, 'awaiting_approval')
        EdgeJob.objects.filter(pk=check.pk).update(completed_at=timezone.now()-timedelta(hours=2))
        with self.assertRaises(ValidationError):
            edge.approve_job(job, self.principal)

    def test_no_arbitrary_shell_arguments(self):
        for kind, arguments in [('shell', {'command': 'id'}), ('os.reboot', {'command': 'id'}), ('app.rollback', {'path': '/etc'})]:
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                self.make_job(kind, arguments)

    def test_safe_release_upload_immutability_and_artifact_job_scope(self):
        content = archive()
        release, created = edge.upload_release(self.installation, self.principal, content)
        self.assertTrue(created)
        same, created = edge.upload_release(self.installation, self.principal, content)
        self.assertFalse(created)
        self.assertEqual(same.pk, release.pk)
        with self.assertRaises(ValidationError):
            release.save()
        with self.assertRaises(PermissionDenied):
            edge.authorized_artifact(self.credential, release)
        job = self.make_job('app.deploy', {'release_id': str(release.pk)}, approve=True)
        delivered = edge.claim_job(self.credential)
        self.assertEqual(set(delivered['arguments']['artifact']), {'release_id', 'url', 'sha256', 'size_bytes', 'version'})
        self.assertEqual(delivered['arguments']['artifact']['sha256'], release.sha256)
        self.assertTrue(delivered['arguments']['artifact']['url'].endswith(str(release.pk) + '/'))
        fetched = self.api(f'/agent/artifacts/{release.pk}/', token=self.agent_token, method='GET')
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(hashlib.sha256(b''.join(fetched.streaming_content)).hexdigest(), release.sha256)
        Path(release.artifact_path).write_bytes(b'tampered')
        self.assertEqual(self.api(f'/agent/artifacts/{release.pk}/', token=self.agent_token, method='GET').status_code, 400)

    def test_job_identity_is_immutable_and_changed_plan_cannot_be_approved(self):
        job = self.make_job('os.reboot')
        job.kind = 'app.rollback'
        with self.assertRaises(ValidationError):
            job.save()
        EdgeJob.objects.filter(pk=job.pk).update(kind='app.rollback')
        with self.assertRaises(edge.Conflict):
            edge.approve_job(job, self.principal)

    def test_redeploy_current_release_preserves_rollback_target(self):
        old, _ = edge.upload_release(self.installation, self.principal, archive(version='1.0.0'))
        current, _ = edge.upload_release(self.installation, self.principal, archive(version='2.0.0'))
        EdgeDevice.objects.filter(pk=self.device.pk).update(current_release=current, previous_release=old)
        job, lease = self.started('app.deploy', {'release_id': str(current.pk)})
        edge.complete_job(job, self.credential, lease, 'succeeded', {'version': '2.0.0'})
        self.device.refresh_from_db()
        self.assertEqual(self.device.current_release_id, current.pk)
        self.assertEqual(self.device.previous_release_id, old.pk)

    def test_artifact_access_rechecks_requester_permissions(self):
        release, _ = edge.upload_release(self.installation, self.principal, archive())
        self.make_job('app.deploy', {'release_id': str(release.pk)}, approve=True)
        edge.claim_job(self.credential)
        Membership.objects.filter(organization=self.organization, user=self.admin).update(active=False)
        with self.assertRaises(PermissionDenied):
            edge.authorized_artifact(self.credential, release)

    def test_production_storage_is_required_only_for_artifacts(self):
        with override_settings(CORE_EDGE_ARTIFACT_ROOT=''):
            with self.assertRaisesMessage(ValidationError, 'persistent CORE_EDGE_ARTIFACT_ROOT'):
                edge.upload_release(self.installation, self.principal, archive())
        self.assertEqual(self.api('/context/', method='GET').status_code, 200)

    def test_archive_traversal_links_devices_and_pax_are_rejected(self):
        for name, kind in [('../bad', tarfile.REGTYPE), ('/bad', tarfile.REGTYPE), ('link', tarfile.SYMTYPE),
                           ('hard', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)]:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.linkname = 'app/main.py'
            with self.subTest(name=name), self.assertRaises(ValidationError):
                edge.validate_archive(archive(extra=item))
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
            item = tarfile.TarInfo('a')
            item.pax_headers = {'comment': 'extended'}
            tar.addfile(item, io.BytesIO(b''))
        with self.assertRaises(ValidationError):
            edge.validate_archive(content.getvalue())

    def test_archive_limits_versions_collisions_and_duplicate_json(self):
        from unittest.mock import patch
        extra = tarfile.TarInfo('unexpected.txt')
        with self.assertRaises(ValidationError):
            edge.validate_archive(archive(extra=extra))
        extra = tarfile.TarInfo('app')
        with self.assertRaises(ValidationError):
            edge.validate_archive(archive(extra=extra))
        for version in ['a+b', 'a..b', 'a'*65]:
            with self.subTest(version=version), self.assertRaises(ValidationError):
                edge.validate_archive(archive(version=version))
        with patch.object(edge, 'MAX_EXPANDED', 1024), self.assertRaises(ValidationError):
            edge.validate_archive(gzip.compress(b'x'*(1024*1024+2048)))
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
            content = b'{"schema_version":1,"schema_version":1,"application":"nce-robotics","version":"1","entrypoint":"app/main.py"}'
            item = tarfile.TarInfo('nce-release.json'); item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
            item = tarfile.TarInfo('app/main.py'); tar.addfile(item, io.BytesIO(b''))
        with self.assertRaises(ValidationError):
            edge.validate_archive(data.getvalue())

    def test_large_bounded_os_plan_result_and_report_are_supported(self):
        plan = {'packages': [{'name': 'package' + str(i), 'version': '1.2.3'} for i in range(2000)]}
        self.assertEqual(edge.safe_document(plan), plan)
        with self.assertRaises(ValidationError):
            edge.safe_document({'packages': list(range(2001))})

    def test_device_tag_restriction_applies_to_nested_jobs(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug='restricted-robot', name='Restricted')
        self.device.permission_tags.add(tag)
        self.assertEqual(self.api('/devices/', token=self.reader_token, method='GET').json()['devices'], [])
        with self.assertRaises(PermissionDenied):
            self.make_job(principal=self.reader_principal)
        with self.assertRaises(PermissionDenied):
            edge.authenticate_device(self.agent_token)

    def test_portal_exposes_release_digest_and_exact_package_plan_for_review(self):
        release, _ = edge.upload_release(self.installation, self.principal, archive('3.2.1'))
        self.make_job('app.deploy', {'release_id': str(release.pk)})
        check, lease = self.started('os.update.check')
        plan = {'plan_id': str(uuid4()), 'sha256': 'a' * 64,
                'created_at': timezone.now().isoformat(),
                'packages': [{'name': 'python3-example', 'version': '3.12.9-reviewed'}]}
        edge.complete_job(check, self.credential, lease, 'succeeded', plan)
        self.make_job('os.update.apply', {'plan_id': plan['plan_id'], 'sha256': plan['sha256']})
        self.client.force_login(self.admin)
        page = self.client.get(reverse('edge_dashboard', args=[self.organization.slug]))
        self.assertContains(page, 'Target release:')
        self.assertContains(page, release.sha256)
        self.assertContains(page, 'Reviewed package plan (1 packages)')
        self.assertContains(page, 'python3-example')
        self.assertContains(page, '3.12.9-reviewed')
        self.assertContains(page, 'Deploy software')

    def test_portal_role_permissions_and_csrf(self):
        self.client.force_login(self.reader)
        page = self.client.get(reverse('edge_dashboard', args=[self.organization.slug]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Create enrollment code')
        self.assertNotContains(page, 'Upload release')
        self.assertEqual(self.client.post(reverse('edge_enroll', args=[self.organization.slug]), {'name': 'No'}).status_code, 403)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(reverse('edge_enroll', args=[self.organization.slug]), {'name': 'No'}).status_code, 403)

    def test_https_required_and_native_association_requires_real_configuration(self):
        self.assertEqual(self.api('/context/', method='GET', secure=False).status_code, 400)
        self.assertEqual(self.client.get('/.well-known/apple-app-site-association').status_code, 404)
        with override_settings(CORE_IOS_TEAM_ID='AB12345678'):
            result = self.client.get('/.well-known/apple-app-site-association').json()
            self.assertEqual(result['webcredentials']['apps'], ['AB12345678.ai.nnytech.NCERobotics'])
            self.assertEqual(result['applinks']['details'][0]['appIDs'], ['AB12345678.ai.nnytech.NCERobotics'])
            self.assertEqual(result['applinks']['details'][0]['components'][0]['/'], '/apps/nce-robotics/oauth/callback')
        callback = self.client.get('/apps/nce-robotics/oauth/callback?code=sensitive-code&state=sensitive-state')
        self.assertNotContains(callback, 'sensitive')
        self.assertEqual(callback['Cache-Control'], 'no-store')
