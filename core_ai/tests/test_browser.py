"""Opt-in real Chromium administrator-to-member workflow, isolated from live data."""
import os
import secrets
from contextlib import ExitStack
from unittest import skipUnless

from django.test import LiveServerTestCase, override_settings
from django.urls import reverse

from . import test_query as fixtures


@skipUnless(os.environ.get('CORE_BROWSER_EXECUTABLE') and os.environ.get('DATABASE_URL'),
    'Run ci/ai_verification.py with --browser-executable for isolated PostgreSQL browser verification.')
class BrowserQueryTests(LiveServerTestCase):
    def test_administrator_configures_source_and_member_queries_cited_accessible_data(self):
        from playwright.sync_api import sync_playwright
        fixtures.QueryTests.setUpTestData.__func__(type(self))
        self.source.active = False
        self.source.save()
        credentials = {self.admin.email: secrets.token_urlsafe(32), self.member.email: secrets.token_urlsafe(32)}
        for user in [self.admin, self.member]:
            user.set_password(credentials[user.email])
            user.save()
        retained_permissions = [str(pk) for pk in self.role.permissions.exclude(pk=self.app_permission.pk).values_list('pk', flat=True)]
        with override_settings(DEBUG=True, ISSUER=self.live_server_url, ALLOWED_HOSTS=['localhost', '127.0.0.1'],
                CSRF_TRUSTED_ORIGINS=[self.live_server_url]), sync_playwright() as playwright, ExitStack() as cleanup:
            browser = playwright.chromium.launch(executable_path=os.environ['CORE_BROWSER_EXECUTABLE'], headless=True,
                args=['--no-sandbox'])
            cleanup.callback(browser.close)
            page = browser.new_page()
            page.goto(self.live_server_url + '/accounts/login/')
            page.locator('[name=username]').fill(self.admin.email)
            page.locator('[name=password]').fill(credentials[self.admin.email])
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.goto(self.live_server_url + '/organizations/research/ai/admin/')
            page.get_by_role('link', name='Add application query source').click()
            page.locator('[name=name]').fill('Browser reports')
            page.locator('[name=installation]').select_option(label='Research Studio · 0.1.0')
            page.locator('[name=environment]').select_option(str(self.environment.pk))
            page.locator('[name=dataset]').select_option(str(self.dataset.pk))
            page.locator('[name=fields]').fill('["title", "status"]')
            page.locator('[name=filters]').fill('{"project": "shared"}')
            page.locator('[name=required_permissions]').fill('["read"]')
            page.locator('[name=allowed_roles]').select_option(str(self.role.pk))
            page.locator('[name=active]').check()
            page.get_by_role('button', name='Save', exact=True).click()
            page.wait_for_url('**/ai/admin/')
            self.assertTrue(page.get_by_role('link', name='Browser reports').is_visible())
            context = browser.new_context(viewport={'width': 390, 'height': 844})
            cleanup.callback(context.close)
            user_page = context.new_page()
            user_page.goto(self.live_server_url + '/accounts/login/')
            user_page.locator('[name=username]').fill(self.member.email)
            user_page.locator('[name=password]').fill(credentials[self.member.email])
            user_page.get_by_role('button', name='Sign in', exact=True).click()
            user_page.goto(self.live_server_url + '/organizations/research/ai/')
            self.assertFalse(user_page.get_by_role('link', name='Manage query sources').count())
            self.assertTrue(user_page.get_by_text('AI generation is not configured.', exact=False).is_visible())
            user_page.get_by_label('Maximum records to inspect').fill('1')
            user_page.get_by_role('button', name='Find evidence', exact=True).click()
            self.assertTrue(user_page.get_by_text('No matching accessible evidence in this page.').is_visible())
            user_page.get_by_role('button', name='Inspect next page', exact=True).click()
            self.assertTrue(user_page.get_by_text('Shared report', exact=True).is_visible())
            self.assertTrue(user_page.get_by_text('Citation:', exact=False).is_visible())
            self.assertNotIn('excluded content', user_page.locator('main').inner_text())
            self.assertNotIn('Private report', user_page.locator('main').inner_text())
            self.assertLessEqual(user_page.evaluate('document.documentElement.scrollWidth'), 391)
            page.goto(self.live_server_url + reverse('role', args=[self.org.slug, self.role.pk]))
            page.locator('[name=permissions]').select_option(retained_permissions)
            page.get_by_role('button', name='Save', exact=True).click()
            user_page.get_by_role('button', name='Inspect next page', exact=True).click()
            self.assertFalse(user_page.get_by_text('Shared report', exact=True).count())
            self.assertFalse(user_page.locator('article').count())
