"""Shared-template geometry in Chromium; no database, accounts, or native builds.

Insets are simulated with the documented CSS variables. Actual iOS/Android
insets, keyboards, WebViews, authentication and store packaging need later tasks.
"""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest import skipUnless
from urllib.parse import urlsplit

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils.safestring import mark_safe

from core_ai.portal import QueryForm
from core_chat.models import Conversation
from core_chat.portal import SendForm
from identity.models import Organization, User
from identity.forms import CoreAuthenticationForm
from platform_services.forms import DeploymentConfigurationForm, MetadataForm
from platform_services.header_forms import HeaderForm, LinkForm, LinkFormSet
from platform_services.header_configuration import DEFAULT, PRESETS, WIDGETS
from platform_services.models import Deployment, MarketplacePackage
from platform_services.documentation import safe_html


@skipUnless(os.environ.get('CORE_BROWSER_EXECUTABLE'),
    'Set CORE_BROWSER_EXECUTABLE to an already-installed Chromium binary.')
class SharedUIBrowserFixture(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        org = Organization(slug='mobile', name='Mobile Test', identity_name='Customer Core')
        room = Conversation(organization=org, kind='channel', title='Mobile test', topic='Shared UI')
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        request.session = {}
        common = {'organization': org, 'header_organization': org,
            'user': User(email='synthetic@example.test', first_name='Synthetic', last_name='member')}
        room_state = {'id': str(room.pk), 'title': room.title, 'kind': room.kind, 'url': '/chat/',
            'unread': 2, 'notifications': 2, 'mentions': 1, 'notification_mode': 'all'}
        workspace = {'rooms': [room_state], 'notifications': 2, 'unread': 2}
        message = {'id': '11111111-1111-4111-8111-111111111111', 'actor_kind': 'human',
            'sender_name': 'Synthetic member', 'agent_name': '', 'text': 'A shared message',
            'created': '2026-10-10T12:00:00Z', 'cursor': 1, 'reply_to': None}
        pages = {
            '/login/': render_to_string('registration/login.html', {
                'form': CoreAuthenticationForm(request), 'product_name': 'Customer' + 'Brand' * 25,
            }, request=request).encode(),
            '/ai/': render_to_string('core_ai/index.html', {**common, 'form': QueryForm()}, request=request).encode(),
            '/chat/': render_to_string('core_chat/room.html', {**common, 'conversation': room,
                'workspace': workspace, 'chat_messages': [message], 'form': SendForm(), 'can_send': True,
                'chat_state': {'cursor': 0, 'query': '', 'before': 1}}, request=request).encode(),
        }
        cls.audit_pages = ['/login/', '/ai/', '/chat/']
        fixture_form = MetadataForm()
        extra_pages = {
            '/403/': ('403.html', {}),
            '/403-csrf/': ('403_csrf.html', {}),
            '/404/': ('404.html', {}),
            '/500/': ('500.html', {}),
            '/home/': ('identity/home.html', {'organizations': [org]}),
            '/admin/': ('platform_services/dashboard.html', {}),
            '/members/': ('identity/organization.html', {}),
            '/marketplace/': ('platform_services/marketplace.html', {'packages': [MarketplacePackage(slug='example', name='Example application', version='1.0', description='A free test application')]}),
            '/environments/': ('platform_services/environments.html', {}),
            '/modernize/': ('platform_services/modernize.html', {'form': fixture_form}),
            '/documentation/': ('platform_services/documentation.html', {}),
            '/documentation-page/': ('platform_services/documentation_page.html', {
                'document': {'title': 'Synthetic deployment guide', 'summary': 'Local accessibility fixture',
                    'revision': 1, 'mode': 'extended', 'citation': 'synthetic-guide', 'digest': 'synthetic-digest'},
                'exports': {'markdown': 'format=markdown', 'json': 'format=json', 'jsonl': 'format=jsonl'},
                'html': mark_safe(safe_html('## Runtime output\n\n```text\n' + 'synthetic-output-' * 60 + '\n```')),
            }),
            '/ai-results/': ('core_ai/index.html', {'form': QueryForm(), 'result': {
                'evidence': [{'source': 'Synthetic source', 'application': 'Synthetic app', 'environment': 'sandbox',
                    'synthetic': True, 'data': {'title': 'Accessible evidence'}, 'citation_id': 'synthetic-evidence',
                    'authority_organization_id': str(org.pk)}], 'usage': {'inspected_rows': 1, 'tokens': 0},
                'sources': [{'source_id': 'synthetic-source', 'status': 'ok'}],
            }}),
            '/fabric/': ('core_fabric/index.html', {}),
            '/ai-admin/': ('core_ai/admin.html', {}),
            '/development/': ('platform_services/development_index.html', {'form': fixture_form}),
            '/onboarding/': ('platform_services/onboarding.html', {'form': DeploymentConfigurationForm(), 'title': 'Configure deployment', 'state': SimpleNamespace(step=3)}),
            '/form-errors/': ('identity/form.html', {'form': MetadataForm(data={}), 'title': 'Organization metadata'}),
        }
        for path, (template, context) in extra_pages.items():
            pages[path] = render_to_string(template, {**common, **context}, request=request).encode()
            cls.audit_pages.append(path)
        state = {'version': 1, 'organization_id': str(org.pk), 'enabled': True,
            'brand': org.brand, 'organization_name': org.name, 'applications': [], 'pins': [], 'tools': [],
            'user': {'name': 'Synthetic member'},
            'chat': {**workspace, 'enabled': True, 'url': '/chat/'}}
        header_form = HeaderForm(organization=org)
        header_form.fields['shortcuts'].queryset = Deployment.objects.none()
        class EmptyLinkForm(LinkForm):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.fields['permission_tags'].queryset = self.fields['permission_tags'].queryset.none()
        links = LinkFormSet(prefix='links', form_kwargs={'organization': org})
        links.form = EmptyLinkForm
        pages['/header-settings/'] = render_to_string('platform_services/header_settings.html', {**common,
            'form': header_form, 'links': links, 'widgets': [(key, header_form['widget_' + key], header_form['order_' + key]) for key in WIDGETS],
            'preview_state': state, 'header_presets': PRESETS, 'header_defaults': DEFAULT}, request=request).encode()
        cls.audit_pages.append('/header-settings/')
        cls.header_state = state
        pages['/header-state/'] = json.dumps(state).encode()
        feed_path = reverse('chat_feed', args=[org.slug, room.pk])
        activity_path = reverse('chat_activity', args=[org.slug])
        pages[feed_path] = json.dumps({'can_send': True, 'messages': [], 'next_cursor': 0}).encode()
        pages[activity_path] = json.dumps(workspace).encode()
        header_feed = f'/organizations/mobile/tools/chat/{room.pk}/messages/'
        pages[header_feed] = json.dumps({'organization_id': str(org.pk), 'conversation_id': str(room.pk),
            'can_send': True, 'messages': [message], 'next_cursor': 0}).encode()
        json_paths = {'/header-state/', feed_path, activity_path, header_feed}
        # Use the real browser component and shared Django templates.
        pages['/user-tools/header.js'] = (Path(__file__).resolve().parents[2] / 'platform_services/assets/header.js').read_bytes()
        pages['/user-tools/header-editor.js'] = (Path(__file__).resolve().parents[2] / 'platform_services/assets/header-editor.js').read_bytes()
        root = Path(__file__).resolve().parents[2]
        factory = root / 'platform_services/app_factory_templates'
        pages['/generated/'] = (factory / 'index.html').read_bytes()
        pages['/ui.js'] = (factory / 'ui.js').read_bytes()
        pages['/header.js'] = pages['/user-tools/header.js']
        pages['/ui.css'] = b'\n'.join(path.read_bytes() for path in [factory / 'ui.css',
            root / 'templates/identity/mobile-foundation.css.html', root / 'templates/identity/accessibility.css.html'])
        pages['/api/spec'] = json.dumps({'name': 'Accessible application', 'organization_id': str(org.pk),
            'csrf': secrets.token_urlsafe(32), 'resources': [{'slug': 'entries', 'name': 'Entries',
            'fields': {'title': 'string', 'count': 'integer', 'approved': 'boolean'}}]}).encode()
        pages['/api/records/entries'] = json.dumps({'records': []}).encode()
        json_paths.update({'/api/spec', '/api/records/entries'})
        cls.audit_pages.append('/generated/')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in {'/organizations/mobile/tools/', '/core-header/'}:
                    path = '/header-state/'
                data = pages.get(path)
                self.send_response(200 if data is not None else 404)
                mime = 'text/javascript' if path.endswith('.js') else 'text/css' if path.endswith('.css') else 'application/json' if path in json_paths else 'text/html'
                self.send_header('Content-Type', mime + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data or b'Unknown fixture path')

        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        thread = Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        cls.addClassCleanup(thread.join, 2)
        cls.addClassCleanup(cls.server.shutdown)
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    def setUp(self):
        from playwright.sync_api import sync_playwright
        cleanup = self.enterContext(ExitStack())
        playwright = cleanup.enter_context(sync_playwright())
        browser = playwright.chromium.launch(executable_path=os.environ['CORE_BROWSER_EXECUTABLE'],
            headless=True, args=['--no-sandbox'])
        cleanup.callback(browser.close)
        self.page = browser.new_page(viewport={'width': 390, 'height': 844})
        self.page.set_default_timeout(5000)
        # The fixture never accesses external services.
        self.page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.origin + '/') else route.abort())


