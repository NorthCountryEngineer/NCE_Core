"""Header settings, personal preferences, delegation, and disclosure boundaries."""
from datetime import timedelta
import json
import secrets
import uuid
from unittest.mock import Mock

from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from core_chat.models import Message, Participant
from core_chat.services import send_message
from core_chat.tests import test_portal as portal
from core_client import CoreClient, CoreServiceError
from header_client import HeaderAdapter
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, ServicePermission
from platform_services.environments import create_environment
from platform_services.models import ApplicationPin, Deployment, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential
from platform_services.services import create_deployment, install_package, issue_workload_credential


@override_settings(DEBUG=True, ALLOWED_HOSTS=['localhost'], CSRF_TRUSTED_ORIGINS=['http://localhost:8000'])
class HeaderTests(TestCase):
    setUp = portal.ChatPortalTests.setUp
    get = portal.ChatPortalTests.get
    path = portal.ChatPortalTests.path

    @classmethod
    def setUpTestData(cls):
        portal.ChatPortalTests.setUpTestData.__func__(cls)
        cls.environment = create_environment(cls.organization, name='sandbox', namespace='header-sandbox', application_origin='https://app.example.test')
        cls.membership.access_tags.add(cls.environment.boundary_tag)
        cls.owner.memberships.get(organization=cls.organization).access_tags.add(cls.environment.boundary_tag)
        cls.room.permission_tags.add(cls.environment.boundary_tag)
        package = MarketplacePackage.objects.create(slug='header-app', name='Review Studio', kind='application', description='Test app',
            manifest={'image': 'registry.example.test/app@sha256:' + 'b' * 64, 'capabilities': ['chat.read', 'chat.send']})
        cls.app = install_package(cls.organization, package, ['chat.read', 'chat.send'])
        cls.app_permission = ServicePermission.objects.create(service=cls.app.service, codename='app-read', name='Read application')
        cls.role.permissions.add(cls.app_permission)
        cls.node = RuntimeNode.objects.create(organization=cls.organization, name='Sandbox', namespace=cls.environment.namespace,
            environment=cls.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        cls.deployment = create_deployment(cls.app, cls.node, 'https://app.example.test')
        cls.credential, cls.raw = issue_workload_credential(cls.app, 'Browser adapter', cls.environment, cls.deployment)
        cls.application_participant = Participant.objects.create(conversation=cls.room, installation=cls.app)

    def browser_json(self, route, values, *args, csrf=True, origin='http://localhost:8000'):
        self.get('header_state')
        return self.browser.post(self.path(route, *args), json.dumps(values), content_type='application/json',
            HTTP_HOST='localhost:8000', HTTP_ORIGIN=origin, **({'HTTP_X_CSRFTOKEN': self.browser.cookies['csrftoken'].value} if csrf else {}))

    def oauth(self, *, application=None, user=None, scope='openid authorization'):
        raw = secrets.token_urlsafe(48)
        token = AccessToken(application=application or self.deployment.application, user=user or self.member,
            scope=scope, expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, raw)
        token.save()
        return raw, token

    def api(self, suffix='', *, values=None, delegated=True, token=None):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (token or self.raw), 'HTTP_HOST': 'localhost:8000'}
        if delegated:
            headers['HTTP_X_CORE_USER_TOKEN'] = delegated if isinstance(delegated, str) else self.oauth()[0]
        return getattr(self.client, 'post' if values is not None else 'get')('/api/platform/v1/header/' + suffix,
            json.dumps(values) if values is not None else {}, content_type='application/json', **headers)

    def test_member_header_catalog_and_native_component(self):
        value = self.get('header_state')
        self.assertEqual(value.status_code, 200)
        self.assertEqual(value.json()['applications'][0]['id'], str(self.deployment.pk))
        self.assertEqual(value.json()['chat']['rooms'][0]['id'], str(self.room.pk))
        self.assertNotIn('Administration', [r['name'] for r in value.json()['tools']])
        self.assertIn('no-store', value['Cache-Control'])
        self.assertContains(self.get('chat'), '<core-header')
        self.assertContains(self.browser.get('/', HTTP_HOST='localhost:8000'), '<core-header')
        self.assertEqual(self.client.get('/user-tools/header.js', HTTP_HOST='localhost:8000')['Content-Type'], 'text/javascript; charset=utf-8')

    def test_pin_is_private_persistent_and_idempotent(self):
        values = {'deployment_id': str(self.deployment.pk), 'pinned': True}
        for _ in range(2):
            self.assertEqual(self.browser_json('header_pins', values).status_code, 200)
        self.assertEqual(ApplicationPin.objects.count(), 1)
        self.assertEqual(self.get('header_state').json()['pins'], [str(self.deployment.pk)])
        self.browser.force_login(self.owner)
        self.assertEqual(self.get('header_state').json()['pins'], [])
        self.browser.force_login(self.member)
        self.assertEqual(self.get('header_state').json()['pins'], [str(self.deployment.pk)])

    def test_pin_revocation_never_grants_access_and_can_be_unpinned(self):
        self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True})
        self.role.permissions.remove(self.app_permission)
        self.assertEqual(self.get('header_state').json()['applications'], [])
        self.assertEqual(self.get('header_state').json()['pins'], [])
        self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True}).status_code, 403)
        self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': False}).status_code, 200)
        self.assertFalse(ApplicationPin.objects.exists())

    def test_pin_limit_is_per_user_and_does_not_create_a_thirteenth_pin(self):
        for index in range(12):
            deployment = Deployment.objects.create(installation=self.app, name=f'Instance {index}', target='kubernetes',
                environment=self.environment, node=self.node, endpoint=f'https://app-{index}.example.test')
            ApplicationPin.objects.create(membership=self.membership, deployment=deployment)
        self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True}).status_code, 400)
        self.assertEqual(self.membership.application_pins.count(), 12)

    def test_foreign_deployments_and_arbitrary_urls_cannot_be_pinned(self):
        foreign = Organization.objects.create(name='Other', slug='header-other')
        foreign_app = install_package(foreign, self.app.package)
        deployment = Deployment.objects.create(installation=foreign_app, name='Foreign', endpoint='https://private.example.test', target='kubernetes')
        self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(deployment.pk), 'pinned': True}).status_code, 403)
        self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True, 'url': 'javascript:alert(1)'}).status_code, 400)
        self.assertFalse(ApplicationPin.objects.exists())

    def test_catalog_excludes_stopped_disabled_unsafe_and_protected_apps(self):
        for value in ['javascript:alert(1)', 'http://external.example.test', 'https://app.example.test/?token=x',
                'https://user:' + secrets.token_urlsafe(32) + '@app.example.test']:
            Deployment.objects.filter(pk=self.deployment.pk).update(endpoint=value)
            self.assertEqual(self.get('header_state').json()['applications'], [])
        Deployment.objects.filter(pk=self.deployment.pk).update(endpoint='https://app.example.test', desired_state='stopped')
        self.assertEqual(self.get('header_state').json()['applications'], [])
        Deployment.objects.filter(pk=self.deployment.pk).update(desired_state='running')
        Installation.objects.filter(pk=self.app.pk).update(enabled=False)
        self.assertEqual(self.get('header_state').json()['applications'], [])
        Installation.objects.filter(pk=self.app.pk).update(enabled=True)
        self.deployment.permission_tags.add(PermissionTag.objects.create(organization=self.organization, slug='sensitive-app', name='Sensitive', requires_mfa=True))
        self.assertEqual(self.get('header_state').json()['applications'], [])

    def test_environment_grant_is_required_and_revocable(self):
        self.membership.access_tags.clear()
        self.assertEqual(self.get('header_state').json()['applications'], [])

    def test_header_default_and_per_app_overrides(self):
        Organization.objects.filter(pk=self.organization.pk).update(unified_header_enabled=False)
        self.assertFalse(self.get('header_state').json()['enabled'])
        self.assertNotContains(self.get('chat'), '<core-header')
        self.assertFalse(self.api().json()['enabled'])
        Installation.objects.filter(pk=self.app.pk).update(header_mode='enabled')
        self.assertTrue(self.api().json()['enabled'])
        Installation.objects.filter(pk=self.app.pk).update(header_mode='disabled')
        self.assertFalse(self.api().json()['enabled'])
        self.assertEqual(self.api('pins/', values={'deployment_id': str(self.deployment.pk), 'pinned': True}).status_code, 403)

    def test_settings_require_organization_admin(self):
        self.assertEqual(self.get('header_settings').status_code, 403)
        self.browser.force_login(self.owner)
        self.assertEqual(self.get('header_settings').status_code, 200)

    def header_data(self, **changes):
        from platform_services.header_configuration import DEFAULT, WIDGETS
        values = {**DEFAULT, 'unified_header_enabled': 'on', 'identity_name': 'Test Platform',
            'show_organization': 'on', 'links-TOTAL_FORMS': '0', 'links-INITIAL_FORMS': '0',
            'links-MIN_NUM_FORMS': '0', 'links-MAX_NUM_FORMS': '8'}
        values.pop('links'); values.pop('widgets'); values.pop('shortcuts')
        for index, key in enumerate(WIDGETS):
            values['widget_' + key] = 'on' if key in DEFAULT['widgets'] else ''
            values['order_' + key] = str(index + 1)
        values.update(changes)
        return values

    def save_header(self, values, *, csrf=True, origin='http://localhost:8000'):
        self.browser.get('/', HTTP_HOST='localhost:8000')
        self.get('header_settings')
        return self.browser.post(self.path('header_settings'), values, HTTP_HOST='localhost:8000', HTTP_ORIGIN=origin,
            **({'HTTP_X_CSRFTOKEN': self.browser.cookies['csrftoken'].value} if csrf else {}))

    def configure(self, **changes):
        from copy import deepcopy
        from platform_services.header_configuration import DEFAULT, validate
        self.organization.header_configuration = validate({**deepcopy(DEFAULT), **changes}, self.organization)
        self.organization.save(update_fields=['header_configuration'])

    def test_administrator_editor_saves_brand_colors_widgets_links_and_shortcuts(self):
        self.browser.force_login(self.owner)
        values = self.header_data(preset='custom', background='#153c31', foreground='#ffffff', accent='#28775c',
            logo_url='https://cdn.example.test/organization.png', shortcuts=[str(self.deployment.pk)],
            widget_environment='on', order_environment='1', **{'links-TOTAL_FORMS': '1',
            'links-0-label': 'Support', 'links-0-url': 'https://support.example.test/help', 'links-0-new_tab': 'on', 'links-0-ORDER': '1'})
        self.assertEqual(self.save_header(values).status_code, 302)
        self.browser.force_login(self.member)
        state = self.get('header_state').json()
        self.assertEqual(state['brand'], 'Test Platform')
        self.assertEqual(state['appearance']['background'], '#153c31')
        self.assertEqual(state['appearance']['logo_url'], 'https://cdn.example.test/organization.png')
        self.assertEqual(state['appearance']['widgets'][1], 'environment')  # Catalog order breaks the position tie.
        self.assertEqual(state['shortcuts'][0]['id'], str(self.deployment.pk))
        self.assertEqual(state['links'], [{'label': 'Support', 'url': 'https://support.example.test/help', 'new_tab': True}])
        external = self.api().json()
        self.assertEqual(external['appearance'], state['appearance'])
        self.assertEqual(external['environment'], 'sandbox')
        self.assertEqual(external['shortcuts'], state['shortcuts'])
        self.assertEqual(external['links'], state['links'])
        self.assertTrue(AuditEvent.objects.filter(organization=self.organization, action='header.configuration_updated', actor=self.owner).exists())

    def test_member_cannot_change_header_or_reset_it(self):
        self.assertEqual(self.save_header(self.header_data()).status_code, 403)
        self.assertEqual(self.save_header({'action': 'reset'}).status_code, 403)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})

    def test_editor_save_and_reset_require_csrf_and_trusted_origin(self):
        self.browser.force_login(self.owner)
        for values in (self.header_data(), {'action': 'reset'}):
            for csrf, origin in ((False, 'http://localhost:8000'), (True, 'null'), (True, 'https://attacker.example.test')):
                self.assertEqual(self.save_header(values, csrf=csrf, origin=origin).status_code, 403)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})

    def test_editor_rejects_executable_urls_css_and_poor_contrast(self):
        self.browser.force_login(self.owner)
        for changes in ({'logo_url': 'javascript:alert(1)'}, {'logo_url': 'data:image/svg+xml,<svg/>'},
                {'logo_url': 'https://cdn.example.test/image?signature=opaque'},
                {'background': 'red;position:fixed'}, {'preset': 'custom', 'foreground': '#24263e'}):
            self.assertContains(self.save_header(self.header_data(**changes)), 'errorlist')
        for url in ('javascript:alert(1)', 'data:text/html,<p>unsafe</p>', '//example.test/path', 'https://name@example.test/path'):
            values = self.header_data(**{'links-TOTAL_FORMS': '1', 'links-0-label': 'Link', 'links-0-url': url})
            self.assertContains(self.save_header(values), 'errorlist')
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})

    def test_editor_rejects_foreign_apps_tags_and_excessive_links(self):
        self.browser.force_login(self.owner)
        foreign = Organization.objects.create(slug='other-header-brand', name='Other')
        app = install_package(foreign, self.app.package)
        deployment = Deployment.objects.create(installation=app, name='Foreign', endpoint='https://foreign.example.test', target='kubernetes')
        tag = PermissionTag.objects.create(organization=foreign, name='Foreign', slug='foreign')
        for changes in ({'shortcuts': [str(deployment.pk)]}, {'links-TOTAL_FORMS': '1', 'links-0-label': 'Private',
                'links-0-url': 'https://private.example.test', 'links-0-permission_tags': [tag.pk]},
                {'links-TOTAL_FORMS': '99'}):
            self.assertContains(self.save_header(self.header_data(**changes)), 'errorlist')
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})

    def test_shared_shortcuts_never_grant_access_or_expose_a_revoked_application(self):
        self.configure(shortcuts=[str(self.deployment.pk)])
        self.assertEqual(len(self.get('header_state').json()['shortcuts']), 1)
        self.role.permissions.remove(self.app_permission)
        state = self.get('header_state').json()
        self.assertEqual(state['shortcuts'], [])
        self.assertNotIn('app.example.test', json.dumps(state))
        self.assertEqual(self.api().status_code, 403)

    def test_custom_links_obey_live_user_and_application_tag_grants(self):
        tag = PermissionTag.objects.create(organization=self.organization, name='Operations', slug='header-operations', allow_workloads=True)
        self.configure(links=[{'label': 'Private operations', 'url': 'https://private.example.test', 'new_tab': False, 'permission_tags': [tag.pk]}])
        self.assertEqual(self.get('header_state').json()['links'], [])
        self.membership.access_tags.add(tag)
        self.assertEqual(len(self.get('header_state').json()['links']), 1)
        self.assertEqual(self.api().json()['links'], [])
        self.app.access_tags.add(tag)
        self.assertEqual(len(self.api().json()['links']), 1)
        self.membership.access_tags.remove(tag)
        self.assertEqual(self.api().json()['links'], [])
        tag.active = False; tag.save()
        self.assertEqual(self.get('header_state').json()['links'], [])

    def test_header_configuration_inherits_organization_mfa_and_purpose_restrictions(self):
        tag = PermissionTag.objects.create(organization=self.organization, name='Protected branding', slug='header-mfa', requires_mfa=True)
        self.organization.permission_tags.add(tag)
        self.owner.memberships.get(organization=self.organization).access_tags.add(tag)
        self.browser.force_login(self.owner)
        self.assertEqual(self.get('header_settings').status_code, 403)
        self.assertEqual(self.save_header(self.header_data()).status_code, 403)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})

    def test_reset_preserves_private_pins_brand_and_permissions(self):
        self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True})
        self.configure(preset='forest', shortcuts=[str(self.deployment.pk)], density='compact')
        self.organization.identity_name = 'Retained brand'; self.organization.save(update_fields=['identity_name'])
        self.browser.force_login(self.owner)
        self.assertEqual(self.save_header({'action': 'reset'}).status_code, 302)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.header_configuration, {})
        self.assertEqual(self.organization.identity_name, 'Retained brand')
        self.assertTrue(self.role.permissions.filter(pk=self.app_permission.pk).exists())
        self.assertEqual(self.membership.application_pins.count(), 1)

    def test_disabling_chat_widget_hides_activity_and_blocks_drawer_routes(self):
        self.configure(widgets=['apps', 'pins', 'account'])
        self.assertFalse(self.get('header_state').json()['chat']['enabled'])
        self.assertEqual(self.get('header_messages', self.room.pk).status_code, 403)
        self.assertFalse(self.api().json()['chat']['enabled'])
        self.assertEqual(self.api(f'chat/{self.room.pk}/messages/').status_code, 403)
        self.assertEqual(self.get('chat').status_code, 200)

    def test_configuration_validator_has_bounded_typed_catalog_and_safe_fallback(self):
        from copy import deepcopy
        from django.core.exceptions import ValidationError
        from platform_services.header_configuration import DEFAULT, configuration, validate
        for changes in ({'widgets': ['custom-script']}, {'widgets': ['chat', 'chat']}, {'show_organization': 'yes'},
                {'links': [{'label': 'Hi', 'url': 'https://example.test', 'new_tab': 'yes', 'permission_tags': []}]},
                {'links': [{'label': 'Hi', 'url': 'https://example.test', 'new_tab': True, 'permission_tags': [True]}]},
                {'shortcuts': ['invalid']}, {'html': '<script>unsafe</script>'}):
            with self.assertRaises(ValidationError):
                validate({**deepcopy(DEFAULT), **changes}, self.organization)
        self.organization.header_configuration = {'widgets': ['custom-script']}
        self.assertEqual(configuration(self.organization), DEFAULT)

    @override_settings(DEBUG=False)
    def test_production_disallows_insecure_images_and_links(self):
        from django.core.exceptions import ValidationError
        from platform_services.header_configuration import safe_url
        for url in ('http://localhost:8000/logo.png', 'http://example.test/logo.png'):
            with self.assertRaises(ValidationError):
                safe_url(url)
        self.assertEqual(safe_url('https://cdn.example.test/logo.png', logo=True), 'https://cdn.example.test/logo.png')

    def test_native_header_posts_require_origin_and_csrf(self):
        for csrf, origin in [(False, 'http://localhost:8000'), (True, 'null'), (True, 'https://attacker.example.test')]:
            self.assertEqual(self.browser_json('header_pins', {'deployment_id': str(self.deployment.pk), 'pinned': True}, csrf=csrf, origin=origin).status_code, 403)
        self.assertFalse(ApplicationPin.objects.exists())

    def test_inactive_membership_removes_all_user_tools(self):
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        self.assertEqual(self.get('header_state').status_code, 403)
        self.assertEqual(self.api().status_code, 401)

    def test_delegation_requires_user_scope_calling_service_and_exact_deployment(self):
        self.assertEqual(self.api(delegated=False).status_code, 403)
        self.assertEqual(self.api(delegated=self.oauth(scope='openid')[0]).status_code, 401)
        second = create_deployment(self.app, self.node, 'https://other-app.example.test')
        self.assertEqual(self.api(delegated=self.oauth(application=second.application)[0]).status_code, 401)
        self.assertEqual(self.api(delegated=self.oauth(user=self.foreign)[0]).status_code, 401)
        raw, token = self.oauth()
        token.expires = timezone.now() - timedelta(seconds=1)
        token.save()
        self.assertEqual(self.api(delegated=raw).status_code, 401)

    def test_external_app_access_is_checked_live(self):
        self.assertEqual(self.api().status_code, 200)
        self.role.permissions.remove(self.app_permission)
        self.assertEqual(self.api().status_code, 403)
        WorkloadCredential.objects.filter(pk=self.credential.pk).update(active=False)
        self.assertEqual(self.api().status_code, 401)

    def test_delegated_chat_requires_both_app_and_user_participation(self):
        value = self.api().json()
        self.assertEqual(value['chat']['rooms'][0]['id'], str(self.room.pk))
        Participant.objects.filter(pk=self.application_participant.pk).update(active=False)
        self.assertEqual(self.api().json()['chat']['rooms'], [])
        self.assertEqual(self.api(f'chat/{self.room.pk}/messages/').status_code, 403)

    def test_chat_capability_revocation_removes_badge_and_reading(self):
        Installation.objects.filter(pk=self.app.pk).update(granted_capabilities=[])
        self.assertFalse(self.api().json()['chat']['enabled'])
        self.assertEqual(self.api(f'chat/{self.room.pk}/messages/').status_code, 403)

    def test_header_send_is_attributed_idempotent_and_respects_reader(self):
        values = {'request_id': str(uuid.uuid4()), 'text': 'Header message'}
        self.assertEqual(self.api(f'chat/{self.room.pk}/send/', values=values).status_code, 201)
        self.assertEqual(self.api(f'chat/{self.room.pk}/send/', values=values).status_code, 200)
        self.assertEqual(self.api(f'chat/{self.room.pk}/send/', values={**values, 'text': 'Conflict'}).status_code, 409)
        message = Message.objects.get()
        self.assertEqual(message.actor_id, str(self.member.pk))
        self.assertEqual(message.via_installation_id, self.app.pk)
        Participant.objects.filter(pk=self.application_participant.pk).update(role='reader')
        self.assertFalse(self.api(f'chat/{self.room.pk}/messages/').json()['can_send'])
        self.assertEqual(self.api(f'chat/{self.room.pk}/send/', values={'request_id': str(uuid.uuid4()), 'text': 'Blocked'}).status_code, 403)

    def test_inbox_respects_message_tags_for_user_and_calling_app(self):
        send_message(Principal(user=self.owner), self.room, 'unread', 'Mention @' + self.member.email)
        self.assertEqual(self.get('header_state').json()['chat']['notifications'], 1)
        Message.objects.get().permission_tags.add(PermissionTag.objects.create(organization=self.organization, slug='private-row', name='Private row', allow_workloads=False))
        self.assertEqual(self.get('header_state').json()['chat']['notifications'], 0)
        self.assertEqual(self.api().json()['chat']['notifications'], 0)
        self.assertEqual(self.api(f'chat/{self.room.pk}/messages/').json()['messages'], [])

    def test_private_read_marker_is_monotonic_and_get_never_marks_read(self):
        send_message(Principal(user=self.owner), self.room, 'new', 'New activity')
        self.assertEqual(self.get('header_state').json()['chat']['notifications'], 1)
        self.get('header_messages', self.room.pk)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, 0)
        for cursor in [Message.objects.get().pk, 0]:
            self.assertEqual(self.api(f'chat/{self.room.pk}/preferences/', values={'cursor': cursor}).status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, Message.objects.get().pk)
        self.assertEqual(self.room.participants.get(user=self.owner).last_read_cursor, 0)

    def test_bad_preference_does_not_partially_update_read_marker(self):
        for mode in ['unknown', [], {}]:
            self.assertEqual(self.api(f'chat/{self.room.pk}/preferences/', values={'cursor': 12, 'notifications': mode}).status_code, 400)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.last_read_cursor, 0)
        self.assertEqual(self.api(f'chat/{self.room.pk}/messages/?after=bad').status_code, 400)


