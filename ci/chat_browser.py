"""Real administrator/member UI checks against a disposable Chat deployment."""
import secrets
from pathlib import Path
from urllib.parse import urljoin, urlparse
import uuid

from playwright.sync_api import expect, sync_playwright


class ChatBrowserChecks:
    def __init__(self, instance, engine, executable=None):
        self.instance, self.engine, self.executable = instance, engine, executable
        self.origin = instance['origin']
        self.slug = engine + '-sandbox'
        self.organization = '/organizations/' + self.slug + '/'
        self.email = engine + '-user@example.test'
        self.admin_email = engine + '-operator@example.test'
        self.user_password = secrets.token_urlsafe(32)
        self.checks = []
        self.errors = []

    def __enter__(self):
        self.playwright = sync_playwright().start()
        options = {'executable_path': self.executable} if self.engine == 'chromium' and self.executable else {}
        self.browser = getattr(self.playwright, self.engine).launch(headless=True, **options)
        self.admin_context = self.browser.new_context()
        self.member_context = self.browser.new_context()
        self.admin = self.admin_context.new_page()
        self.member = self.member_context.new_page()
        for page in [self.admin, self.member]:
            page.on('pageerror', lambda error: self.errors.append(type(error).__name__))
            page.set_default_timeout(10000)
        return self

    def __exit__(self, *args):
        self.browser.close()
        self.playwright.stop()

    def check(self, name):
        if self.errors:
            raise AssertionError('Unexpected JavaScript page error.')
        self.checks.append(name)
        print(f'Chat browser {self.engine}: {name}', flush=True)

    def open(self, page, path, status=200):
        response = page.goto(self.origin + path)
        if response.status != status:
            raise AssertionError(f'Unexpected HTTP status; expected {status}.')
        if status == 200:
            if response.headers.get('referrer-policy') != 'same-origin':
                raise AssertionError('Browser form referrer policy changed.')

    def submit(self, page, button='Save', status=302):
        with page.expect_navigation(wait_until='load'), page.expect_response(lambda r: r.request.method == 'POST') as posted:
            page.get_by_role('button', name=button, exact=True).click()
        response = posted.value
        if response.status != status:
            raise AssertionError(f'Form returned HTTP {response.status}; expected {status}.')
        if response.request.headers.get('origin') != self.origin:
            raise AssertionError('Native browser form did not send the trusted origin.')
        page.wait_for_load_state('load')

    def login(self, page, email, password):
        self.open(page, '/accounts/login/')
        page.locator('[name=username]').fill(email)
        page.locator('[name=password]').fill(password)
        self.submit(page, 'Sign in')

    def permissions(self, *names):
        self.open(self.admin, self.role_path)
        self.admin.locator('[name=permissions]').select_option(label=['core-chat: ' + name for name in names])
        self.submit(self.admin)
        self.member.bring_to_front()

    def membership(self, active=True):
        self.open(self.admin, self.membership_path)
        self.admin.locator('[name=kind]').select_option('member')
        self.admin.locator('[name=active]').set_checked(active)
        self.admin.locator('[name=roles]').select_option(label='Chat User')
        self.submit(self.admin)
        self.member.bring_to_front()

    def installation(self, enabled=True):
        self.open(self.admin, self.installation_path)
        self.admin.locator('[name=enabled]').set_checked(enabled)
        self.submit(self.admin)
        self.member.bring_to_front()

    def participant(self, role='member', active=True):
        self.open(self.admin, self.participants_path)
        self.admin.get_by_role('link', name=self.email, exact=True).click()
        self.admin.locator('[name=role]').select_option(role)
        self.admin.locator('[name=active]').set_checked(active)
        self.submit(self.admin, 'Save permissions')
        self.member.bring_to_front()

    def send(self, page, text):
        page.locator('#chat-composer [name=text]').fill(text)
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/post/')) as posted:
            page.get_by_role('button', name='Send message', exact=True).click()
        if posted.value.status not in {200, 201}:
            raise AssertionError('Asynchronous message send failed.')
        if posted.value.request.all_headers().get('origin') != self.origin:
            raise AssertionError('Asynchronous send did not use the trusted origin.')
        expect(page.locator('#chat-history .chat-message-text').filter(has_text=text)).to_have_count(1)
        expect(page.locator('#chat-composer [name=text]')).to_have_value('')

    def forged_send(self, page, origin=None, token=True):
        data = {'text': 'Forbidden browser probe', 'request_id': str(uuid.uuid4())}
        if token:
            data['csrfmiddlewaretoken'] = page.locator('[name=csrfmiddlewaretoken]').first.input_value()
        response = page.request.post(self.origin + self.shared_path, form=data,
                                     headers={'Origin': self.origin if origin is None else origin})
        if response.status != 403:
            raise AssertionError('Unauthorized or CSRF-invalid send was accepted.')

    def run(self):
        self.login(self.admin, self.admin_email, self.instance['password'])
        self.check('native login retains CSRF origin through redirect')
        self.open(self.admin, '/onboarding/')
        self.admin.locator('[name=name]').fill(self.engine.title() + ' Sandbox')
        self.admin.locator('[name=slug]').fill(self.slug)
        self.submit(self.admin, 'Continue')
        for name, value in {'legal_name': 'Deployment Test', 'country': 'US', 'support_email': 'support@example.test',
                            'billing_email': 'billing@example.test'}.items():
            self.admin.locator(f'[name={name}]').fill(value)
        self.admin.locator('[name=provider]').select_option('placeholder')
        self.submit(self.admin, 'Continue')
        self.admin.locator('[name=environment]').select_option('sandbox')
        self.admin.locator('[name=namespace]').fill(self.slug)
        self.admin.locator('[name=application_origin]').fill(self.origin)
        self.submit(self.admin, 'Continue')
        self.submit(self.admin, 'Continue')
        self.admin.locator('[name=confirm]').check()
        self.submit(self.admin, 'Activate workspace')
        expect(self.admin).to_have_url(self.origin + self.organization + 'platform/')
        self.check('all five onboarding steps activate an organization without Kubernetes or payment')

        self.open(self.admin, self.organization + 'marketplace/')
        expect(self.admin.get_by_role('heading', name='Core Chat', exact=True)).to_be_visible()
        self.open(self.admin, self.organization + 'platform/')
        self.installation_path = urlparse(self.admin.locator('p').filter(has=self.admin.locator('strong', has_text='Core Chat'))
            .get_by_role('link', name='Set service access').get_attribute('href')).path
        self.installation(False)
        self.open(self.admin, self.organization + 'chat/', 403)
        self.installation(True)
        self.open(self.admin, self.organization + 'chat/')
        self.check('administrator marketplace activation and live disable/enable')

        self.open(self.admin, self.organization + 'roles/new/')
        self.admin.locator('[name=name]').fill('Chat User')
        self.admin.locator('[name=slug]').fill('chat-user')
        self.admin.locator('[name=permissions]').select_option(label=['core-chat: chat-read', 'core-chat: chat-send'])
        self.submit(self.admin)
        self.role_path = self.admin.get_by_role('link', name='Chat User', exact=True).get_attribute('href')
        self.open(self.admin, self.organization + 'invite/')
        self.admin.locator('[name=email]').fill(self.email)
        self.admin.locator('[name=kind]').select_option('member')
        self.submit(self.admin, status=200)
        invitation = self.admin.locator('code').inner_text()
        self.open(self.member, urlparse(invitation).path)
        self.member.locator('[name=password1]').fill(self.user_password)
        self.member.locator('[name=password2]').fill(self.user_password)
        self.submit(self.member, 'Accept invitation')
        self.open(self.member, self.organization + 'chat/', 403)
        self.check('invited member chooses its own password and receives no implicit Chat permission')
        self.open(self.admin, self.organization)
        self.membership_path = self.admin.get_by_role('link', name=self.email, exact=True).get_attribute('href')
        self.membership()
        self.open(self.member, self.organization + 'chat/')
        expect(self.member.get_by_role('link', name='Create conversation')).to_have_count(0)
        expect(self.member.get_by_text('An organization administrator can grant', exact=False)).to_be_visible()
        self.open(self.member, self.organization + 'chat/new/', 403)
        self.check('read/send member has no misleading creation link and direct creation remains denied')

        self.open(self.admin, self.organization + 'chat/new/')
        self.admin.locator('[name=title]').fill('Laptop Test')
        self.submit(self.admin)
        self.shared_path = urlparse(self.admin.url).path
        self.admin.get_by_role('link', name='Participants and permissions').click()
        self.participants_path = urlparse(self.admin.url).path
        self.admin.locator('[name=user]').select_option(label=self.email)
        self.admin.locator('[name=role]').select_option('member')
        self.submit(self.admin, 'Save permissions')
        self.open(self.member, self.organization + 'chat/')
        self.member.get_by_role('link', name='Laptop Test', exact=True).click()
        self.send(self.member, 'Message from test user')
        self.open(self.admin, self.shared_path)
        expect(self.admin.get_by_text('Message from test user', exact=True)).to_be_visible()
        self.check('administrator shares conversation and member sends under its own account')
        self.member.locator('#chat-composer [name=text]').fill('Draft must survive polling')
        self.send(self.admin, 'Administrator reply')
        expect(self.member.get_by_text('Administrator reply', exact=True)).to_be_visible()
        expect(self.member.locator('#chat-composer [name=text]')).to_have_value('Draft must survive polling')
        self.check('cross-session live polling preserves unsent drafts')

        self.send(self.admin, '<script>window.chatInjected = true</script>')
        expect(self.member.get_by_text('<script>window.chatInjected = true</script>', exact=True)).to_be_visible()
        if self.member.evaluate('Boolean(window.chatInjected)'):
            raise AssertionError('Polled message HTML executed.')
        self.open(self.member, self.shared_path)
        if self.member.evaluate('Boolean(window.chatInjected)'):
            raise AssertionError('Server-rendered message HTML executed.')
        self.check('server and polled messages escape unsafe HTML')

        request_id = self.member.locator('#chat-composer [name=request_id]').input_value()
        csrf = self.member.locator('#chat-composer [name=csrfmiddlewaretoken]').input_value()
        data = {'request_id': request_id, 'csrfmiddlewaretoken': csrf, 'text': 'One logical message'}
        for _ in range(2):
            if self.member.request.post(self.origin + self.shared_path, form=data,
                                        headers={'Origin': self.origin}, max_redirects=0).status != 302:
                raise AssertionError('Idempotent native send failed.')
        self.open(self.member, self.shared_path)
        expect(self.member.get_by_text('One logical message', exact=True)).to_have_count(1)
        self.check('duplicate native send persists exactly one message')
        for origin, token in [('null', True), ('https://attacker.example.test', True), (None, False)]:
            self.forged_send(self.member, origin, token)
        self.check('null origin, foreign origin and missing CSRF token cannot send')

        self.permissions('chat-read')
        expect(self.member.locator('#chat-composer')).to_be_hidden()
        expect(self.member.locator('#chat-read-only')).to_be_visible()
        self.forged_send(self.member)
        self.open(self.member, self.shared_path)
        expect(self.member.locator('#chat-composer')).to_have_count(0)
        self.check('live send permission revocation hides composer and blocks forged writes')
        self.permissions('chat-read', 'chat-send')
        self.open(self.member, self.shared_path)
        self.participant('reader')
        expect(self.member.locator('#chat-composer')).to_be_hidden()
        self.forged_send(self.member)
        self.participant('member')
        self.open(self.member, self.shared_path)
        self.check('reader participant remains read-only despite service send grant')

        self.permissions('chat-read', 'chat-send', 'chat-manage')
        self.open(self.member, self.organization + 'chat/')
        self.member.get_by_role('link', name='Create conversation').click()
        self.member.locator('[name=title]').fill('Member-owned conversation')
        self.submit(self.member)
        expect(self.member.get_by_role('link', name='Participants and permissions')).to_be_visible()
        self.send(self.member, 'Owner message survives restart')
        self.member.get_by_role('link', name='Participants and permissions').click()
        self.member.locator('[name=user]').select_option(label=self.admin_email)
        self.submit(self.member, 'Save permissions')
        self.open(self.member, self.shared_path)
        expect(self.member.get_by_role('link', name='Participants and permissions')).to_have_count(0)
        self.open(self.member, self.participants_path, 403)
        self.check('explicit manage grant permits creation and owned-room controls, not another owners controls')
        for path in ['platform/', 'marketplace/', 'roles/new/', 'invite/', 'chat/admin/']:
            self.open(self.member, self.organization + path, 403)
        self.check('conversation manager cannot administer platform, marketplace, roles, invitations or federation')

        self.modern_workspace()

        self.open(self.member, self.shared_path)
        self.participant(active=False)
        expect(self.member.locator('#chat-status')).to_have_text('Conversation access has been revoked.')
        expect(self.member.locator('#chat-history section')).to_have_count(0)
        expect(self.member.locator('#chat-composer')).to_be_hidden()
        self.participant(active=True)
        self.open(self.member, self.shared_path)
        self.check('participant revocation clears displayed history and disables composer on poll')
        self.membership(False)
        expect(self.member.locator('#chat-status')).to_have_text('Conversation access has been revoked.')
        self.open(self.member, self.organization + 'chat/', 403)
        self.membership(True)
        self.open(self.member, self.shared_path)
        self.check('membership revocation takes effect within an existing browser session')
        self.installation(False)
        expect(self.member.locator('#chat-status')).to_have_text('Conversation access has been revoked.')
        self.installation(True)
        self.open(self.member, self.shared_path)
        self.check('service disable revokes existing conversation UI and re-enable restores it')
        self.submit(self.member, 'Sign out')
        self.login(self.member, self.email, self.user_password)
        self.open(self.member, self.shared_path)
        expect(self.member.get_by_text('Message from test user', exact=True)).to_be_visible()
        self.check('native logout/login preserves account permissions and durable history')
        return self.checks

    def modern_workspace(self):
        self.open(self.member, self.organization + 'chat/new/')
        self.member.locator('[name=title]').fill('Direct exchange')
        self.member.locator('[name=kind]').select_option('direct')
        self.member.locator('[name=members]').select_option(label=self.admin_email)
        self.submit(self.member)
        direct_path = urlparse(self.member.url).path
        expect(self.member.get_by_text('Direct message', exact=True)).to_be_visible()
        self.send(self.member, 'A private direct message')
        self.open(self.admin, direct_path)
        expect(self.admin.get_by_text('A private direct message', exact=True)).to_be_visible()
        self.check('direct conversation creation invites the selected organization member')

        third_email = self.engine + '-group@example.test'
        third_password = secrets.token_urlsafe(32)
        self.open(self.admin, self.organization + 'invite/')
        self.admin.locator('[name=email]').fill(third_email)
        self.admin.locator('[name=kind]').select_option('member')
        self.submit(self.admin, status=200)
        invitation = self.admin.locator('code').inner_text()
        third_context = self.browser.new_context()
        try:
            third = third_context.new_page()
            third.on('pageerror', lambda error: self.errors.append(type(error).__name__))
            self.open(third, urlparse(invitation).path)
            third.locator('[name=password1]').fill(third_password)
            third.locator('[name=password2]').fill(third_password)
            self.submit(third, 'Accept invitation')
            self.open(self.admin, self.organization)
            self.admin.get_by_role('link', name=third_email, exact=True).click()
            self.admin.locator('[name=roles]').select_option(label='Chat User')
            self.submit(self.admin)
            self.open(self.member, self.organization + 'chat/new/')
            self.member.locator('[name=title]').fill('Release group')
            self.member.locator('[name=kind]').select_option('group')
            self.member.locator('[name=topic]').fill('Release planning together')
            self.member.locator('[name=members]').select_option(label=[self.admin_email, third_email])
            self.submit(self.member)
            group_path = urlparse(self.member.url).path
            self.send(self.member, 'A shared group discussion')
            self.open(third, group_path)
            expect(third.get_by_text('A shared group discussion', exact=True)).to_be_visible()
            expect(third.get_by_text('Release planning together', exact=True)).to_be_visible()
            self.open(third, direct_path, 403)
            self.check('group messages reach invited users and direct messages deny an uninvited account')
        finally:
            third_context.close()

        self.open(self.member, self.shared_path)
        original = self.member.locator('#chat-history section').filter(has=self.member.get_by_text('Administrator reply', exact=True))
        original.get_by_role('button', name='Reply', exact=True).click()
        expect(self.member.locator('#reply-banner')).to_be_visible()
        self.send(self.member, 'A contextual reply')
        reply = self.member.locator('#chat-history section').filter(has=self.member.get_by_text('A contextual reply', exact=True))
        expect(reply.locator('.chat-reply-quote')).to_contain_text('Administrator reply')
        expect(self.member.locator('#reply-banner')).to_be_hidden()
        self.send(self.member, 'Review https://example.test/report')
        expect(self.member.get_by_role('link', name='https://example.test/report', exact=True)).to_be_visible()
        share = reply.get_by_role('link', name='Share link').get_attribute('href')
        self.open(self.member, urlparse(urljoin(self.member.url, share)).path + '?' + urlparse(share).query + '#' + urlparse(share).fragment)
        expect(self.member.get_by_text('A contextual reply', exact=True)).to_be_visible()
        self.member.locator('#chat-query').fill('A contextual reply')
        with self.member.expect_navigation():
            self.member.get_by_role('button', name='Search', exact=True).click()
        expect(self.member.locator('#chat-history section')).to_have_count(1)
        expect(self.member.get_by_text('A contextual reply', exact=True)).to_be_visible()
        self.check('replies, safe link sharing, message permalinks and message search work in the browser')

        self.open(self.member, self.shared_path)
        with self.member.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/preferences/')) as saved:
            self.member.locator('#chat-notification-mode').select_option('mentions')
        if saved.value.status != 200:
            raise AssertionError('Notification preferences were not saved.')
        self.open(self.member, self.organization + 'chat/')
        self.open(self.admin, self.shared_path)
        self.send(self.admin, 'Ordinary notification probe')
        self.send(self.admin, 'Please review @' + self.email)
        self.member.bring_to_front()
        self.member.get_by_role('button', name='Notifications', exact=False).click()
        notice = self.member.locator('#chat-notification-list .chat-notification').filter(has_text='Laptop Test')
        expect(notice).to_contain_text('1 mention')
        expect(notice).to_contain_text('1 new notification')
        self.member.locator('#chat-inbox-close').click()
        self.member.get_by_role('link', name='Laptop Test', exact=True).click()
        expect(self.member.locator('#chat-notification-mode')).to_have_value('mentions')
        with self.member.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/preferences/')):
            self.member.locator('#chat-notification-mode').select_option('muted')
        self.open(self.member, self.organization + 'chat/')
        self.send(self.admin, 'Muted probe @' + self.email)
        self.member.bring_to_front()
        expect(self.member.locator('.chat-room-link').filter(has_text='Laptop Test').locator('.chat-badge')).to_have_text('1')
        expect(self.member.locator('#notification-count')).to_have_text('0')
        self.check('live mention notifications and unread badges honor per-user mute preferences')

        self.open(self.member, self.shared_path)
        self.member_context.grant_permissions(['notifications'], origin=self.origin)
        self.member.get_by_role('button', name='Notifications', exact=False).click()
        self.member.locator('#desktop-notifications').click()
        expect(self.member.locator('#desktop-notifications')).to_have_text('Disable desktop notifications')
        self.member.locator('#desktop-notifications').click()
        expect(self.member.locator('#desktop-notifications')).to_have_text('Enable desktop notifications')
        self.member.locator('#chat-inbox-close').click()
        self.check('desktop notifications require browser permission and an explicit user opt-in')

        self.member.set_viewport_size({'width': 1440, 'height': 900})
        Path('artifacts').mkdir(exist_ok=True)
        self.member.screenshot(path='artifacts/chat-modern-desktop.png')
        self.member.set_viewport_size({'width': 390, 'height': 844})
        expect(self.member.locator('#chat-composer')).to_be_visible()
        if self.member.evaluate('document.documentElement.scrollWidth > window.innerWidth'):
            raise AssertionError('Chat workspace overflows a phone-sized viewport.')
        self.member.screenshot(path='artifacts/chat-modern-mobile.png')
        self.member.set_viewport_size({'width': 1280, 'height': 720})
        self.member.locator('#chat-notification-mode').select_option('all')
        self.check('desktop and phone-sized layouts keep messages and composer usable')

    def check_restart(self):
        self.login(self.admin, self.admin_email, self.instance['password'])
        self.open(self.admin, self.organization + 'chat/')
        self.admin.get_by_role('link', name='Laptop Test', exact=True).click()
        expect(self.admin.get_by_text('Message from test user', exact=True)).to_be_visible()
        expect(self.admin.get_by_text('One logical message', exact=True)).to_have_count(1)
        self.admin.get_by_role('link', name='Conversations', exact=True).click()
        self.admin.get_by_role('link', name='Member-owned conversation', exact=True).click()
        expect(self.admin.get_by_text('Owner message survives restart', exact=True)).to_be_visible()
        self.check('browser history remains readable after container restart')
