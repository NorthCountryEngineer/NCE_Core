from datetime import timedelta
import json
import secrets
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from identity.access import Principal
from identity.models import Application, AuditEvent, Membership, Organization, PermissionTag, Role, User
from platform_services.models import AgentDefinition, Installation, MarketplacePackage, WorkflowRun
from platform_services.services import install_core, install_package, issue_workload_credential
from platform_services.workflows import enqueue, process_one, snapshot_agents
from core_chat.models import Conversation, FederationConnection, Message, Participant
from core_chat.services import create_conversation, message_page, send_message


CAPABILITIES = ['chat.read', 'chat.send', 'chat.manage', 'chat.ai', 'chat.federate', 'agents.run']


class ChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name='Chat organization', slug='chat-test')
        cls.foreign = Organization.objects.create(name='Other organization', slug='chat-other')
        install_core(cls.organization)
        install_core(cls.foreign)
        cls.owner = User.objects.create_user('owner@chat.example.test')
        cls.member = User.objects.create_user('member@chat.example.test')
        cls.owner_membership = Membership.objects.create(organization=cls.organization, user=cls.owner, kind='administrator')
        cls.member_membership = Membership.objects.create(organization=cls.organization, user=cls.member)
        package = MarketplacePackage.objects.create(slug='chat-consumer', name='Chat consumer', kind='application', description='Chat tests',
            manifest={'image': 'registry.example.test/chat@sha256:' + 'a' * 64, 'capabilities': CAPABILITIES, 'dependencies': ['core-chat', 'core-agents']})
        cls.installation = install_package(cls.organization, package, CAPABILITIES)
        cls.credential, cls.token = issue_workload_credential(cls.installation, 'Chat application')
        cls.room = create_conversation(Principal(user=cls.owner), cls.organization, 'Team conversation', ai_enabled=True)
        cls.owner_participant = Participant.objects.get(conversation=cls.room, user=cls.owner)
        cls.owner_participant.allow_ai = True
        cls.owner_participant.save()
        cls.service_participant = Participant.objects.create(conversation=cls.room, installation=cls.installation, allow_ai=True)
        cls.chat_service = Installation.objects.get(organization=cls.organization, package__slug='core-chat').service

    def api(self, path, method='GET', values=None, token=None, **headers):
        return getattr(self.client, method.lower())('/api/platform/v1/chat' + path,
            data=json.dumps(values or {}) if method == 'POST' else values or {}, content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + (token or self.token), **headers)

    def send(self, **values):
        return self.api(f'/conversations/{self.room.pk}/send/', 'POST', {'request_id': 'message-1', 'text': 'Hello', **values})

    def application(self, service=None):
        return Application.objects.create(service=service or self.installation.service, name='Chat OAuth client',
            client_type='confidential', authorization_grant_type='authorization-code', algorithm='RS256',
            client_secret=secrets.token_urlsafe(32), redirect_uris='https://app.example.test/callback')

    def user_token(self, application=None, user=None, scope='openid authorization'):
        raw = secrets.token_urlsafe(32)
        token = AccessToken(application=application or self.application(), user=user or self.owner, scope=scope,
                            expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, raw)
        token.save()
        return raw, token

    def test_chat_is_vended_monitored_and_discovered_as_core_package(self):
        installation = Installation.objects.get(organization=self.organization, package__slug='core-chat')
        self.assertEqual(installation.package.kind, 'core')
        self.assertEqual(installation.deployments.get().target, 'embedded')
        from platform_services.services import integration_document
        document = integration_document(self.installation)
        self.assertIn('core-chat', document['services'])
        self.assertTrue(document['chat_url'].endswith('/api/platform/v1/chat'))

    def test_application_send_read_cursor_and_idempotency(self):
        first = self.send()
        self.assertEqual(first.status_code, 201, first.content)
        message = first.json()['message']
        self.assertEqual(message['actor_kind'], 'application')
        self.assertEqual(message['actor_id'], str(self.installation.pk))
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send(text='Different').status_code, 409)
        page = self.api(f'/conversations/{self.room.pk}/messages/').json()
        self.assertEqual(page['messages'][0]['id'], message['id'])
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', values={'after': page['next_cursor']}).json()['messages'], [])

    def test_missing_authentication_wrong_tenant_and_unknown_room_deny(self):
        path = f'/api/platform/v1/chat/conversations/{self.room.pk}/messages/'
        self.assertEqual(self.client.get(path).status_code, 401)
        foreign_room = Conversation.objects.create(organization=self.foreign, title='Private')
        self.assertEqual(self.api(f'/conversations/{foreign_room.pk}/messages/').status_code, 404)

    def test_participant_readonly_role_and_revocation_are_live(self):
        self.service_participant.role = 'reader'
        self.service_participant.save()
        self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/').status_code, 200)
        self.service_participant.active = False
        self.service_participant.save()
        self.assertEqual(self.api('/conversations/').json()['conversations'], [])
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/').status_code, 403)

    def test_credential_capability_package_and_room_revocation(self):
        self.credential.active = False
        self.credential.save()
        self.assertEqual(self.send().status_code, 401)
        self.credential.active = True
        self.credential.save()
        self.installation.granted_capabilities = ['chat.read']
        self.installation.save()
        self.assertEqual(self.send().status_code, 403)
        Installation.objects.filter(organization=self.organization, package__slug='core-chat').update(enabled=False)
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/').status_code, 403)

    def test_ai_requires_capability_room_and_participant_opt_in(self):
        result = self.send(ai=True, agent_name='support-assistant')
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.json()['message']['actor_kind'], 'ai')
        self.assertEqual(result.json()['message']['agent_name'], 'support-assistant')
        self.service_participant.allow_ai = False
        self.service_participant.save()
        self.assertEqual(self.send(ai=True, agent_name='support-assistant').status_code, 403)
        self.service_participant.allow_ai = True
        self.service_participant.save()
        self.room.ai_enabled = False
        self.room.save()
        self.assertEqual(self.send(ai=True, agent_name='support-assistant').status_code, 403)

    def test_ai_capability_and_boolean_validation_cannot_be_bypassed(self):
        self.installation.granted_capabilities.remove('chat.ai')
        self.installation.save()
        self.assertEqual(self.send(ai=True, agent_name='assistant').status_code, 403)
        self.assertEqual(self.send(ai='false', agent_name='assistant').status_code, 403)
        self.assertEqual(self.send(actor_id=str(self.owner.pk)).status_code, 400)

    def test_message_size_and_origin_spoofing_fail_without_side_effects(self):
        self.assertEqual(self.send(text='x' * 16385).status_code, 400)
        self.assertEqual(self.send(ai=True).status_code, 400)
        self.assertEqual(self.send(origin='bad').status_code, 400)
        self.assertEqual(self.send(origin={'issuer': 'https://untrusted.example', 'actor_kind': 'user', 'actor_id': 'someone'},
            ).status_code, 400)
        self.assertFalse(Message.objects.exists())

    def test_administrator_cannot_read_an_unjoined_conversation(self):
        room = Conversation.objects.create(organization=self.organization, title='Private conversation')
        with self.assertRaises(PermissionDenied):
            message_page(Principal(user=self.owner), room)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('chat_room', args=[self.organization.slug, room.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('chat_settings', args=[self.organization.slug, room.pk])).status_code, 200)

    def test_permission_tags_apply_to_rooms_participants_and_messages(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug='restricted-chat', name='Restricted chat', allow_workloads=True)
        self.room.permission_tags.add(tag)
        self.assertEqual(self.send().status_code, 403)
        self.installation.access_tags.add(tag)
        self.assertEqual(self.send().status_code, 201)
        message = Message.objects.get()
        second = PermissionTag.objects.create(organization=self.organization, slug='one-message', name='One message', allow_workloads=True)
        message.permission_tags.add(second)
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/').json()['messages'], [])
        self.installation.access_tags.add(second)
        self.assertEqual(len(self.api(f'/conversations/{self.room.pk}/messages/').json()['messages']), 1)
        self.service_participant.permission_tags.add(PermissionTag.objects.create(organization=self.organization, slug='participant', name='Participant'))
        self.assertEqual(self.send(request_id='blocked-participant').status_code, 403)

    def test_mfa_only_tags_do_not_allow_ai_or_federation_workloads(self):
        tag = PermissionTag.objects.create(organization=self.organization, slug='human-only', name='Human only', requires_mfa=True, allow_workloads=True)
        self.room.permission_tags.add(tag)
        self.installation.access_tags.add(tag)
        self.assertEqual(self.send(ai=True, agent_name='assistant').status_code, 403)
        with self.assertRaises(PermissionDenied):
            send_message(Principal(user=self.owner), self.room, 'human', 'Hello')

    def test_oauth_requires_correct_audience_scope_and_live_membership(self):
        raw, token = self.user_token(self.application(self.chat_service))
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=raw).status_code, 200)
        token.scope = 'openid'
        token.save()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=raw).status_code, 401)
        wrong, _ = self.user_token()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=wrong).status_code, 401)
        token.scope = 'authorization'
        token.save()
        self.owner_membership.active = False
        self.owner_membership.save()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=raw).status_code, 401)

    def test_delegated_user_and_application_both_require_room_access(self):
        raw, _ = self.user_token()
        response = self.api(f'/conversations/{self.room.pk}/send/', 'POST', {'request_id': 'delegated', 'text': 'User message'}, HTTP_X_CORE_USER_TOKEN=raw)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['message']['actor_kind'], 'user')
        self.assertEqual(response.json()['message']['actor_id'], str(self.owner.pk))
        self.assertEqual(response.json()['message']['via_installation_id'], str(self.installation.pk))
        self.service_participant.active = False
        self.service_participant.save()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', HTTP_X_CORE_USER_TOKEN=raw).status_code, 403)

    def test_delegation_rejects_another_application_service_token(self):
        raw, _ = self.user_token(self.application(self.chat_service))
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', HTTP_X_CORE_USER_TOKEN=raw).status_code, 401)

    def test_oauth_binding_uses_immutable_service_identity_after_customer_renaming(self):
        raw, _ = self.user_token(self.application(self.chat_service))
        self.chat_service.slug = 'customer-messaging'
        self.chat_service.name = 'Customer Messaging'
        self.chat_service.save()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=raw).status_code, 200)
        self.installation.service.slug = 'core-chat'
        self.installation.service.save()
        unrelated, _ = self.user_token()
        self.assertEqual(self.api(f'/conversations/{self.room.pk}/messages/', token=unrelated).status_code, 401)

    def test_member_needs_live_chat_service_role_and_room_participation(self):
        Participant.objects.create(conversation=self.room, user=self.member)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('chat_room', args=[self.organization.slug, self.room.pk])).status_code, 403)
        role = Role.objects.create(organization=self.organization, slug='chatter', name='Chat user')
        role.permissions.add(*self.chat_service.permissions.filter(codename__in=['chat-read', 'chat-send']))
        self.member_membership.roles.add(role)
        self.assertEqual(self.client.get(reverse('chat_room', args=[self.organization.slug, self.room.pk])).status_code, 200)
        role.permissions.clear()
        self.assertEqual(self.client.get(reverse('chat_room', args=[self.organization.slug, self.room.pk])).status_code, 403)

    def test_browser_chat_is_csrf_protected_and_escapes_untrusted_text(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse('chat_room', args=[self.organization.slug, self.room.pk])
        self.assertEqual(client.post(url, {'text': 'Hello', 'request_id': secrets.token_hex(16)}).status_code, 403)
        self.send(text='<script>alert(1)</script>')
        response = client.get(url)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        feed = client.get(reverse('chat_feed', args=[self.organization.slug, self.room.pk]))
        self.assertEqual(feed.status_code, 200)
        self.assertEqual(feed.json()['messages'][0]['text'], '<script>alert(1)</script>')
        self.owner_participant.active = False
        self.owner_participant.save()
        self.assertEqual(client.get(reverse('chat_feed', args=[self.organization.slug, self.room.pk])).status_code, 403)

    def test_peer_origin_requires_an_explicit_issuer_binding(self):
        origin = {'issuer': 'https://peer.example', 'actor_kind': 'user', 'actor_id': 'remote-user'}
        response = self.api(f'/conversations/{self.room.pk}/send/', 'POST',
            {'request_id': 'unapproved-peer', 'text': 'Hello', 'origin': origin}, HTTP_X_CORE_CHAT_FEDERATION=origin['issuer'])
        self.assertEqual(response.status_code, 403)
        origin['actor_kind'] = []
        response = self.api(f'/conversations/{self.room.pk}/send/', 'POST',
            {'request_id': 'malformed-peer', 'text': 'Hello', 'origin': origin}, HTTP_X_CORE_CHAT_FEDERATION=origin['issuer'])
        self.assertEqual(response.status_code, 400)

    def test_participant_management_rejects_cross_tenant_and_reader_owner_escalation(self):
        self.service_participant.role = 'owner'
        self.service_participant.save()
        foreign_user = User.objects.create_user('foreign@chat.example.test')
        Membership.objects.create(organization=self.foreign, user=foreign_user)
        response = self.api(f'/conversations/{self.room.pk}/participants/', 'POST', {'user_id': str(foreign_user.pk)})
        self.assertEqual(response.status_code, 400, response.content)
        self.service_participant.role = 'reader'
        self.service_participant.save()
        response = self.api(f'/conversations/{self.room.pk}/participants/', 'POST', {'user_id': str(self.member.pk), 'role': 'owner'})
        self.assertEqual(response.status_code, 403)

    def test_workflow_chat_tool_is_bounded_idempotent_and_attributed(self):
        agent = AgentDefinition.objects.create(organization=self.organization, slug='notify', name='Notify', kind='chat_send',
            configuration={'conversation_id': str(self.room.pk), 'text_field': 'reply'})
        run, _ = enqueue(self.organization, 'ai-notification', agent.name, 'agent', snapshot_agents(self.organization, [agent.slug]),
                         {'reply': 'Please review this conversation.'}, credential=self.credential)
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'succeeded', run.error)
        message = Message.objects.get()
        self.assertEqual(message.actor_kind, 'ai')
        self.assertEqual(message.agent_name, 'notify')
        self.assertEqual(run.output['chat_message_id'], str(message.public_id))
        run.status = 'queued'
        run.save()
        process_one()
        self.assertEqual(Message.objects.count(), 1)

    def test_queued_agent_permission_revocation_prevents_sending(self):
        agent = AgentDefinition.objects.create(organization=self.organization, slug='notify', name='Notify', kind='chat_send',
            configuration={'conversation_id': str(self.room.pk), 'text_field': 'reply'})
        run, _ = enqueue(self.organization, 'revoked-notification', agent.name, 'agent', snapshot_agents(self.organization, [agent.slug]),
                         {'reply': 'Do not send after revocation.'}, credential=self.credential)
        self.service_participant.allow_ai = False
        self.service_participant.save()
        process_one()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertFalse(Message.objects.exists())

    def test_chat_tool_rejects_cross_organization_conversation(self):
        room = Conversation.objects.create(organization=self.foreign, title='Other room', ai_enabled=True)
        agent = AgentDefinition(organization=self.organization, slug='wrong', name='Wrong', kind='chat_send',
            configuration={'conversation_id': str(room.pk), 'text_field': 'reply'})
        with self.assertRaises(ValidationError):
            agent.full_clean()

    def test_audit_contains_metadata_without_message_bodies(self):
        secret_text = secrets.token_urlsafe(32)
        self.assertEqual(self.send(text=secret_text).status_code, 201)
        events = list(AuditEvent.objects.filter(action='chat.sent').values())
        self.assertTrue(events)
        self.assertNotIn(secret_text, repr(events))
        self.api(f'/conversations/{self.room.pk}/messages/')
        self.assertTrue(AuditEvent.objects.filter(action='chat.read').exists())
