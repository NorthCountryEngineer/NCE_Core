"""Opt-in WCAG rules plus keyboard/state tests on actual shared UI assets.

No real account, customer database, model, push service or external site is used.
Automated checks do not replace assistive-technology testing or certify compliance.
"""
import copy
import os
import re
from pathlib import Path
from unittest import skipUnless

from platform_services.header_configuration import PRESETS, contrast
from .test_mobile_ui import SharedUIBrowserFixture

ROOT = Path(__file__).resolve().parents[2]
AXE = Path(os.environ.get('CORE_AXE_PATH', ROOT / 'ci/accessibility/node_modules/axe-core/axe.min.js'))


class AccessibilityKeyboardTests(SharedUIBrowserFixture):
    def test_skip_link_and_linked_form_errors_move_keyboard_focus(self):
        self.page.goto(self.origin + '/login/')
        self.page.keyboard.press('Tab')
        self.assertEqual(self.page.evaluate('document.activeElement.textContent'), 'Skip to main content')
        self.page.keyboard.press('Enter')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'main-content')
        self.page.goto(self.origin + '/form-errors/')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'form-errors')
        self.page.get_by_role('link', name='Support email: This field is required.').click()
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'id_support_email')
        self.assertEqual(self.page.locator('[name=support_email]').get_attribute('aria-invalid'), 'true')

    def test_header_focus_survives_updates_and_returns_on_close_or_disable(self):
        self.page.goto(self.origin + '/ai/')
        self.page.get_by_role('button', name='Applications and pins').click()
        self.page.keyboard.press('F6')
        self.assertEqual(self.page.locator('core-header').evaluate('e => e.shadowRoot.activeElement.getAttribute("aria-label")'), 'Applications and pins')
        self.page.keyboard.press('Escape')
        self.assertFalse(self.page.get_by_role('dialog', name='Organization tools panel').is_visible())
        self.page.get_by_role('button', name='Applications and pins').click()
        self.page.keyboard.press('F6')
        self.page.keyboard.press('F6')
        self.assertEqual(self.page.locator('core-header').evaluate('e => e.shadowRoot.activeElement.getAttribute("aria-label")'), 'Close organization tools')
        search = self.page.get_by_role('textbox', name='Find an application')
        search.fill('Sample')
        state = copy.deepcopy(self.header_state)
        state['applications'] = [{'id': 'sample', 'name': 'Sample app', 'url': '/generated/',
            'environment': 'development', 'status': 'running', 'pinned': False}]
        self.page.locator('core-header').evaluate('(e,state) => e.renderState(state)', state)
        self.assertTrue(search.evaluate('e => e.getRootNode().activeElement === e'))
        self.assertEqual(search.input_value(), 'Sample')
        self.page.keyboard.press('Escape')
        self.assertFalse(self.page.get_by_role('dialog', name='Organization tools panel').is_visible())
        self.page.get_by_role('button', name='Applications and pins').click()
        self.page.locator('main input[name=text]').focus()
        self.assertFalse(self.page.get_by_role('dialog', name='Organization tools panel').is_visible())
        self.assertEqual(self.page.evaluate('document.activeElement.name'), 'text')
        self.page.get_by_role('button', name='Applications and pins').click()
        state['enabled'] = False
        self.page.locator('core-header').evaluate('(e,state) => e.renderState(state)', state)
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'main-content')

    def test_header_room_and_revocation_keep_focus_on_available_controls(self):
        self.page.goto(self.origin + '/ai/')
        self.page.get_by_role('button', name='Open header chat').click()
        self.page.get_by_role('button', name='Open header conversation Mobile test').click()
        message = self.page.get_by_role('textbox', name='Header chat message')
        message.wait_for(state='visible')
        message.focus()
        self.page.locator('core-header').evaluate('e => e.revoke()')
        self.assertEqual(self.page.locator('core-header').evaluate('e => e.shadowRoot.activeElement.getAttribute("aria-label")'), 'Close organization tools')
        self.assertFalse(message.count())
        self.page.keyboard.press('Escape')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'main-content')

    def test_revoked_header_shortcut_moves_focus_to_available_page_content(self):
        self.page.goto(self.origin + '/ai/')
        self.page.get_by_role('button', name='Applications and pins').wait_for()
        self.page.wait_for_function('document.querySelector("core-header").state !== undefined')
        state = copy.deepcopy(self.header_state)
        state['applications'] = [{'id': 'sample', 'name': 'Sample app', 'url': '/generated/',
            'environment': 'development', 'status': 'running', 'pinned': True}]
        state['pins'] = ['sample']
        self.page.locator('core-header').evaluate('(e,state) => e.renderState(state)', state)
        self.page.get_by_role('link', name='Sample app', exact=True).focus()
        self.page.locator('core-header').evaluate('e => e.revoke()')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'main-content')

    def test_unavailable_header_chat_keeps_focus_out_of_hidden_composer(self):
        self.page.goto(self.origin + '/ai/')
        self.page.get_by_role('button', name='Open header chat').click()
        self.page.get_by_role('button', name='Open header conversation Mobile test').click()
        message = self.page.get_by_role('textbox', name='Header chat message')
        message.wait_for(state='visible')
        self.page.wait_for_function('document.querySelector("core-header").reading === null')
        message.focus()
        self.page.route('**/tools/chat/*/messages/**', lambda route: route.fulfill(status=503, json={'error': 'unavailable'}))
        self.page.locator('core-header').evaluate('e => e.read()')
        self.assertFalse(message.is_visible())
        self.assertEqual(self.page.locator('core-header').evaluate('e => e.shadowRoot.activeElement.getAttribute("aria-label")'), 'Close organization tools')

    def test_notification_panel_and_polling_preserve_keyboard_focus(self):
        self.page.goto(self.origin + '/chat/')
        self.page.get_by_role('button', name='Notifications', exact=False).click()
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'chat-inbox-close')
        self.page.keyboard.press('Escape')
        self.assertEqual(self.page.evaluate('document.activeElement.className'), 'chat-inbox-toggle')
        room = self.page.locator('#chat-room-list a').first
        room.focus()
        self.page.evaluate('renderWorkspace({...workspacePrevious, notifications: 3, rooms: workspacePrevious.rooms.map(room => ({...room, notifications: 3}))})')
        self.assertEqual(self.page.evaluate('document.activeElement.dataset.room'), self.header_state['chat']['rooms'][0]['id'])
        self.page.evaluate('renderWorkspace({rooms: [], notifications: 0, unread: 0})')
        self.assertEqual(self.page.evaluate('document.activeElement.className'), 'chat-inbox-toggle')
        self.page.get_by_label('Message', exact=True).focus()
        self.page.evaluate('revokeChat()')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'chat-status')

    def test_tables_and_reduced_motion_remain_keyboard_usable(self):
        self.page.set_viewport_size({'width': 320, 'height': 844})
        self.page.goto(self.origin + '/environments/')
        region = self.page.get_by_role('region', name='Deployment environments')
        self.page.wait_for_function('document.querySelector(".table-scroll").hasAttribute("tabindex")')
        region.focus()
        self.page.keyboard.press('ArrowRight')
        self.page.wait_for_function('document.querySelector(".table-scroll").scrollLeft > 0')
        self.assertGreater(region.evaluate('e => e.scrollLeft'), 0)
        self.page.goto(self.origin + '/documentation-page/')
        text = self.page.get_by_role('region', name='Text block 2', exact=True)
        self.page.wait_for_function('document.querySelector("main pre").tabIndex === 0')
        text.focus()
        self.page.keyboard.press('ArrowRight')
        self.page.wait_for_function('document.querySelector("main pre").scrollLeft > 0')
        self.assertGreater(text.evaluate('e => e.scrollLeft'), 0)
        self.page.emulate_media(reduced_motion='reduce', forced_colors='active')
        self.page.goto(self.origin + '/chat/')
        self.assertEqual(self.page.locator('#chat-history').evaluate('e => getComputedStyle(e).scrollBehavior'), 'auto')
        self.page.get_by_role('button', name='Open header chat').click()
        self.assertEqual(self.page.locator('core-header .drawer').evaluate('e => e.getAnimations().length'), 0)

    def test_query_results_receive_focus_with_accessible_evidence(self):
        self.page.goto(self.origin + '/ai-results/')
        self.assertEqual(self.page.evaluate('document.activeElement.getAttribute("aria-label")'), 'Query results')
        self.assertTrue(self.page.get_by_text('Accessible evidence', exact=True).is_visible())

    def test_header_editor_focuses_added_fields_and_has_no_dead_preview_controls(self):
        self.page.goto(self.origin + '/header-settings/')
        self.assertTrue(self.page.locator('#header-preview').evaluate('e => e.inert'))
        self.page.get_by_role('button', name='Add organization link').click()
        self.assertEqual(self.page.evaluate('document.activeElement.name'), 'links-1-label')
        for field in self.page.locator('[aria-describedby]').all():
            for reference in field.get_attribute('aria-describedby').split():
                self.assertTrue(self.page.locator('[id="' + reference + '"]').count(), reference)

    def test_generated_application_retains_focus_after_record_creation(self):
        self.page.goto(self.origin + '/generated/')
        self.page.get_by_role('heading', name='Entries', exact=True).wait_for()
        self.page.route('**/api/records/entries', lambda route: route.fulfill(json={'id': 'synthetic-record'}) if route.request.method == 'POST' else route.continue_())
        self.page.get_by_label('title', exact=True).fill('A synthetic record')
        self.page.get_by_label('count', exact=True).fill('1')
        self.page.get_by_role('button', name='Create record').click()
        self.page.wait_for_function('document.activeElement.id === "entries-heading"')
        self.assertEqual(self.page.locator('#collections').get_attribute('aria-busy'), 'false')
        self.assertIn('Accessible application', self.page.title())


