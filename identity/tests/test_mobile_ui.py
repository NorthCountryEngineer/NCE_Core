"""Shared-template geometry in Chromium; no database, accounts, or native builds.

Insets are simulated with the documented CSS variables. Actual iOS/Android
insets, keyboards, WebViews, authentication and store packaging need later tasks.
"""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from unittest import skipUnless
from urllib.parse import urlsplit

from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from core_ai.portal import QueryForm
from core_chat.models import Conversation
from core_chat.portal import SendForm
from identity.models import Organization, User


@skipUnless(os.environ.get('CORE_BROWSER_EXECUTABLE'),
    'Set CORE_BROWSER_EXECUTABLE to an already-installed Chromium binary.')
class MobileSharedUITests(SimpleTestCase):
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
        pages = {
            '/login/': render_to_string('registration/login.html', {
                'form': AuthenticationForm(request), 'product_name': 'Customer' + 'Brand' * 25,
            }, request=request).encode(),
            '/ai/': render_to_string('core_ai/index.html', {**common, 'form': QueryForm()}, request=request).encode(),
            '/chat/': render_to_string('core_chat/room.html', {**common, 'conversation': room,
                'workspace': {'rooms': [], 'notifications': 0}, 'form': SendForm(), 'can_send': True,
                'chat_state': {'cursor': 0, 'query': '', 'before': 1}}, request=request).encode(),
        }
        state = {'version': 1, 'organization_id': str(org.pk), 'enabled': True,
            'brand': org.brand, 'organization_name': org.name, 'applications': [], 'pins': [], 'tools': [],
            'user': {'name': 'Synthetic member'},
            'chat': {'enabled': True, 'rooms': [], 'unread': 0, 'notifications': 0}}
        pages['/header-state/'] = json.dumps(state).encode()
        feed_path = reverse('chat_feed', args=[org.slug, room.pk])
        activity_path = reverse('chat_activity', args=[org.slug])
        pages[feed_path] = json.dumps({'can_send': True, 'messages': [], 'next_cursor': 0}).encode()
        pages[activity_path] = json.dumps({'rooms': [], 'notifications': 0}).encode()
        json_paths = {'/header-state/', feed_path, activity_path}
        # Use the real browser component and shared Django templates.
        pages['/user-tools/header.js'] = (Path(__file__).resolve().parents[2] / 'platform_services/assets/header.js').read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/organizations/mobile/tools/':
                    path = '/header-state/'
                data = pages.get(path)
                self.send_response(200 if data is not None else 404)
                mime = 'text/javascript' if path.endswith('.js') else 'application/json' if path in json_paths else 'text/html'
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
