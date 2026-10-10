"""Independent Core deployment: actual HTTP permits, member query, and revocation."""
from datetime import timedelta
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import requests
from django.apps import apps
from django.test import LiveServerTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from core_client import CoreClient
from core_fabric.models import FabricContract, FabricPeer, FabricPermit
from core_fabric.services import fingerprint
from identity.models import Membership, Organization, Role, ServicePermission, User
from platform_services.catalog import seed_catalog
from platform_services.environments import create_environment
from platform_services.models import DataRecord, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential
from core_ai.models import QuerySource


class LiveFederatedQueryTests(LiveServerTestCase):
    def test_delegated_user_queries_independent_authority_only_after_both_approvals(self):
        seed_catalog(apps, None)
        org = Organization.objects.create(name='AI recipient', slug='recipient')
        install_core(org)
        environment = create_environment(org, name='sandbox', namespace='ai-sandbox', application_origin='https://app.example.test')
        admin = User.objects.create_user('ai-recipient-admin@example.test')
        user = User.objects.create_user('ai-recipient-user@example.test')
        Membership.objects.create(organization=org, user=admin, kind='administrator')
        member = Membership.objects.create(organization=org, user=user)
        member.access_tags.add(environment.boundary_tag)
        role = Role.objects.create(organization=org, slug='joint-query', name='Joint query')
        member.roles.add(role)
        def application(slug, capabilities):
            package = MarketplacePackage.objects.create(slug=slug, name=slug, kind='application', description='Isolated HTTP fixture',
                manifest={'image': 'registry.example.test/ai@sha256:' + 'c' * 64, 'capabilities': capabilities})
            installed = install_package(org, package, capabilities)
            return installed
        inbound = application('ai-source-bridge', ['fabric.peer'])
        _, callback = issue_workload_credential(inbound, 'Temporary source callback', environment)
        consumer = application('ai-consumer', ['ai.query', 'fabric.read'])
        role.permissions.add(*ServicePermission.objects.filter(service__organization=org, codename__in=['ai-query', 'fabric-read']))
        node = RuntimeNode.objects.create(organization=org, name='Sandbox', namespace=environment.namespace, environment=environment,
            credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(consumer, node, 'https://app.example.test')
        _, token = issue_workload_credential(consumer, 'Temporary query client', environment, deployment)
        raw_user = secrets.token_urlsafe(48)
        access = AccessToken(application=deployment.application, user=user, scope='openid authorization',
            expires=timezone.now() + timedelta(minutes=5))
        set_token_value(access, raw_user)
        access.save()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root / 'recipient.json'
            configuration.write_text(json.dumps({'organization_id': str(org.pk), 'token': callback}))
            configuration.chmod(0o600)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            issuer = f'http://127.0.0.1:{port}'
            process = subprocess.Popen([sys.executable, '-m', 'core_fabric.tests.federated_instance', directory, str(port), self.live_server_url],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('Independent authority exited during startup.')
                    try:
                        if requests.get(issuer + '/health/', timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.1)
                else:
                    self.fail('Independent authority did not become ready.')
                state = json.loads((root / 'state.json').read_text())
                folder = root / str(org.pk)
                folder.mkdir()
                credential_file = folder / 'source.credential'
                shutil.copy(root / 'peer.credential', credential_file)
                credential_file.chmod(0o600)
                with override_settings(DEBUG=True, ISSUER=self.live_server_url, CORE_FABRIC_SECRET_ROOT=directory):
                    peer = FabricPeer.objects.create(organization=org, name='Independent authority', issuer=issuer,
                        remote_organization_id=state['organization_id'], inbound_installation=inbound, credential_file=str(credential_file))
                    offered = state['terms']
                    contract = FabricContract.objects.create(organization=org, name='Joint query', peer=peer, direction='import',
                        agreement_id=offered['agreement_id'], remote_dataset_id=state['dataset_id'], data_class='synthetic',
                        purpose_code='joint-project', fields=offered['fields'], filters=offered['filters'], allow_write=True)
                    contract.permission_tags.add(environment.boundary_tag)
                    contract.allowed_memberships.add(member)
                    contract.allowed_installations.add(consumer)
                    source = QuerySource.objects.create(organization=org, name='Remote reports', installation=consumer,
                        environment=environment, contract=contract, fields=['title'], required_permissions=['fabric-read'])
                    source.allowed_roles.add(role)
                    source.allowed_consumers.add(consumer)
                    client = CoreClient(self.live_server_url + '/api/platform/v1', token, str(org.pk), allow_local_http=True, purpose_code='joint-project')
                    self.assertFalse(client.ai_sources(user_access_token=raw_user)['sources'])
                    self.client.force_login(admin)
                    self.assertEqual(self.client.post(reverse('fabric_approve', args=[org.slug, contract.pk]), {'terms_hash': fingerprint(contract)}).status_code, 302)
                    before = client.ai_query([str(source.pk)], user_access_token=raw_user)
                    self.assertTrue(before['partial'])
                    self.assertFalse(before['evidence'])
                    browser = requests.Session()
                    try:
                        page = browser.get(issuer + '/accounts/login/', timeout=10)
                        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
                        result = browser.post(issuer + '/accounts/login/', data={'username': 'fabric-admin@example.test',
                            'password': (root / 'admin.credential').read_text(), 'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False)
                        self.assertEqual(result.status_code, 302)
                        approval = issuer + '/organizations/source/fabric/contracts/' + state['contract_id'] + '/approve/'
                        csrf = browser.cookies['csrftoken']
                        self.assertEqual(browser.post(approval, data={'terms_hash': state['terms_hash'], 'csrfmiddlewaretoken': csrf},
                            timeout=10, allow_redirects=False).status_code, 302)
                        value = client.ai_query([str(source.pk)], user_access_token=raw_user, text='Approved')
                        self.assertEqual(value['evidence'][0]['data'], {'title': 'Approved project'})
                        self.assertEqual(value['evidence'][0]['authority_organization_id'], state['organization_id'])
                        self.assertTrue(value['evidence'][0]['synthetic'])
                        self.assertFalse(value['partial'])
                        self.assertFalse(DataRecord.objects.exists(), 'Remote evidence must not be copied into the recipient dataset store.')
                        self.assertFalse(FabricPermit.objects.exists())
                        revoke = issuer + '/organizations/source/fabric/contracts/' + state['contract_id'] + '/revoke/'
                        self.assertEqual(browser.post(revoke, data={'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False).status_code, 302)
                        denied = client.ai_query([str(source.pk)], user_access_token=raw_user)
                        self.assertTrue(denied['partial'])
                        self.assertFalse(denied['evidence'])
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=10)
