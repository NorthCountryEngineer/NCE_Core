import base64
import hashlib
import io
import json
import secrets
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from oauth2_provider.models import get_grant_model

from identity.consent import registration_fingerprint
from identity.models import Application, ApplicationConsent, Membership, Organization, Role, Service, User
from identity.native_clients import ROBOTICS_DEVELOPMENT_REDIRECT
from platform_services.edge import register_application, register_development_client


@override_settings(ISSUER='https://core.example.test', SECURE_SSL_REDIRECT=False)
class NativeDevelopmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Robot family', slug='robot-family')
        cls.other_org = Organization.objects.create(name='Other family', slug='other-family')
        cls.admin = User.objects.create_user('native-admin@example.test')
        cls.reader = User.objects.create_user('native-reader@example.test')
        cls.outsider = User.objects.create_user('native-outsider@example.test')
        cls.admin_member = Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
        reader_member = Membership.objects.create(organization=cls.org, user=cls.reader)
        Membership.objects.create(organization=cls.other_org, user=cls.outsider, kind='administrator')
        cls.installation, cls.production = register_application(cls.org)
        cls.installation, cls.development = register_development_client(cls.org)
        role = Role.objects.create(organization=cls.org, slug='read-robots', name='Read robots')
        role.permissions.add(cls.installation.service.permissions.get(codename='device-read'))
        reader_member.roles.add(role)

    def authorization(self, app=None, user=None, **overrides):
        app = app or self.development
        self.client.force_login(user or self.admin)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': app.client_id, 'redirect_uri': app.redirect_uris, 'response_type': 'code',
            'scope': 'openid authorization', 'state': secrets.token_urlsafe(24), 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'prompt': 'consent', **overrides}
        return self.client.get('/oauth/authorize/', params), verifier, params

    def code(self, app=None, **overrides):
        app = app or self.development
        page, verifier, params = self.authorization(app=app, **overrides)
        self.assertEqual(page.status_code, 200, page.content)
        form = {field.name: field.value() or '' for field in page.context['form']}
        form['allow'] = 'true'
        result = self.client.post('/oauth/authorize/', form)
        self.assertEqual(result.status_code, 302, result.content)
        self.assertTrue(result['Location'].startswith(app.redirect_uris + '?'))
        values = parse_qs(urlparse(result['Location']).query)
        self.assertEqual(values['state'], [params['state']])
        return values['code'][0], verifier

    def redeem(self, code, verifier, app=None, **overrides):
        app = app or self.development
        return self.client.post('/oauth/token/', {'grant_type': 'authorization_code', 'client_id': app.client_id,
            'code': code, 'redirect_uri': app.redirect_uris, 'code_verifier': verifier, **overrides})

    def test_registration_is_separate_idempotent_and_preserves_production(self):
        _, same = register_development_client(self.org)
        self.assertEqual(same.pk, self.development.pk)
        self.assertNotEqual(self.development.client_id, self.production.client_id)
        self.production.refresh_from_db()
        self.assertFalse(self.production.native_development)
        self.assertEqual(self.production.redirect_uris, 'https://core.example.test/apps/nce-robotics/oauth/callback')
        self.assertTrue(self.development.native_development)
        self.assertEqual(self.development.client_type, 'public')
        self.assertEqual(self.development.redirect_uris, ROBOTICS_DEVELOPMENT_REDIRECT)
        self.assertEqual(self.production.service_id, self.development.service_id)

    def test_only_exact_explicit_development_callback_is_allowed(self):
        for uri in [ROBOTICS_DEVELOPMENT_REDIRECT + '/', ROBOTICS_DEVELOPMENT_REDIRECT + '?x=1',
                    'ai.nnytech.ncerobotics.dev://oauth/callback', 'other.scheme:/oauth/callback',
                    ROBOTICS_DEVELOPMENT_REDIRECT + '#fragment']:
            self.assertFalse(self.development.redirect_uri_allowed(uri))
        self.assertFalse(self.production.redirect_uri_allowed(ROBOTICS_DEVELOPMENT_REDIRECT))
        self.assertNotIn('ai.nnytech.ncerobotics.dev', self.production.get_allowed_schemes())
        with self.assertRaises(ValidationError):
            Application(service=self.installation.service, name='Unmarked app', client_type='public',
                authorization_grant_type='authorization-code', algorithm='RS256',
                redirect_uris=ROBOTICS_DEVELOPMENT_REDIRECT).full_clean()
        for changed in [{'client_type': 'confidential'}, {'allowed_origins': 'https://example.test'},
                        {'redirect_uris': ROBOTICS_DEVELOPMENT_REDIRECT + ' ' + self.production.redirect_uris}]:
            app = Application.objects.get(pk=self.development.pk)
            for key, value in changed.items(): setattr(app, key, value)
            with self.assertRaises(ValidationError): app.full_clean()

    def test_profile_cannot_move_to_another_service_or_convert_production(self):
        self.production.native_development = True
        self.production.redirect_uris = ROBOTICS_DEVELOPMENT_REDIRECT
        with self.assertRaises(ValidationError): self.production.full_clean()
        other_service = Service.objects.create(organization=self.org, slug='unrelated', name='Unrelated')
        app = Application.objects.get(pk=self.development.pk)
        app.service = other_service
        with self.assertRaises(ValidationError): app.full_clean()
        app = Application(service=other_service, name='Pretend dev', client_type='public', native_development=True,
            authorization_grant_type='authorization-code', algorithm='RS256', redirect_uris=ROBOTICS_DEVELOPMENT_REDIRECT)
        with self.assertRaises(ValidationError): app.full_clean()

    def test_public_pkce_flow_and_core_service_access_work_without_client_secret(self):
        for app in (self.production, self.development):
            with self.subTest(profile=app.name):
                code, verifier = self.code(app)
                result = self.redeem(code, verifier, app)
                self.assertEqual(result.status_code, 200, result.content)
                token = result.json()['access_token']
                context = self.client.get('/api/edge/v1/context/', secure=True, HTTP_AUTHORIZATION='Bearer ' + token)
                self.assertEqual(context.status_code, 200, context.content)
                self.assertEqual(context.json()['service_id'], str(self.installation.service_id))
                self.assertEqual(context.json()['organization_id'], str(self.org.pk))

    def test_codes_cannot_cross_profiles_or_change_redirect_at_redemption(self):
        for source, other in ((self.development, self.production), (self.production, self.development)):
            with self.subTest(profile=source.name):
                code, verifier = self.code(source)
                response = self.redeem(code, verifier, other)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'invalid_grant')
                code, verifier = self.code(source)
                response = self.redeem(code, verifier, source, redirect_uri=other.redirect_uris)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'invalid_request')

    @override_settings(DEBUG=False)
    def test_http_and_other_private_use_callbacks_remain_forbidden(self):
        for app in (self.production, self.development):
            for uri in ('http://core.example.test/apps/nce-robotics/oauth/callback',
                        'http://127.0.0.1/oauth/callback', 'ai.nnytech.other:/oauth/callback'):
                with self.subTest(profile=app.name, uri=uri):
                    self.assertFalse(app.redirect_uri_allowed(uri))
                    response, _, _ = self.authorization(app=app, redirect_uri=uri)
                    self.assertEqual(response.status_code, 400)
        invalid = Application(service=self.installation.service, name='HTTP app', client_type='public',
            authorization_grant_type='authorization-code', algorithm='RS256',
            redirect_uris='http://core.example.test/oauth/callback')
        with self.assertRaises(ValidationError): invalid.full_clean()

    def test_claims_and_saved_consent_stay_bound_to_profile_and_exact_request(self):
        claims = {'userinfo': {'name': None}}
        code, verifier = self.code(claims=json.dumps(claims))
        grant = get_grant_model().objects.get(code=code)
        self.assertEqual(json.loads(grant.claims), claims)
        consent = ApplicationConsent.objects.get(user=self.admin, application=self.development)
        self.assertEqual(consent.registration_fingerprint,
            registration_fingerprint(self.development, self.development.redirect_uris, claims))
        self.assertEqual(self.redeem(code, verifier).status_code, 200)
        identical, verifier, _ = self.authorization(prompt='', claims=json.dumps(claims))
        self.assertEqual(identical.status_code, 302)
        self.assertIn('code', parse_qs(urlparse(identical['Location']).query))
        changed, _, _ = self.authorization(prompt='', claims=json.dumps({'userinfo': {'email': None}}))
        self.assertEqual(changed.status_code, 200)
        other_profile, _, _ = self.authorization(app=self.production, prompt='', claims=json.dumps(claims))
        self.assertEqual(other_profile.status_code, 200)
        self.assertFalse(ApplicationConsent.objects.filter(user=self.admin, application=self.production).exists())

    def test_wrong_pkce_and_missing_challenge_remain_rejected(self):
        code, _ = self.code()
        result = self.redeem(code, secrets.token_urlsafe(48))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['error'], 'invalid_grant')
        result, _, _ = self.authorization(code_challenge='', code_challenge_method='')
        self.assertIn(result.status_code, [400, 302])
        self.assertNotIn('code=', result.get('Location', ''))

    def test_wrong_callback_and_cross_organization_membership_remain_rejected(self):
        result, _, _ = self.authorization(redirect_uri=ROBOTICS_DEVELOPMENT_REDIRECT + '?changed=1')
        self.assertEqual(result.status_code, 400)
        result, _, _ = self.authorization(user=self.outsider)
        self.assertEqual(result.status_code, 403)

    def test_live_membership_and_installation_revocation_remain_enforced(self):
        code, verifier = self.code()
        self.admin_member.active = False
        self.admin_member.save(update_fields=['active'])
        self.assertEqual(self.redeem(code, verifier).status_code, 400)
        self.installation.enabled = False
        self.installation.save(update_fields=['enabled'])
        self.assertFalse(self.development.is_usable(None))

    def test_portal_registration_is_admin_only_and_csrf_protected(self):
        self.client.force_login(self.reader)
        url = reverse('edge_register_development', args=[self.org.slug])
        self.assertEqual(self.client.post(url).status_code, 403)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.admin)
        self.assertEqual(protected.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).status_code, 302)
        page = self.client.get(reverse('edge_dashboard', args=[self.org.slug]))
        self.assertContains(page, 'Development client ID')
        self.assertContains(page, self.development.client_id)
        self.assertContains(page, self.production.client_id)

    def test_cli_returns_distinct_development_client(self):
        output = io.StringIO()
        call_command('register_robotics', organization=self.org.slug, development_client=True, stdout=output)
        self.assertIn(self.development.client_id, output.getvalue())
        self.assertIn(ROBOTICS_DEVELOPMENT_REDIRECT, output.getvalue())

    def test_production_consent_fingerprint_is_unchanged(self):
        app = self.production
        prior = {'client_id': app.client_id, 'client_type': app.client_type,
            'grant_type': app.authorization_grant_type, 'algorithm': app.algorithm, 'name': app.name,
            'service_id': str(app.service_id), 'organization_id': str(app.service.organization_id),
            'redirect_uris': sorted(app.redirect_uris.split()), 'allowed_origins': sorted(app.allowed_origins.split()),
            'redirect_uri': app.redirect_uris, 'claims': {}, 'deployment_id': None, 'environment_id': None}
        expected = hashlib.sha256(json.dumps(prior, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(registration_fingerprint(app, app.redirect_uris), expected)
