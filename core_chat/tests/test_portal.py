"""Native browser permissions, CSRF, tenant boundaries, and service lifecycle."""
import secrets
import uuid

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core_chat.models import Conversation, Message, Participant
from core_chat.services import create_conversation
from identity.access import Principal
from identity.models import Membership, Organization, PermissionTag, Role, User
from platform_services.management.commands.core_worker import heartbeat_embedded
from platform_services.models import Installation
from platform_services.services import install_core


@override_settings(DEBUG=True, ALLOWED_HOSTS=['localhost'], CSRF_TRUSTED_ORIGINS=['http://localhost:8000'])
class ChatPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(slug='portal-chat', name='Portal Chat')
        install_core(cls.organization)
        cls.owner = User.objects.create_user('owner@example.test', secrets.token_urlsafe(32))
        cls.member = User.objects.create_user('member@example.test', secrets.token_urlsafe(32))
        cls.foreign = User.objects.create_user('foreign@example.test', secrets.token_urlsafe(32))
        Membership.objects.create(organization=cls.organization, user=cls.owner, kind='administrator')
        cls.membership = Membership.objects.create(organization=cls.organization, user=cls.member)
        cls.installation = Installation.objects.get(organization=cls.organization, package__slug='core-chat')
        cls.role = Role.objects.create(organization=cls.organization, slug='chat-member', name='Chat Member')
        cls.role.permissions.set(cls.installation.service.permissions.filter(codename__in=['chat-read', 'chat-send']))
        cls.membership.roles.add(cls.role)
        cls.room = create_conversation(Principal(user=cls.owner), cls.organization, 'Shared conversation')
        cls.participant = Participant.objects.create(conversation=cls.room, user=cls.member)

    def setUp(self):
        self.browser = Client(enforce_csrf_checks=True)
        self.browser.force_login(self.member)

    def path(self, name, *args):
        return reverse(name, args=[self.organization.slug, *args])

    def get(self, name, *args):
        return self.browser.get(self.path(name, *args), HTTP_HOST='localhost:8000')

    def post(self, name, data, *args, origin='http://localhost:8000', token=True):
        self.get('chat')
        if token:
            data = {**data, 'csrfmiddlewaretoken': self.browser.cookies['csrftoken'].value}
        return self.browser.post(self.path(name, *args), data, HTTP_HOST='localhost:8000', HTTP_ORIGIN=origin)

    def grants(self, *codenames):
        self.role.permissions.set(self.installation.service.permissions.filter(codename__in=codenames))

    def test_basic_member_sees_joined_conversation_but_no_creation_control(self):
        page = self.get('chat')
        self.assertContains(page, 'Shared conversation')
        self.assertNotContains(page, 'href="' + self.path('chat_create') + '"')
        self.assertContains(page, 'administrator can grant you access to create conversations')
        self.assertEqual(self.get('chat_create').status_code, 403)
        self.assertEqual(self.post('chat_create', {'title': 'Unapproved'}).status_code, 403)
        self.assertFalse(Conversation.objects.filter(title='Unapproved').exists())

    def test_creation_requires_explicit_manage_grant_and_adds_owner(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        self.assertContains(self.get('chat'), 'Create conversation')
        result = self.post('chat_create', {'title': 'Member conversation', 'active': 'on'})
        self.assertEqual(result.status_code, 302)
        room = Conversation.objects.get(title='Member conversation')
        self.assertEqual(room.organization, self.organization)
        self.assertEqual(room.participants.get().user, self.member)
        self.assertEqual(room.participants.get().role, 'owner')
        self.assertContains(self.get('chat_room', room.pk), 'Participants and permissions')

    def test_manage_grant_does_not_manage_another_owners_conversation(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        self.assertNotContains(self.get('chat_room', self.room.pk), 'Participants and permissions')
        self.assertEqual(self.get('chat_settings', self.room.pk).status_code, 403)
        self.assertEqual(self.get('chat_edit', self.room.pk).status_code, 403)

    def test_native_member_send_and_poll_attribution(self):
        result = self.post('chat_room', {'text': 'Member message', 'request_id': str(uuid.uuid4())}, self.room.pk)
        self.assertEqual(result.status_code, 302)
        message = Message.objects.get()
        self.assertEqual(message.actor_id, str(self.member.pk))
        self.assertEqual(message.actor_kind, 'user')
        page = self.get('chat_feed', self.room.pk).json()
        self.assertEqual(page['messages'][0]['text'], 'Member message')
        self.assertTrue(page['can_send'])

    def test_live_send_permission_revocation_removes_composer(self):
        self.assertContains(self.get('chat_room', self.room.pk), 'id="chat-composer"')
        self.grants('chat-read')
        self.assertNotContains(self.get('chat_room', self.room.pk), 'id="chat-composer"')
        self.assertContains(self.get('chat_room', self.room.pk), 'read-only access')
        self.assertFalse(self.get('chat_feed', self.room.pk).json()['can_send'])
        self.assertEqual(self.post('chat_room', {'text': 'Denied', 'request_id': str(uuid.uuid4())}, self.room.pk).status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_reader_participant_cannot_send_even_with_service_grant(self):
        self.participant.role = 'reader'
        self.participant.save()
        self.assertNotContains(self.get('chat_room', self.room.pk), 'id="chat-composer"')
        self.assertFalse(self.get('chat_feed', self.room.pk).json()['can_send'])
        self.assertEqual(self.post('chat_room', {'text': 'Denied', 'request_id': str(uuid.uuid4())}, self.room.pk).status_code, 403)

    def test_membership_revocation_blocks_existing_session_and_poll(self):
        self.membership.active = False
        self.membership.save()
        for route in ['chat_room', 'chat_feed', 'chat_settings']:
            with self.subTest(route=route):
                self.assertEqual(self.get(route, self.room.pk).status_code, 403)

    def test_participant_revocation_removes_room_and_blocks_direct_access(self):
        self.participant.active = False
        self.participant.save()
        self.assertNotContains(self.get('chat'), 'Shared conversation')
        self.assertEqual(self.get('chat_room', self.room.pk).status_code, 403)
        self.assertEqual(self.get('chat_feed', self.room.pk).status_code, 403)

    def test_tagged_manage_permission_does_not_expose_creation(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        permission = self.installation.service.permissions.get(codename='chat-manage')
        permission.permission_tags.add(PermissionTag.objects.create(
            organization=self.organization, slug='restricted-creation', name='Restricted creation', requires_mfa=True,
        ))
        self.assertNotContains(self.get('chat'), 'Create conversation')
        self.assertEqual(self.get('chat_create').status_code, 403)

    def test_tagged_messages_and_unsafe_html_remain_protected(self):
        text = '<script>window.chatInjected = true</script>'
        self.post('chat_room', {'text': text, 'request_id': str(uuid.uuid4())}, self.room.pk)
        page = self.get('chat_room', self.room.pk)
        self.assertContains(page, '&lt;script&gt;')
        self.assertNotContains(page, text)
        message = Message.objects.get()
        message.permission_tags.add(PermissionTag.objects.create(
            organization=self.organization, slug='private-message', name='Private message', requires_mfa=True,
        ))
        self.assertNotContains(self.get('chat_room', self.room.pk), '&lt;script&gt;')
        self.assertEqual(self.get('chat_feed', self.room.pk).json()['messages'], [])

    def test_native_forms_reject_missing_csrf_null_and_foreign_origins(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        for origin, token in [('null', True), ('https://attacker.example.test', True), ('http://localhost:8000', False)]:
            with self.subTest(origin=origin, token=token):
                self.assertEqual(self.post('chat_create', {'title': 'Denied'}, origin=origin, token=token).status_code, 403)
                self.assertEqual(self.post('chat_room', {'text': 'Denied', 'request_id': str(uuid.uuid4())}, self.room.pk,
                    origin=origin, token=token).status_code, 403)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Conversation.objects.filter(title='Denied').exists())

    def test_member_cannot_administer_platform_or_self_escalate(self):
        for name in ['marketplace', 'platform_dashboard', 'role_add', 'invite', 'chat_admin']:
            with self.subTest(name=name):
                self.assertEqual(self.get(name).status_code, 403)

    def test_cross_tenant_participant_submission_cannot_add_foreign_user(self):
        self.browser.force_login(self.owner)
        result = self.post('chat_settings', {'user': str(self.foreign.pk), 'role': 'member', 'active': 'on'}, self.room.pk)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.context['form'].errors)
        self.assertFalse(self.room.participants.filter(user=self.foreign).exists())

    def test_disabled_installation_and_service_deny_ui_and_are_monitored(self):
        heartbeat_embedded()
        deployment = self.installation.deployments.get()
        self.assertEqual(deployment.status, 'running')
        self.assertTrue(deployment.healthy)
        self.installation.enabled = False
        self.installation.save()
        self.assertEqual(self.get('chat').status_code, 403)
        heartbeat_embedded()
        deployment.refresh_from_db()
        self.assertEqual(deployment.status, 'stopped')
        self.assertFalse(deployment.healthy)
        self.installation.enabled = True
        self.installation.save()
        self.installation.service.active = False
        self.installation.service.save()
        self.assertEqual(self.get('chat').status_code, 403)

    def test_foreign_and_anonymous_accounts_cannot_enter_organization_chat(self):
        self.browser.force_login(self.foreign)
        self.assertEqual(self.get('chat').status_code, 403)
        self.browser.logout()
        self.assertEqual(self.get('chat').status_code, 302)

    def test_empty_message_reports_validation_error_without_sending(self):
        result = self.post('chat_room', {'text': '', 'request_id': str(uuid.uuid4())}, self.room.pk)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.context['form'].errors)
        self.assertFalse(Message.objects.exists())