@skipUnless(AXE.is_file(), 'Run npm ci --prefix ci/accessibility --ignore-scripts to install pinned axe-core.')
class AccessibilityRulesTests(SharedUIBrowserFixture):
    def audit(self):
        self.page.add_script_tag(path=str(AXE.resolve()))
        violations = self.page.evaluate('''async () => {
          await new Promise(resolve => requestAnimationFrame(resolve));
          const panels = Array.from(document.querySelectorAll('core-header')).map(host => host.shadowRoot?.querySelector('.drawer')).filter(Boolean);
          const animations = [...document.getAnimations(), ...panels.flatMap(panel => panel.getAnimations())];
          await Promise.all(animations.map(animation => animation.finished.catch(() => {})));
          const result = await axe.run(document, {runOnly: {type: 'tag', values:
            ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']}});
          return result.violations.map(item => ({id: item.id, impact: item.impact,
            nodes: item.nodes.map(node => ({target: node.target, summary: node.failureSummary}))}));
        }''')
        self.assertEqual(violations, [])

    def test_shared_pages_have_no_automated_accessibility_violations(self):
        for width in [390, 1280]:
            self.page.set_viewport_size({'width': width, 'height': 900})
            for path in self.audit_pages:
                with self.subTest(width=width, page=path):
                    self.page.goto(self.origin + path)
                    if path == '/generated/':
                        self.page.get_by_role('heading', name='Entries', exact=True).wait_for()
                    self.audit()

    def test_open_header_tools_and_notifications_have_no_automated_violations(self):
        for width in [390, 1280]:
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.goto(self.origin + '/ai/')
            for button in ['Applications and pins', 'Open header chat', 'Open header conversation Mobile test']:
                with self.subTest(width=width, panel=button):
                    self.page.get_by_role('button', name=button, exact=True).click()
                    self.audit()
            self.page.goto(self.origin + '/chat/')
            self.page.get_by_role('button', name='Notifications', exact=False).click()
            self.audit()

    def test_allowed_brand_colors_retain_contrast_for_links_and_hover(self):
        themes = {**PRESETS, 'boundary-dark': ('#767676', '#ffffff', '#ffff00'),
            'boundary-light': ('#ffffff', '#767676', '#000000')}
        for width in [390, 1280]:
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.goto(self.origin + '/ai/')
            self.page.wait_for_function('document.querySelector("core-header").state !== undefined')
            for name, (background, foreground, accent) in themes.items():
                with self.subTest(width=width, theme=name):
                    state = copy.deepcopy(self.header_state)
                    state['appearance'] = {'background': background, 'foreground': foreground, 'accent': accent}
                    state['applications'] = [{'id': 'sample', 'name': 'Sample app', 'url': '/generated/',
                        'environment': 'development', 'status': 'running', 'pinned': True}]
                    state['pins'] = ['sample']
                    state['links'] = [{'label': 'Organization guide', 'url': '/documentation/', 'new_tab': False}]
                    self.page.locator('core-header').evaluate('(e,state) => e.renderState(state)', state)
                    self.page.get_by_role('button', name='Applications and pins').hover()
                    self.audit()
                    self.page.get_by_role('link', name='Sample app', exact=True).hover()
                    self.audit()
                    # The account form is slotted into the shadow header; its
                    # focus indicator must retain contrast on custom themes too.
                    self.page.get_by_role('button', name='Sign out', exact=True).focus()
                    self.page.keyboard.press('Tab')
                    self.page.keyboard.press('Shift+Tab')
                    signout = self.page.get_by_role('button', name='Sign out', exact=True)
                    self.assertTrue(signout.evaluate('e => e.matches(":focus-visible")'))
                    self.assertEqual(signout.evaluate('e => getComputedStyle(e).outlineStyle'), 'solid')
                    colors = self.page.locator('core-header').evaluate('''e => ({
                      background: getComputedStyle(e.shadowRoot.querySelector('.bar')).backgroundColor,
                      outline: getComputedStyle(e.querySelector('button')).outlineColor})''')
                    def hex_color(value):
                        return '#' + ''.join(f'{int(part):02x}' for part in re.findall(r'\d+', value)[:3])
                    self.assertGreaterEqual(contrast(hex_color(colors['background']), hex_color(colors['outline'])), 3)
