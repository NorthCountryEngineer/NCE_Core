"""Independent disposable Core authority for the real HTTP federation test."""
import json
import os
from pathlib import Path
import sys
from wsgiref.simple_server import make_server

directory, port, source = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings'
os.environ['CORE_DEBUG'] = '1'
os.environ['CORE_ISSUER'] = 'http://127.0.0.1:' + str(port)
os.environ['CORE_ALLOWED_HOSTS'] = '127.0.0.1,localhost'

from django.conf import settings
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(directory / 'authority.sqlite3')}}
import django
django.setup()
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from identity.access import Principal
from identity.models import Organization
from platform_services.models import MarketplacePackage
from platform_services.services import install_core, install_package, issue_workload_credential
from core_chat.models import Participant
from core_chat.services import create_conversation

call_command('migrate', interactive=False, verbosity=0)
organization = Organization.objects.create(name='Independent chat authority', slug='authority')
install_core(organization)
capabilities = ['chat.read', 'chat.send', 'chat.manage', 'chat.ai', 'chat.federate']
package = MarketplacePackage.objects.create(slug='trusted-chat-peer', name='Trusted chat peer', kind='application',
    description='Disposable federation fixture', manifest={'image': 'registry.example.test/peer@sha256:' + 'b' * 64,
    'dependencies': ['core-chat'], 'capabilities': capabilities})
installation = install_package(organization, package, capabilities)
credential, token = issue_workload_credential(installation, 'Trusted instance')
room = create_conversation(Principal(credential=credential), organization, 'Shared authoritative conversation', ai_enabled=True)
participant = Participant.objects.get(conversation=room, installation=installation)
participant.allow_ai = True
participant.federated_issuer = source
participant.full_clean()
participant.save()
credential_file = directory / 'peer.credential'
credential_file.write_text(token)
credential_file.chmod(0o600)
(directory / 'state.json').write_text(json.dumps({'organization_id': str(organization.pk), 'conversation_id': str(room.pk),
    'installation_id': str(installation.pk), 'participant_id': participant.pk}))
with make_server('127.0.0.1', port, get_wsgi_application()) as server:
    server.serve_forever()
