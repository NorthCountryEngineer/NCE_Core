import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import requests
from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import LiveServerTestCase, SimpleTestCase, override_settings

from core_client import CoreClient, CoreServiceError
from identity.models import Organization
from platform_services.models import MarketplacePackage
from platform_services.catalog import seed_catalog
from platform_services.services import install_core, install_package, issue_workload_credential
from core_chat.models import Conversation, FederationConnection, Message, Participant
from core_chat.federation import credential_path, validate_issuer


class FederationConfigurationTests(SimpleTestCase):
    @override_settings(DEBUG=False, CORE_CHAT_FEDERATION_ORIGINS=['https://approved.example'])
    def test_requires_exact_https_origin_and_rejects_redirect_style_urls(self):
        self.assertEqual(validate_issuer('https://approved.example'), 'https://approved.example')
        for value in ['http://approved.example', 'https://other.example', 'https://approved.example/path',
                      'https://approved.example@other.example', 'https://approved.example?target=other', 'https://approved.example/']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_issuer(value)

    def test_credential_mount_is_tenant_bound_and_rejects_symlink_escape(self):
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as directory:
            organization = Organization(id=uuid4())
            root = Path(directory)
            folder = root / str(organization.pk)
            folder.mkdir()
            (root / 'outside').write_text(secrets.token_urlsafe(32))
            (folder / 'link').symlink_to(root / 'outside')
            connection = FederationConnection(organization=organization, credential_file=str(folder / 'link'))
            with override_settings(CORE_CHAT_SECRET_ROOT=directory), self.assertRaises(ValidationError):
                credential_path(connection)


class LiveFederationTests(LiveServerTestCase):
    def test_independent_instances_share_history_ai_messages_and_live_revocation(self):
        seed_catalog(apps, None)
        organization = Organization.objects.create(name='Consuming Core', slug='consumer')
        install_core(organization)
        capabilities = ['chat.read', 'chat.send', 'chat.manage', 'chat.ai', 'chat.federate']
        package = MarketplacePackage.objects.create(slug='chat-application', name='Chat application', kind='application',
            description='Chat consumer', manifest={'image': 'registry.example.test/consumer@sha256:' + 'a' * 64,
            'dependencies': ['core-chat'], 'capabilities': capabilities})
        installation = install_package(organization, package, capabilities)
        credential, token = issue_workload_credential(installation, 'Chat consumer')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            issuer = 'http://127.0.0.1:' + str(port)
            # The peer has a separate process, settings, database, and organization IDs.
            process = subprocess.Popen([sys.executable, '-m', 'core_chat.tests.federated_instance', directory, str(port), self.live_server_url],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('Independent chat authority exited during startup.')
                    try:
                        if requests.get(issuer + '/health/', timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.1)
                else:
                    self.fail('Independent chat authority did not become ready.')
                state = json.loads((root / 'state.json').read_text())
                folder = root / str(organization.pk)
                folder.mkdir()
                token_path = folder / 'peer.credential'
                shutil.copy(root / 'peer.credential', token_path)
                token_path.chmod(0o600)
                with override_settings(DEBUG=True, ISSUER=self.live_server_url, CORE_CHAT_SECRET_ROOT=directory):
                    connection = FederationConnection(organization=organization, name='Independent authority', issuer=issuer,
                        remote_organization_id=state['organization_id'], credential_file=str(token_path))
                    connection.full_clean()
                    connection.save()
                    room = Conversation.objects.create(organization=organization, title='Shared conversation', ai_enabled=True,
                        connection=connection, remote_conversation_id=state['conversation_id'])
                    local_participant = Participant.objects.create(conversation=room, installation=installation, allow_ai=True)
                    client = CoreClient(self.live_server_url + '/api/platform/v1', token, str(organization.pk), allow_local_http=True)
                    remote = CoreClient(issuer + '/api/platform/v1', token_path.read_text(), state['organization_id'], allow_local_http=True)
                    first = client.chat_send(str(room.pk), 'shared-message', 'Message through consuming instance')
                    self.assertTrue(first['created'])
                    self.assertEqual(first['authority_issuer'], issuer)
                    self.assertEqual(first['message']['actor_kind'], 'federated_application')
                    self.assertEqual(first['message']['actor_id'], str(installation.pk))
                    self.assertFalse(client.chat_send(str(room.pk), 'shared-message', 'Message through consuming instance')['created'])
                    ai = client.invoke_chat_tool({'text': 'AI conversation handoff'}, conversation_id=str(room.pk),
                        request_id='ai-handoff', agent_name='conversation-assistant')
                    self.assertEqual(ai['message']['actor_kind'], 'federated_ai')
                    self.assertEqual(ai['message']['agent_name'], 'conversation-assistant')
                    authoritative = remote.chat_messages(state['conversation_id'])
                    shared = client.chat_messages(str(room.pk))
                    self.assertEqual([m['id'] for m in shared['messages']], [m['id'] for m in authoritative['messages']])
                    self.assertEqual(len(shared['messages']), 2)
                    self.assertFalse(Message.objects.exists(), 'Shared history must remain authoritative, without local cached copies.')
                    # Expected remote organization is checked before any response is consumed.
                    from uuid import uuid4
                    connection.remote_organization_id = uuid4()
                    connection.save()
                    with self.assertRaises(CoreServiceError):
                        client.chat_messages(str(room.pk))
                    connection.remote_organization_id = state['organization_id']
                    connection.save()
                    local_participant.active = False
                    local_participant.save()
                    with self.assertRaises(CoreServiceError):
                        client.chat_messages(str(room.pk))
                    local_participant.active = True
                    local_participant.save()
                    # Revoking the remote peer grant immediately closes the local bridge.
                    remote.chat_participant(state['conversation_id'], installation_id=state['installation_id'], active=False)
                    with self.assertRaises(CoreServiceError):
                        client.chat_messages(str(room.pk))
            finally:
                process.terminate()
                process.wait(timeout=10)

    def test_ai_tool_does_not_accept_model_selected_destination_or_identity(self):
        client = CoreClient(self.live_server_url + '/api/platform/v1', secrets.token_urlsafe(48), 'organization', allow_local_http=True)
        with patch.object(client, 'chat_send') as send:
            with self.assertRaises(ValueError):
                client.invoke_chat_tool({'text': 'Hello', 'conversation_id': 'another-room'}, conversation_id='fixed-room', request_id='fixed-call', agent_name='assistant')
            send.assert_not_called()
