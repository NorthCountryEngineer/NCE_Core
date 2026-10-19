"""The directory guide is an additive, administrator-only upgrade."""
from importlib import import_module

from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from platform_services.documentation import render_document, save_page
from platform_services.documentation_catalog import sync_bundled_documentation
from platform_services.models import DocumentationPage, DocumentationSource
from platform_services.tests.test_documentation import fixtures


class DirectoryDocumentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures(cls)

    def test_directory_guide_requires_administrative_document_access(self):
        page = self.core.documentation_pages.get(slug='employee-directory')
        self.assertIn('Reporting relationships', render_document(page, self.principal)['markdown'])
        with self.assertRaises(PermissionDenied):
            render_document(page, self.reader)

    def test_first_catalog_sync_keeps_the_migrated_directory_source_revision(self):
        source = self.core.documentation_pages.get(slug='employee-directory').source
        count = DocumentationSource.objects.filter(package=source.package, slug=source.slug).count()
        sync_bundled_documentation()
        self.assertEqual(DocumentationSource.objects.filter(package=source.package, slug=source.slug).count(), count)

    def test_upgrade_is_repeatable_and_preserves_local_edits_and_source_pins(self):
        page = self.core.documentation_pages.get(slug='employee-directory')
        pinned = page.source
        page.mode, page.markdown = 'extend', 'Local personnel instructions.'
        save_page(page, self.principal, page.revision)
        latest = DocumentationSource(package=pinned.package, version=pinned.version,
            slug=pinned.slug, title=pinned.title, summary=pinned.summary,
            category=pinned.category, audience=pinned.audience,
            markdown=pinned.markdown + '\n\nPublisher update.', revision=pinned.revision + 1)
        latest.full_clean()
        latest.save()
        missing = self.other.documentation_pages.get(installation__package__slug='core-platform', slug=pinned.slug)
        installation = missing.installation
        missing.revisions.all().delete()
        missing.delete()
        before = (DocumentationSource.objects.count(), DocumentationPage.objects.count())
        seed = import_module('platform_services.migrations.0041_employee_directory_documentation').seed
        seed(apps, None)
        seed(apps, None)
        self.assertEqual(DocumentationSource.objects.count(), before[0])
        self.assertEqual(DocumentationPage.objects.count(), before[1] + 1)
        page.refresh_from_db()
        self.assertEqual(page.source_id, pinned.pk)
        self.assertEqual(page.markdown, 'Local personnel instructions.')
        self.assertEqual(page.revision, 2)
        restored = installation.documentation_pages.get(slug=pinned.slug)
        self.assertEqual(restored.source_id, latest.pk)
        self.assertEqual(restored.revisions.count(), 1)
