"""Independent Core deployment used only by local real-HTTP federation tests."""
import json
import os
from pathlib import Path
import secrets
from socketserver import ThreadingMixIn
import sys
from wsgiref.simple_server import WSGIServer, make_server

directory, port, recipient = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
configuration = json.loads((directory/'recipient.json').read_text())
os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings'
os.environ['CORE_DEBUG'] = '1'
os.environ['CORE_ISSUER'] = 'http://127.0.0.1:' + str(port)
os.environ['CORE_ALLOWED_HOSTS'] = '127.0.0.1,localhost'
os.environ['CORE_FABRIC_SECRET_ROOT'] = str(directory/'secrets')

from django.conf import settings
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(directory/'authority.sqlite3')}}
import django
django.setup()
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from identity.models import Membership, Organization, User
from platform_services.data import publish
from platform_services.models import Dataset, MarketplacePackage
from platform_services.services import install_core, install_package, issue_workload_credential
from core_fabric.models import FabricContract, FabricPeer
from core_fabric.services import fingerprint, terms

call_command('migrate', interactive=False, verbosity=0)
organization = Organization.objects.create(name='Independent source', slug='source')
install_core(organization)
admin_secret = secrets.token_urlsafe(32)
admin = User.objects.create_user('fabric-admin@example.test', password=admin_secret)
Membership.objects.create(organization=organization, user=admin, kind='administrator')
package = MarketplacePackage.objects.create(slug='fabric-peer-app', name='Dedicated recipient bridge', kind='application',
    description='Temporary federation fixture', manifest={'image': 'registry.example.test/fabric@sha256:'+'b'*64,
    'dependencies': ['core-fabric'], 'capabilities': ['fabric.peer', 'data.read', 'data.write']})
installation = install_package(organization, package, package.manifest['capabilities'])
credential, token = issue_workload_credential(installation, 'Temporary bridge')
folder = directory/'secrets'/str(organization.pk)
folder.mkdir(parents=True)
credential_file = folder/'recipient.credential'
credential_file.write_text(configuration['token'])
credential_file.chmod(0o600)
peer = FabricPeer(organization=organization, name='Recipient', issuer=recipient,
    remote_organization_id=configuration['organization_id'], inbound_installation=installation, credential_file=str(credential_file))
peer.full_clean()
peer.save()
dataset = Dataset.objects.create(organization=organization, name='Joint project', slug='joint-project', data_class='synthetic',
    schema={'type': 'object', 'required': ['project', 'title']})
training_id = '00000000-0000-4000-8000-000000000001'
publish(dataset, 'private', {'project': 'private', 'title': 'Private project', 'private': 'Source-only field'})
publish(dataset, 'joint', {'project': 'joint', 'title': 'Approved project', 'private': 'Source-only field'}, training_run_id=training_id)
contract = FabricContract(organization=organization, name='Joint project', peer=peer, direction='export', dataset=dataset,
    data_class='synthetic', purpose_code='joint-project', fields=['project', 'title'], filters={'project': 'joint'}, allow_write=True)
contract.full_clean()
contract.save()
for filename, value in [('peer.credential', token), ('admin.credential', admin_secret)]:
    path = directory/filename
    path.write_text(value)
    path.chmod(0o600)
(directory/'state.json').write_text(json.dumps({'organization_id': str(organization.pk), 'contract_id': str(contract.pk),
    'dataset_id': str(dataset.pk), 'terms': terms(contract), 'terms_hash': fingerprint(contract)}))


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


with make_server('127.0.0.1', port, get_wsgi_application(), server_class=ThreadedServer) as server:
    server.serve_forever()