class MobileSharedUITests(SharedUIBrowserFixture):
    def test_shared_login_and_ai_forms_fit_phone_and_desktop(self):
        for width in [320, 390, 768, 1280]:
            for path in ['/login/', '/ai/']:
                with self.subTest(width=width, page=path):
                    self.page.set_viewport_size({'width': width, 'height': 844})
                    self.page.goto(self.origin + path)
                    self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
                    for field in self.page.locator('main input:not([type=hidden]):not([type=checkbox])').all():
                        if width <= 700:
                            self.assertGreaterEqual(field.evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), 16)
                        box = field.bounding_box()
                        self.assertGreaterEqual(box['x'], 0)
                        self.assertLessEqual(box['x'] + box['width'], width + 1)
                    if width <= 700:
                        self.assertGreaterEqual(self.page.locator('main button').first.bounding_box()['height'], 44)
                    viewport = self.page.locator('meta[name=viewport]').get_attribute('content')
                    self.assertIn('viewport-fit=cover', viewport)
                    self.assertNotIn('user-scalable=no', viewport)
                    self.assertNotIn('maximum-scale', viewport)

    def test_header_drawer_preserves_simulated_screen_insets(self):
        self.page.goto(self.origin + '/ai/')
        self.page.add_style_tag(content=':root {--core-safe-area-top:24px;--core-safe-area-right:18px;--core-safe-area-bottom:34px;--core-safe-area-left:12px;}')
        apps = self.page.get_by_role('button', name='Applications and pins')
        apps.click()
        self.assertEqual(self.page.locator('core-header .bar').evaluate('e => getComputedStyle(e).paddingTop'), '8px')
        self.assertEqual(self.page.locator('core-header .bar').bounding_box()['y'], 24)
        search = self.page.get_by_role('textbox', name='Find an application')
        self.assertGreaterEqual(search.evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), 16)
        for height in [844, 500]:
            with self.subTest(height=height):
                self.page.set_viewport_size({'width': 390, 'height': height})
                drawer = self.page.get_by_role('dialog', name='Organization tools panel').bounding_box()
                self.assertGreaterEqual(drawer['x'], 12)
                self.assertLessEqual(drawer['x'] + drawer['width'], 390 - 18)
                self.assertGreaterEqual(drawer['y'], 24)
                self.assertLessEqual(drawer['y'] + drawer['height'], height - 34)
                self.assertGreater(drawer['height'], 100)
        self.page.get_by_role('button', name='Close organization tools').click()
        self.assertFalse(self.page.get_by_role('dialog', name='Organization tools panel').is_visible())

    def test_chat_controls_keep_readable_size_after_page_specific_styles(self):
        self.page.goto(self.origin + '/chat/')
        for name in ['Search messages', 'Message', 'Mention a person', 'Conversation notifications']:
            field = self.page.get_by_label(name, exact=True)
            with self.subTest(control=name):
                self.assertGreaterEqual(field.evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), 16)
        self.assertGreaterEqual(self.page.get_by_role('button', name='Send message').bounding_box()['height'], 44)
