"""Chat workspace behavior and disclosure boundaries, beyond the legacy portal."""
import secrets
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from core_chat.federation import FederationUnavailable
from core_chat.models import Conversation, Message, Participant
from core_chat.services import create_conversation, send_message
from core_chat.tests import test_portal as portal_tests
from core_chat.workspace import mentioned
from identity.access import Principal
from identity.models import Membership, PermissionTag, User


@override_settings(DEBUG=True, ALLOWED_HOSTS=['localhost'], CSRF_TRUSTED_ORIGINS=['http://localhost:8000'])
class ChatWorkspaceTests(TestCase):
    # Reuse the portal's organization and explicit role grants, without inheriting
    # its tests or installing implicit permissions for these new capabilities.
    setUp = portal_tests.ChatPortalTests.setUp
    path = portal_tests.ChatPortalTests.path
    get = portal_tests.ChatPortalTests.get
    post = portal_tests.ChatPortalTests.post
    grants = portal_tests.ChatPortalTests.grants

    @classmethod
    def setUpTestData(cls):
        portal_tests.ChatPortalTests.setUpTestData.__func__(cls)
        cls.other = User.objects.create_user('another@example.test', secrets.token_urlsafe(32))
        membership = Membership.objects.create(organization=cls.organization, user=cls.other)
        membership.roles.add(cls.role)

    def send(self, text='Hello', *, user=None, reply_to=None, request_id=None):
        return send_message(Principal(user=user or self.owner), self.room, request_id or str(uuid.uuid4()), text, reply_to=reply_to)

    def state(self):
        return self.get('chat_activity').json()

    def test_direct_message_invites_exactly_one_org_member(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        result = self.post('chat_create', {'title': 'Private exchange', 'kind': 'direct', 'members': [str(self.owner.pk)]})
        self.assertEqual(result.status_code, 302)
        room = Conversation.objects.get(title='Private exchange')
        self.assertEqual(room.kind, 'direct')
        self.assertEqual(set(room.participants.values_list('user_id', flat=True)), {self.member.pk, self.owner.pk})
        self.browser.force_login(self.other)
        self.assertNotContains(self.get('chat'), 'Private exchange')
        self.assertEqual(self.get('chat_room', room.pk).status_code, 403)

    def test_group_creation_invites_multiple_people_with_topic(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        result = self.post('chat_create', {'title': 'Launch team', 'kind': 'group', 'topic': 'Release planning',
            'members': [str(self.owner.pk), str(self.other.pk)]})
        self.assertEqual(result.status_code, 302)
        room = Conversation.objects.get(title='Launch team')
        self.assertEqual(room.kind, 'group')
        self.assertEqual(room.topic, 'Release planning')
        self.assertEqual(room.participants.count(), 3)
        self.browser.force_login(self.other)
        self.assertContains(self.get('chat_room', room.pk), 'Release planning')

    def test_invalid_invites_and_conversation_sizes_create_nothing(self):
        self.grants('chat-read', 'chat-send', 'chat-manage')
        for kind, people in [('direct', []), ('direct', [self.owner.pk, self.other.pk]),
                             ('group', [self.owner.pk]), ('group', [self.owner.pk, self.foreign.pk])]:
            with self.subTest(kind=kind, people=people):
                result = self.post('chat_create', {'title': 'Invalid group', 'kind': kind, 'members': list(map(str, people))})
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.context['form'].errors)
        self.assertFalse(Conversation.objects.filter(title='Invalid group').exists())

    def test_async_send_is_server_attributed_and_idempotent(self):
        values = {'text': 'Modern send', 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 201)
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 200)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Message.objects.get().actor_id, str(self.member.pk))
        self.assertEqual(self.get('chat_feed', self.room.pk).json()['messages'][0]['sender_name'], self.member.email)
        self.assertEqual(self.post('chat_post', {**values, 'actor_id': str(self.owner.pk)}, self.room.pk).status_code, 400)

    def test_async_send_enforces_csrf_and_live_send_permission(self):
        values = {'text': 'Blocked', 'request_id': str(uuid.uuid4())}
        for origin, token in [('null', True), ('https://attacker.example.test', True), ('http://localhost:8000', False)]:
            self.assertEqual(self.post('chat_post', values, self.room.pk, origin=origin, token=token).status_code, 403)
        self.grants('chat-read')
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_reply_is_preserved_and_idempotency_covers_parent(self):
        first = self.send('First')['message']['id']
        second = self.send('Second')['message']['id']
        values = {'text': 'A reply', 'request_id': str(uuid.uuid4()), 'reply_to': first}
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 201)
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 200)
        self.assertEqual(self.post('chat_post', {**values, 'reply_to': second}, self.room.pk).status_code, 409)
        message = self.get('chat_feed', self.room.pk).json()['messages'][-1]
        self.assertEqual(message['reply_to'], first)
        self.assertEqual(message['reply_preview'], 'First')

    def test_hidden_or_other_conversation_reply_target_is_rejected(self):
        target = self.send('Restricted root')['message']['id']
        tag = PermissionTag.objects.create(organization=self.organization, slug='restricted-root', name='Restricted root', requires_mfa=True)
        Message.objects.get(public_id=target).permission_tags.add(tag)
        values = {'text': 'No access', 'request_id': str(uuid.uuid4()), 'reply_to': target}
        self.assertEqual(self.post('chat_post', values, self.room.pk).status_code, 403)
        room = create_conversation(Principal(user=self.owner), self.organization, 'Other conversation')
        foreign_message = send_message(Principal(user=self.owner), room, 'other', 'Other')['message']['id']
        self.assertEqual(self.post('chat_post', {**values, 'reply_to': foreign_message}, self.room.pk).status_code, 400)

    def test_revoking_parent_visibility_removes_reply_preview(self):
        root = self.send('Secret root')['message']['id']
        self.send('Safe reply', user=self.member, reply_to=root)
        Message.objects.get(public_id=root).permission_tags.add(PermissionTag.objects.create(
            organization=self.organization, slug='root-mfa', name='Root MFA', requires_mfa=True))
        messages = self.get('chat_feed', self.room.pk).json()['messages']
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0]['reply_to'])
        self.assertEqual(messages[0]['reply_preview'], '')
        self.assertNotContains(self.get('chat_room', self.room.pk), 'Secret root')

    def test_unread_get_does_not_acknowledge_and_own_messages_do_not_notify(self):
        self.send('One')
        self.send('My own message', user=self.member)
        self.send('Two')
        self.assertEqual(self.state()['unread'], 2)
        self.assertEqual(self.state()['notifications'], 2)
        self.get('chat_room', self.room.pk)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, 0)

    def test_read_acknowledgment_is_private_csrf_protected_and_monotonic(self):
        self.send('First')
        self.send('Second')
        latest = Message.objects.order_by('-pk').first().pk
        self.assertEqual(self.post('chat_preferences', {'cursor': latest}, self.room.pk, token=False).status_code, 403)
        self.assertEqual(self.post('chat_preferences', {'cursor': latest}, self.room.pk).status_code, 200)
        self.assertEqual(self.post('chat_preferences', {'cursor': latest - 1}, self.room.pk).status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, latest)
        self.assertEqual(self.room.participants.get(user=self.owner).last_read_cursor, 0)
        self.assertEqual(self.state()['unread'], 0)
        self.send('New')
        self.assertEqual(self.state()['unread'], 1)

    def test_mentions_only_and_mute_preserve_unread_badges(self):
        self.send('Ordinary message')
        self.send('For @' + self.member.email)
        self.assertEqual(self.post('chat_preferences', {'notifications': 'mentions'}, self.room.pk).status_code, 200)
        self.assertEqual(self.state()['notifications'], 1)
        self.assertEqual(self.state()['unread'], 2)
        self.assertEqual(self.state()['rooms'][0]['mentions'], 1)
        self.assertEqual(self.post('chat_preferences', {'notifications': 'muted'}, self.room.pk).status_code, 200)
        self.assertEqual(self.state()['notifications'], 0)
        self.assertEqual(self.state()['unread'], 2)
        self.assertEqual(self.room.participants.get(user=self.owner).notifications, 'all')

    def test_mention_match_is_exact_case_insensitive_and_not_a_substring(self):
        self.assertTrue(mentioned('Hi @MEMBER@example.test!', self.member.email))
        self.assertFalse(mentioned('Hi @member@example.test.attacker', self.member.email))
        self.assertFalse(mentioned('Hi x@member@example.test', self.member.email))
        self.assertFalse(mentioned('Hi member@example.test', self.member.email))

    def test_notification_counts_and_search_do_not_disclose_tagged_messages(self):
        target = self.send('Classified @' + self.member.email)['message']['id']
        Message.objects.get(public_id=target).permission_tags.add(PermissionTag.objects.create(
            organization=self.organization, slug='message-mfa', name='Message MFA', requires_mfa=True))
        self.assertEqual(self.state()['unread'], 0)
        self.assertEqual(self.state()['notifications'], 0)
        page = self.browser.get(self.path('chat_room', self.room.pk), {'q': 'Classified'}, HTTP_HOST='localhost:8000')
        self.assertNotContains(page, 'Classified @')
        self.assertEqual(page.context['chat_messages'], [])

    def test_revoked_participant_membership_and_service_block_notifications(self):
        self.send('Private activity')
        self.participant.active = False
        self.participant.save()
        self.assertEqual(self.state()['rooms'], [])
        self.assertEqual(self.post('chat_preferences', {'notifications': 'all'}, self.room.pk).status_code, 403)
        self.participant.active = True
        self.participant.save()
        self.membership.active = False
        self.membership.save()
        self.assertEqual(self.get('chat_activity').status_code, 403)
        self.membership.active = True
        self.membership.save()
        self.installation.enabled = False
        self.installation.save()
        self.assertEqual(self.get('chat_activity').status_code, 403)

    def test_invalid_preferences_cannot_change_or_target_other_participants(self):
        for values in [{'cursor': -1}, {'cursor': 2**64}, {'notifications': 'invalid'}]:
            self.assertEqual(self.post('chat_preferences', values, self.room.pk).status_code, 400)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, 0)
        self.assertEqual(self.participant.notifications, 'all')
        self.browser.force_login(self.other)
        self.assertEqual(self.post('chat_preferences', {'notifications': 'muted'}, self.room.pk).status_code, 403)

    def test_links_and_permalinks_are_escaped_and_access_checked(self):
        message = self.send('Visit https://example.test/report <script>unsafe()</script>')['message']['id']
        page = self.browser.get(self.path('chat_room', self.room.pk), {'message': message}, HTTP_HOST='localhost:8000')
        self.assertContains(page, 'href="https://example.test/report"')
        self.assertContains(page, '&lt;script&gt;')
        self.assertEqual(self.browser.get(self.path('chat_room', self.room.pk), {'message': 'bad-id'}, HTTP_HOST='localhost:8000').status_code, 400)
        Message.objects.get(public_id=message).permission_tags.add(PermissionTag.objects.create(
            organization=self.organization, slug='permalink-mfa', name='Permalink MFA', requires_mfa=True))
        self.assertEqual(self.browser.get(self.path('chat_room', self.room.pk), {'message': message}, HTTP_HOST='localhost:8000').status_code, 403)

    def test_search_and_older_history_cover_more_than_first_page(self):
        for index in range(105):
            self.send(f'History {index:03d}')
        page = self.get('chat_room', self.room.pk)
        self.assertEqual(len(page.context['chat_messages']), 100)
        self.assertContains(page, 'Load earlier messages')
        self.assertNotContains(page, 'History 000')
        cursor = page.context['chat_state']['before']
        previous = self.browser.get(self.path('chat_feed', self.room.pk), {'before': cursor}, HTTP_HOST='localhost:8000').json()
        self.assertEqual(len(previous['messages']), 5)
        self.assertEqual(previous['messages'][0]['text'], 'History 000')
        self.assertFalse(previous['has_more'])
        page = self.browser.get(self.path('chat_room', self.room.pk), {'q': 'History 000'}, HTTP_HOST='localhost:8000')
        self.assertEqual(len(page.context['chat_messages']), 1)
        self.assertContains(page, 'History 000')

    def test_shared_authority_outage_is_explicit_without_cached_notifications(self):
        with patch('core_chat.workspace.message_page', side_effect=FederationUnavailable):
            state = self.state()
        self.assertTrue(state['rooms'][0]['unavailable'])
        self.assertEqual(state['notifications'], 0)
        self.assertNotIn('messages', state)
