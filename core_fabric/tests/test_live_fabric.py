from datetime import datetime
import json
from pathlib import Path
import re
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

from core_client import CoreClient, CoreServiceError
from identity.models import AuditEvent, Membership, Organization, User
from platform_services.catalog import seed_catalog
from platform_services.models import DataRecord, MarketplacePackage
from platform_services.services import install_core, install_package, issue_workload_credential
from core_fabric.models import FabricContract, FabricPeer, FabricPermit
from core_fabric.services import fingerprint


class LiveFabricTests(LiveServerTestCase):
    def test_independent_deployments_enforce_two_admin_approvals_projection_writes_and_revocation(self):
        seed_catalog(apps, None)
        org = Organization.objects.create(name='Independent recipient', slug='recipient')
        install_core(org)
        admin = User.objects.create_user('recipient-admin@example.test')
        Membership.objects.create(organization=org, user=admin, kind='administrator')
        def application(slug, capabilities):
            package = MarketplacePackage.objects.create(slug=slug, name=slug, kind='application', description='Temporary HTTP fixture',
                manifest={'image': 'registry.example.test/fabric@sha256:'+'c'*64, 'dependencies': ['core-fabric'], 'capabilities': capabilities})
            installed = install_package(org, package, capabilities)
            credential, raw = issue_workload_credential(installed, 'Temporary HTTP client')
            return installed, raw
        inbound, callback_token = application('source-bridge', ['fabric.peer'])
        consumer, token = application('fabric-consumer', ['fabric.read', 'fabric.write'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root/'recipient.json'
            configuration.write_text(json.dumps({'organization_id': str(org.pk), 'token': callback_token}))
            configuration.chmod(0o600)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            issuer = 'http://127.0.0.1:' + str(port)
            process = subprocess.Popen([sys.executable, '-m', 'core_fabric.tests.federated_instance', directory, str(port), self.live_server_url],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('Independent fabric authority exited during startup.')
                    try:
                        if requests.get(issuer+'/health/', timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.1)
                else:
                    self.fail('Independent authority did not become ready.')
                state = json.loads((root/'state.json').read_text())
                folder = root/str(org.pk)
                folder.mkdir()
                credential_file = folder/'source.credential'
                shutil.copy(root/'peer.credential', credential_file)
                credential_file.chmod(0o600)
                with override_settings(DEBUG=True, ISSUER=self.live_server_url, CORE_FABRIC_SECRET_ROOT=directory):
                    peer = FabricPeer(organization=org, name='Source', issuer=issuer, remote_organization_id=state['organization_id'],
                        inbound_installation=inbound, credential_file=str(credential_file))
                    peer.full_clean()
                    peer.save()
                    offered = state['terms']
                    contract = FabricContract(organization=org, name='Joint project', peer=peer, direction='import', agreement_id=offered['agreement_id'],
                        remote_dataset_id=state['dataset_id'], data_class='synthetic', purpose_code=offered['purpose_code'],
                        fields=offered['fields'], filters=offered['filters'], allow_read=True, allow_write=True)
                    contract.full_clean()
                    contract.save()
                    contract.allowed_installations.add(consumer)
                    self.assertEqual(fingerprint(contract), state['terms_hash'])
                    client = CoreClient(self.live_server_url+'/api/platform/v1', token, str(org.pk), allow_local_http=True, purpose_code='joint-project')
                    with self.assertRaises(CoreServiceError):
                        client.fabric_records(contract.pk)
                    self.client.force_login(admin)
                    result = self.client.post(reverse('fabric_approve', args=[org.slug, contract.pk]), {'terms_hash': fingerprint(contract)})
                    self.assertEqual(result.status_code, 302)
                    with self.assertRaises(CoreServiceError):
                        client.fabric_records(contract.pk)
                    # The second approval is an actual, CSRF-protected administrator request on the independent source.
                    browser = requests.Session()
                    page = browser.get(issuer+'/accounts/login/', timeout=10)
                    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
                    logged = browser.post(issuer+'/accounts/login/', data={'username': 'fabric-admin@example.test',
                        'password': (root/'admin.credential').read_text(), 'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False)
                    self.assertEqual(logged.status_code, 302)
                    csrf = browser.cookies['csrftoken']
                    approval_url = issuer+'/organizations/source/fabric/contracts/'+state['contract_id']+'/approve/'
                    remote_approved = browser.post(approval_url, data={'terms_hash': state['terms_hash'], 'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False)
                    self.assertEqual(remote_approved.status_code, 302)
                    page = client.invoke_fabric_tool('core_fabric_read', {'limit': 1}, contract_id=contract.pk)
                    self.assertEqual(page['records'][0]['data'], {'project': 'joint', 'title': 'Approved project'})
                    self.assertTrue(page['records'][0]['synthetic'])
                    self.assertTrue(page['records'][0]['training_run_id'])
                    self.assertEqual(page['authority_organization_id'], state['organization_id'])
                    written = client.invoke_fabric_tool('core_fabric_publish', {'data': {'project': 'joint', 'title': 'Shared AI work'}},
                        contract_id=contract.pk, event_id='shared-write')
                    self.assertTrue(written['created'])
                    self.assertFalse(client.fabric_publish(contract.pk, 'shared-write', {'project': 'joint', 'title': 'Shared AI work'})['created'])
                    shared = client.fabric_records(contract.pk)['records']
                    self.assertEqual(len(shared), 2)
                    self.assertEqual(shared[-1]['federation']['actor'], {'kind': 'application', 'id': str(consumer.pk)})
                    self.assertFalse(DataRecord.objects.exists(), 'Records remain owned by the authoritative deployment.')
                    self.assertFalse(FabricPermit.objects.exists())
                    remote_revoke = issuer+'/organizations/source/fabric/contracts/'+state['contract_id']+'/revoke/'
                    self.assertEqual(browser.post(remote_revoke, data={'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False).status_code, 302)
                    with self.assertRaises(CoreServiceError):
                        client.fabric_records(contract.pk)
                    self.assertEqual(browser.post(approval_url, data={'terms_hash': state['terms_hash'], 'csrfmiddlewaretoken': csrf}, timeout=10, allow_redirects=False).status_code, 302)
                    self.assertEqual(len(client.fabric_records(contract.pk)['records']), 2)
                    self.assertEqual(self.client.post(reverse('fabric_revoke', args=[org.slug, contract.pk])).status_code, 302)
                    with self.assertRaises(CoreServiceError):
                        client.fabric_publish(contract.pk, 'revoked', {'project': 'joint', 'title': 'Denied'})
                    self.assertTrue(AuditEvent.objects.filter(action='fabric.federated_write').exists())
                    browser.close()
            finally:
                process.terminate()
                process.wait(timeout=10)
