import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from identity.models import Application, Invitation, Membership, Organization, PermissionTag, Role, Service, ServicePermission, User

PASSWORD = secrets.token_urlsafe(32)


class AppAuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(slug='family', name='Family')
        cls.other = Organization.objects.create(slug='other', name='Other family')
        cls.service = Service.objects.create(organization=cls.org, slug='axolotl-news', name='Axolotl News')
        cls.app = Application(service=cls.service, name='Axolotl News production browser',
            client_type=Application.CLIENT_PUBLIC, authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            algorithm=Application.RS256_ALGORITHM, redirect_uris='https://axolotl.example.test/oauth/callback',
            allowed_origins='https://axolotl.example.test')
        cls.app.full_clean(); cls.app.save()
        cls.user = User.objects.create_user('existing@example.test', PASSWORD)
        cls.raw = secrets.token_urlsafe(32)
        cls.invitation = Invitation.objects.create(organization=cls.org, email='new@example.test',
            token_hash=Invitation.digest(cls.raw), expires=timezone.now() + timedelta(days=1))

    def flow(self, **overrides):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': self.app.client_id, 'redirect_uri': self.app.redirect_uris,
                  'response_type': 'code', 'scope': 'openid authorization', 'state': 'synthetic-state',
                  'code_challenge': challenge, 'code_challenge_method': 'S256', **overrides}
        return '/oauth/authorize/?' + urlencode(params), verifier

    def entry(self, next_url, raw=None):
        return self.client.post(reverse('app_signup'), {'next': next_url, 'invitation': raw or self.raw})

    def accept(self, next_url, raw=None, **extra):
        return self.client.post(reverse('accept_invitation', args=[raw or self.raw]), {
            'next': next_url, 'first_name': 'Synthetic', 'last_name': 'Reader',
            'password1': PASSWORD, 'password2': PASSWORD, **extra})

    def test_login_and_consent_use_registered_app_brand_without_admin_header(self):
        next_url, _ = self.flow()
        response = self.client.get(reverse('login'), {'next': next_url})
        self.assertContains(response, 'Sign in to Axolotl News')
        self.assertContains(response, '/static/identity/axolotl-mark.svg')
        self.assertContains(response, 'Create your account')
        self.assertNotContains(response, '<core-header')
        self.assertNotContains(response, '>Organizations</a>')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        Membership.objects.create(organization=self.org, user=self.user)
        self.client.force_login(self.user)
        response = self.client.get(next_url)
        self.assertContains(response, 'Continue to Axolotl News?')
        self.assertContains(response, 'name="allow"')
        self.assertNotContains(response, '<core-header')

    def test_new_invited_account_exists_only_in_core_and_has_no_roles_or_permissions(self):
        next_url, verifier = self.flow()
        response = self.entry(next_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlparse(response.url).query)['next'], [next_url])
        response = self.client.get(response.url)
        self.assertContains(response, 'Create your Axolotl News account')
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        response = self.accept(next_url, kind='administrator', roles='forged', email=self.user.email)
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        user = User.objects.get(email='new@example.test')
        membership = Membership.objects.get(organization=self.org, user=user)
        self.assertEqual(membership.kind, 'member')
        self.assertEqual(membership.roles.count(), 0)
        self.assertEqual(membership.access_tags.count(), 0)
        self.assertFalse(user.is_staff or user.is_superuser or user.is_developer)
        page = self.client.get(response.url)
        consent = {field.name: field.value() or '' for field in page.context['form']}
        consent['allow'] = 'true'
        response = self.client.post('/oauth/authorize/', consent)
        code = parse_qs(urlparse(response.url).query)['code'][0]
        tokens = self.client.post('/oauth/token/', {'client_id': self.app.client_id,
            'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.app.redirect_uris,
            'code_verifier': verifier})
        self.assertEqual(tokens.status_code, 200)
        info = self.client.get('/oauth/userinfo/', HTTP_AUTHORIZATION='Bearer ' + tokens.json()['access_token'])
        self.assertEqual(info.json()['sub'], str(user.pk))
        self.assertEqual(info.json()['permissions'], [])
        self.assertEqual(User.objects.filter(email='new@example.test').count(), 1)
        self.assertEqual(self.accept(next_url).status_code, 410)

    def test_existing_account_must_authenticate_and_keeps_password_and_uuid(self):
        self.invitation.email = self.user.email; self.invitation.save()
        next_url, _ = self.flow()
        response = self.client.get(reverse('accept_invitation', args=[self.raw]), {'next': next_url})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.url)
        self.assertContains(response, 'Sign in to Axolotl News')
        self.client.force_login(self.user)
        response = self.accept(next_url)
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertEqual(User.objects.filter(email=self.user.email).count(), 1)
        self.assertEqual(Membership.objects.get(organization=self.org).user_id, self.user.pk)

    def test_same_user_can_join_another_app_without_copying_roles(self):
        Membership.objects.create(user=self.user, organization=self.other, kind='administrator')
        permission = ServicePermission.objects.create(service=self.service, codename='posts-write', name='Edit')
        role = Role.objects.create(organization=self.org, slug='editor', name='Editor')
        role.permissions.add(permission)
        self.invitation.email = self.user.email; self.invitation.save()
        self.client.force_login(self.user)
        next_url, _ = self.flow(); self.accept(next_url)
        member = Membership.objects.get(organization=self.org, user=self.user)
        self.assertEqual(member.kind, 'member')
        self.assertEqual(member.roles.count(), 0)
        self.assertEqual(User.objects.count(), 1)

    def test_ungranted_boundary_returns_to_app_for_access_approval(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='app-access', name='App access')
        self.app.permission_tags.add(tag)
        next_url, _ = self.flow()
        response = self.accept(next_url)
        self.assertRedirects(response, 'https://axolotl.example.test/?account=created', fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(email='new@example.test').exists())
        self.assertEqual(Membership.objects.get(organization=self.org).access_tags.count(), 0)

    def test_invalid_or_inactive_client_and_redirect_cannot_control_registration(self):
        for overrides in [{'redirect_uri': 'https://evil.example/callback'}, {'client_id': 'unknown'},
                          {'code_challenge_method': 'plain'}, {'code_challenge': 'short'}, {'response_type': 'token'}]:
            with self.subTest(overrides=overrides):
                next_url, _ = self.flow(**overrides)
                self.assertEqual(self.entry(next_url).status_code, 404)
        for value in ['//evil.example/path', 'https://evil.example/path', '/platform-admin/', '/oauth/authorize/?client_id=x']:
            self.assertEqual(self.entry(value).status_code, 404)
        self.app.active = False; self.app.save(update_fields=['active'])
        next_url, _ = self.flow(); self.assertEqual(self.entry(next_url).status_code, 404)
        self.assertFalse(User.objects.filter(email='new@example.test').exists())

    def test_wrong_org_expired_or_used_invitations_do_not_create_accounts(self):
        next_url, _ = self.flow()
        for changes in [{'organization': self.other}, {'expires': timezone.now() - timedelta(days=1)}, {'accepted': True}]:
            with self.subTest(changes=changes):
                for key, value in changes.items(): setattr(self.invitation, key, value)
                self.invitation.save()
                response = self.entry(next_url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'This invitation is unavailable')
                self.assertFalse(User.objects.filter(email='new@example.test').exists())
                self.invitation.organization = self.org
                self.invitation.expires = timezone.now() + timedelta(days=1)
                self.invitation.accepted = False
        self.invitation.organization = self.other; self.invitation.save()
        self.assertEqual(self.accept(next_url).status_code, 404)

    def test_invitation_links_and_redirects_stay_with_core_and_registered_app(self):
        next_url, _ = self.flow()
        from django.conf import settings
        response = self.entry(next_url, settings.ISSUER + reverse('accept_invitation', args=[self.raw]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.entry(next_url, 'https://evil.example/invitations/' + self.raw + '/').status_code, 200)
        self.assertEqual(self.accept('https://evil.example/path').status_code, 400)
        duplicate = next_url + '&client_id=other'
        self.assertEqual(self.entry(duplicate).status_code, 404)

    def test_csrf_is_required_for_invitation_entry_and_account_creation(self):
        client = Client(enforce_csrf_checks=True)
        next_url, _ = self.flow()
        self.assertEqual(client.post(reverse('app_signup'), {'next': next_url, 'invitation': self.raw}).status_code, 403)
        self.assertEqual(client.post(reverse('accept_invitation', args=[self.raw]), {'next': next_url}).status_code, 403)
        self.assertFalse(User.objects.filter(email='new@example.test').exists())

    def test_branded_forms_support_browser_same_origin_csrf_posts(self):
        client = Client(enforce_csrf_checks=True)
        next_url, _ = self.flow()
        page = client.get(reverse('app_signup'), {'next': next_url})
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        token = client.cookies['csrftoken'].value
        response = client.post(reverse('app_signup'), {
            'next': next_url, 'invitation': self.raw, 'csrfmiddlewaretoken': token,
        }, HTTP_ORIGIN='http://testserver')
        self.assertEqual(response.status_code, 302)
        page = client.get(response.url)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        response = client.post(reverse('accept_invitation', args=[self.raw]), {
            'next': next_url, 'password1': PASSWORD, 'password2': PASSWORD,
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
        }, HTTP_ORIGIN='http://testserver')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email='new@example.test').exists())

    def test_service_branding_is_server_owned_and_html_escaped(self):
        self.service.auth_name = '<script>bad()</script>'
        self.service.auth_tagline = 'A custom invitation'
        self.service.auth_theme = 'core'; self.service.save()
        next_url, _ = self.flow()
        response = self.client.get(reverse('login'), {'next': next_url, 'auth_theme': 'axolotl'})
        self.assertContains(response, '&lt;script&gt;bad()&lt;/script&gt;')
        self.assertNotContains(response, '<script>bad()')
        self.assertContains(response, 'theme-core')
        self.assertContains(response, 'A custom invitation')
