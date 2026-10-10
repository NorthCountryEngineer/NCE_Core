"""Production error pages retain status, accessible landmarks and safe messaging."""
from django.core.exceptions import PermissionDenied
from django.test import Client, SimpleTestCase, override_settings
from django.urls import path

from core.urls import urlpatterns as core_patterns


def denied(request):
    raise PermissionDenied('Private policy detail')


def broken(request):
    raise RuntimeError('Private internal failure')


urlpatterns = [path('test-denied/', denied), path('test-broken/', broken), *core_patterns]


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
class ProductionErrorPageTests(SimpleTestCase):
    def test_denied_page_preserves_status_without_exposing_policy_detail(self):
        response = self.client.get('/test-denied/')
        self.assertContains(response, '<h1>Access denied</h1>', status_code=403)
        self.assertContains(response, 'href="#main-content"', status_code=403)
        self.assertNotContains(response, 'Private policy detail', status_code=403)

    def test_missing_page_has_named_title_and_main_landmark(self):
        response = self.client.get('/test-missing/')
        self.assertContains(response, '<title>Page not found', status_code=404)
        self.assertContains(response, '<main id="main-content" tabindex="-1">', status_code=404)

    def test_unverified_forms_keep_csrf_rejection_and_accessible_recovery(self):
        response = Client(enforce_csrf_checks=True).post('/test-denied/', {})
        self.assertContains(response, '<h1>Form verification failed</h1>', status_code=403)
        self.assertContains(response, 'href="#main-content"', status_code=403)
        self.assertNotContains(response, 'Private policy detail', status_code=403)

    def test_server_error_renders_without_database_or_private_exception_details(self):
        self.client.raise_request_exception = False
        response = self.client.get('/test-broken/')
        self.assertContains(response, '<html lang="en">', status_code=500)
        self.assertContains(response, '<h1>Service temporarily unavailable</h1>', status_code=500)
        self.assertNotContains(response, 'Private internal failure', status_code=500)