class HeaderAdapterTests(SimpleTestCase):
    def test_only_fixed_routes_and_fields_are_forwarded(self):
        core = Mock()
        adapter = HeaderAdapter(core)
        token = secrets.token_urlsafe(48)
        adapter.handle('GET', '', user_access_token=token)
        core.header_state.assert_called_once_with(user_access_token=token)
        for path in ['https://attacker.example.test/', '../integrations/', 'chat/not-a-uuid/send/']:
            with self.assertRaises(ValueError):
                adapter.handle('GET', path, user_access_token=token)
        with self.assertRaises(ValueError):
            adapter.handle('POST', f'chat/{uuid.uuid4()}/send/', user_access_token=token, payload={'text': 'Hi', 'request_id': 'one', 'user_id': 'other'})

    def test_sdk_pins_organization_and_room(self):
        core = CoreClient('https://core.example.test/api/platform/v1', secrets.token_urlsafe(48), str(uuid.uuid4()))
        core.request = Mock(return_value={'organization_id': str(uuid.uuid4())})
        with self.assertRaises(CoreServiceError):
            core.header_state(user_access_token=secrets.token_urlsafe(48))
        core.request.return_value = {'organization_id': core.organization_id, 'conversation_id': str(uuid.uuid4())}
        with self.assertRaises(CoreServiceError):
            core.header_messages(uuid.uuid4(), user_access_token=secrets.token_urlsafe(48))
