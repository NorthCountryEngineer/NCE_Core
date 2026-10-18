from datetime import timedelta
from importlib import import_module
import copy
import json
import secrets
from unittest.mock import Mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.apps import apps
from django.test import Client, LiveServerTestCase, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, set_token_value

from core_client import CoreClient, CoreServiceError
from documentation_client import OrganizationDocumentation
from identity.access import Principal
from identity.models import AuditEvent, Membership, Organization, PermissionTag, Role, ServicePermission, User
from platform_services.agent_identity import issue_credential
from platform_services.catalog import CORE_CATALOG, seed_catalog
from platform_services.documentation import (DocumentationContext, chunks, documents, publish_source,
    render_document, safe_html, save_page, source_digest)
from platform_services.documentation_catalog import GUIDES, sync_bundled_documentation
from platform_services.environments import create_environment
from platform_services.models import (AgentBinding, AgentDefinition, AgentTask, DocumentationPage,
    DocumentationSource, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential)
from platform_services.services import create_deployment, install_core, install_package, issue_workload_credential


def fixtures(cls):
    # Transaction/live-server tests flush migration seed rows between test classes.
    if not MarketplacePackage.objects.filter(slug='core-platform').exists():
        seed_catalog(apps, None)
        sync_bundled_documentation()
    cls.org = Organization.objects.create(name='Documentation customer', slug='docs-customer', identity_name='Customer Workspace')
    cls.other = Organization.objects.create(name='Other organization', slug='docs-other')
    install_core(cls.org)
    install_core(cls.other)
    cls.admin = User.objects.create_user('docs-admin@example.test')
    cls.member = User.objects.create_user('docs-reader@example.test')
    Membership.objects.create(organization=cls.org, user=cls.admin, kind='administrator')
    cls.membership = Membership.objects.create(organization=cls.org, user=cls.member)
    cls.principal = Principal(user=cls.admin)
    cls.reader = Principal(user=cls.member)
    cls.environment = create_environment(cls.org, name='sandbox', namespace='docs-sandbox', application_origin='https://docs-app.example.test')
    cls.production = create_environment(cls.org, name='production', namespace='docs-production', application_origin='https://docs-prod.example.test')
    cls.membership.access_tags.add(cls.environment.boundary_tag)
    cls.package = MarketplacePackage.objects.create(organization=cls.org, slug='docs-app', name='Documented app', kind='application',
        description='Documented application', manifest={'image': 'registry.example.test/docs@sha256:' + 'a' * 64, 'capabilities': ['docs.read', 'docs.admin']})
    cls.installation = install_package(cls.org, cls.package, cls.package.manifest['capabilities'])
    cls.credential, cls.token = issue_workload_credential(cls.installation, 'Documentation backend', environment=cls.environment)
    cls.core = Installation.objects.get(organization=cls.org, package__slug='core-platform')
    cls.role = Role.objects.create(organization=cls.org, slug='documentation-reader', name='Documentation reader')
    cls.role.permissions.add(cls.core.service.permissions.get(codename='docs-read'), cls.installation.service.permissions.get(codename='docs-read'))
    cls.membership.roles.add(cls.role)
    cls.values = {'version': cls.package.version, 'slug': 'application-guide', 'title': 'Documented application quickstart', 'summary': 'How to use this application',
        'category': 'user-guide', 'audience': 'users', 'required_permissions': [],
        'markdown': '# {{application_name}}\n\nOrganization: {{organization_name}}; Platform: {{platform_name}}.\n\nEnvironment: {{environment_name}}.\n\nRead and create approved records.'}
    cls.source = publish_source(cls.package, cls.values, cls.principal)
    cls.page = cls.installation.documentation_pages.get(slug='application-guide')


class DocumentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures(cls)

    def api(self, suffix='', *, token=None, user_token=None, query=None):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + (token or self.token)}
        if user_token:
            headers['HTTP_X_CORE_USER_TOKEN'] = user_token
        return self.client.get('/api/platform/v1/documentation/' + suffix, query or {}, **headers)

    def oauth(self, user=None, application=None):
        raw = secrets.token_urlsafe(48)
        token = AccessToken(application=application, user=user or self.member, expires=timezone.now() + timedelta(minutes=5), scope='openid authorization')
        set_token_value(token, raw)
        token.save()
        return raw

    def deployment(self):
        node = RuntimeNode.objects.create(organization=self.org, name='Docs runtime', namespace=self.environment.namespace,
            environment=self.environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        return create_deployment(self.installation, node, 'https://docs-app.example.test')

    def scoped(self, environment=None, deployment=None):
        page = DocumentationPage(organization=self.org, installation=self.installation, environment=environment or self.environment,
            deployment=deployment, source=self.source, slug='scoped-guide', title='Scoped guidance', mode='extend', markdown='Local environment instructions.')
        return save_page(page, self.principal)

    def test_bundled_sources_and_organization_inheritance(self):
        self.assertEqual(self.core.documentation_pages.count(), sum(row[0] == 'core-platform' for row in GUIDES))
        self.assertEqual(self.org.installations.count(), len(CORE_CATALOG) + 1)
        page = self.core.documentation_pages.get(slug='getting-started')
        result = render_document(page, self.reader)
        self.assertIn('Customer Workspace', result['markdown'])
        self.assertIn('Documentation customer', result['markdown'])
        self.assertEqual(result['source']['digest'], source_digest(page.source))
        self.assertNotIn('Other organization', result['markdown'])

    def test_installation_automatically_inherits_registered_sources(self):
        new_org = Organization.objects.create(name='New organization', slug='docs-new')
        install_core(new_org)
        self.assertTrue(new_org.documentation_pages.filter(installation__package__slug='core-platform').exists())
        self.assertEqual(self.page.source_id, self.source.pk)
        self.assertEqual(self.page.revisions.count(), 1)

    def test_extend_replace_and_optimistic_revisions(self):
        self.page.mode, self.page.markdown = 'extend', 'Customer escalation instructions.'
        save_page(self.page, self.principal, 1)
        first = render_document(self.page, self.reader)
        self.assertIn('Read and create approved records.', first['markdown'])
        self.assertIn('Customer escalation instructions.', first['markdown'])
        with self.assertRaises(ValidationError):
            save_page(self.page, self.principal, 1)
        self.page.mode, self.page.markdown = 'replace', 'Replacement instructions.'
        save_page(self.page, self.principal, 2)
        result = render_document(self.page, self.reader)
        self.assertEqual(result['markdown'], 'Replacement instructions.')
        self.assertEqual(result['revision'], 3)
        self.assertEqual(self.page.revisions.count(), 3)

    def test_new_publisher_revision_requires_explicit_adoption(self):
        newer = publish_source(self.package, {**self.values, 'markdown': 'Updated publisher guidance.'}, self.principal)
        self.page.refresh_from_db()
        self.assertEqual(self.page.source_id, self.source.pk)
        self.assertEqual(newer.revision, 2)
        self.page.source = newer
        save_page(self.page, self.principal, 1)
        self.assertEqual(render_document(self.page, self.reader)['markdown'], newer.markdown)

    def test_inherited_source_policy_cannot_be_detached_or_changed_to_another_guide(self):
        self.page.mode, self.page.markdown, self.page.source = 'replace', 'Local text', None
        with self.assertRaises(ValidationError):
            save_page(self.page, self.principal, 1)
        unrelated = publish_source(self.package, {**self.values, 'slug': 'unrelated'}, self.principal)
        self.page.source = unrelated
        with self.assertRaises(ValidationError):
            save_page(self.page, self.principal, 1)
        self.page.source, self.page.active = self.source, False
        DocumentationSource.objects.filter(pk=self.source.pk).update(active=False)
        Installation.objects.filter(pk=self.installation.pk).update(version='0.2.0')
        self.page.installation.refresh_from_db()
        save_page(self.page, self.principal, 1)
        with self.assertRaises(PermissionDenied):
            render_document(self.page, self.reader)

    def test_source_content_is_immutable_and_digest_corruption_denies_all_reads(self):
        self.source.markdown = 'Changed in place'
        with self.assertRaises(ValidationError):
            self.source.full_clean()
        DocumentationSource.objects.filter(pk=self.source.pk).update(markdown='Corrupt source')
        self.assertEqual(self.api(f'{self.page.pk}/').status_code, 403)
        self.assertEqual(self.api(query={'q': 'Corrupt source'}).json()['documents'], [])

    def test_source_withdrawal_and_application_disable_fail_closed(self):
        for model, pk, field in [(DocumentationSource, self.source.pk, 'active'), (Installation, self.installation.pk, 'enabled')]:
            with self.subTest(model=model):
                model.objects.filter(pk=pk).update(**{field: False})
                with self.assertRaises(PermissionDenied):
                    render_document(self.page, self.reader)
                model.objects.filter(pk=pk).update(**{field: True})

    def test_installed_version_mismatch_requires_review(self):
        Installation.objects.filter(pk=self.installation.pk).update(version='0.2.0')
        for format in ['json', 'markdown', 'jsonl', 'chunks']:
            self.assertEqual(self.api(f'{self.page.pk}/', query={'format': format}).status_code, 403)
        self.assertEqual(self.api(query={'q': self.page.title}).json()['documents'], [])

    def test_replacement_cannot_downgrade_source_audience_or_permissions(self):
        permission = ServicePermission.objects.create(service=self.installation.service, codename='operator-read', name='Operator docs')
        source = publish_source(self.package, {**self.values, 'slug': 'technical', 'audience': 'administrators', 'required_permissions': ['operator-read']}, self.principal)
        page = self.installation.documentation_pages.get(source=source)
        page.audience, page.mode, page.markdown, page.required_permissions = 'users', 'replace', 'Technical replacement', []
        save_page(page, self.principal, 1)
        with self.assertRaises(PermissionDenied):
            render_document(page, self.reader)
        self.role.permissions.add(self.core.service.permissions.get(codename='docs-admin'))
        with self.assertRaises(PermissionDenied):
            render_document(page, self.reader)
        self.role.permissions.add(permission)
        self.assertEqual(render_document(page, self.reader)['markdown'], 'Technical replacement')
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)

    def test_live_member_application_and_core_roles(self):
        self.assertEqual(render_document(self.page, self.reader)['id'], str(self.page.pk))
        for permission in [self.installation.service.permissions.get(codename='docs-read'), self.core.service.permissions.get(codename='docs-read')]:
            self.role.permissions.remove(permission)
            with self.assertRaises(PermissionDenied):
                render_document(self.page, self.reader)
            self.role.permissions.add(permission)
        Membership.objects.filter(pk=self.membership.pk).update(active=False)
        with self.assertRaises(PermissionDenied):
            render_document(self.page, self.reader)

    def test_role_policy_cannot_be_bypassed_through_documentation(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='restricted-role', name='Restricted role')
        self.role.permission_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            documents(self.org, self.reader)

    def test_page_tags_protect_search_read_and_exports_without_title_leaks(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='restricted-guide', name='Restricted guide', allow_workloads=True)
        self.page.permission_tags.add(tag)
        for format in ['json', 'markdown', 'jsonl', 'chunks']:
            self.assertEqual(self.api(f'{self.page.pk}/', query={'format': format}).status_code, 403)
        result = self.api(query={'q': 'Documented application quickstart'})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('Documented application quickstart', result.content.decode())
        self.installation.access_tags.add(tag)
        self.assertEqual(self.api(f'{self.page.pk}/').status_code, 200)
        self.installation.access_tags.remove(tag)
        self.assertEqual(self.api(f'{self.page.pk}/').status_code, 403)
        denial = AuditEvent.objects.filter(action='documentation.detail_denied').latest('created')
        self.assertEqual(denial.target, str(self.page.pk))
        self.assertEqual(denial.outcome, 'denied')

    def test_purpose_and_human_authentication_tags_apply_to_docs(self):
        tag = PermissionTag.objects.create(organization=self.org, slug='purpose-docs', name='Purpose-bound docs', allow_workloads=True, allowed_purposes=['support'])
        self.page.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        with self.assertRaises(PermissionDenied):
            render_document(self.page, self.reader)
        self.assertTrue(render_document(self.page, Principal(user=self.member, purpose='support')))
        tag.requires_mfa = True
        tag.save()
        with self.assertRaises(PermissionDenied):
            render_document(self.page, Principal(user=self.member, purpose='support'))

    def test_machine_context_and_cross_organization_isolation(self):
        page = self.scoped()
        result = self.api(f'{page.pk}/')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['environment'], 'sandbox')
        self.assertIn('Environment: sandbox', result.json()['markdown'])
        production = self.scoped(self.production)
        self.installation.access_tags.add(self.production.boundary_tag)
        self.assertEqual(self.api(f'{production.pk}/').status_code, 403)
        self.assertEqual(self.api(f'{page.pk}/', query={'environment': str(self.production.pk)}).status_code, 400)
        foreign = self.other.documentation_pages.first()
        self.assertEqual(self.api(f'{foreign.pk}/').status_code, 404)

    def test_machine_cannot_consume_another_application_documentation(self):
        package = MarketplacePackage.objects.create(organization=self.org, slug='docs-app-other', name='Other app', kind='application', description='Other', manifest=self.package.manifest)
        installation = install_package(self.org, package)
        source = publish_source(package, self.values, self.principal)
        page = installation.documentation_pages.get(source=source)
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)
        self.assertFalse(any(row['id'] == str(page.pk) for row in self.api().json()['documents']))
        core_chat_guide = self.org.documentation_pages.get(installation__package__slug='core-chat', slug='chat')
        self.assertEqual(self.api(f'{core_chat_guide.pk}/').status_code, 200)

    def test_deployment_guides_require_exact_machine_placement(self):
        deployment = self.deployment()
        page = self.scoped(deployment=deployment)
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)
        credential, raw = issue_workload_credential(self.installation, 'Exact deployment', self.environment, deployment)
        self.assertEqual(self.api(f'{page.pk}/', token=raw).status_code, 200)
        credential.active = False
        credential.save()
        self.assertEqual(self.api(f'{page.pk}/', token=raw).status_code, 401)

    def test_delegated_user_uses_both_role_and_workload_tag_policies(self):
        deployment = self.deployment()
        page = self.scoped()
        permission = ServicePermission.objects.create(service=self.installation.service, codename='operator-docs', name='Operations')
        page.required_permissions = ['operator-docs']
        save_page(page, self.principal, 1)
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)
        raw = self.oauth(application=deployment.application)
        self.assertEqual(self.api(f'{page.pk}/', user_token=raw).status_code, 403)
        self.role.permissions.add(permission)
        result = self.api(f'{page.pk}/', user_token=raw)
        self.assertEqual(result.status_code, 200, result.content)
        tag = PermissionTag.objects.create(organization=self.org, slug='delegation-doc', name='Both principals', allow_workloads=True)
        page.permission_tags.add(tag)
        self.membership.access_tags.add(tag)
        self.assertEqual(self.api(f'{page.pk}/', user_token=raw).status_code, 403)
        self.installation.access_tags.add(tag)
        self.assertEqual(self.api(f'{page.pk}/', user_token=raw).status_code, 200)
        self.role.permissions.remove(permission)
        self.assertEqual(self.api(f'{page.pk}/', user_token=raw).status_code, 403)

    def test_unbound_delegation_and_context_changes_rejected(self):
        self.assertEqual(self.api(f'{self.page.pk}/', user_token=self.oauth()).status_code, 401)
        self.assertEqual(self.api(f'{self.page.pk}/', user_token=secrets.token_urlsafe(40)).status_code, 401)
        self.assertEqual(self.api(query={'environment': 'production'}).status_code, 400)

    def test_agent_account_task_scope_and_live_revocation(self):
        agent = AgentDefinition.objects.create(organization=self.org, slug='docs-agent', name='Documentation agent', kind='inspect')
        task = AgentTask(organization=self.org, name='Documentation UAT', description='Read only the approved guide', resources={'documentation': [str(self.page.pk)]},
            installation=self.installation, environment='sandbox', approved_by=self.admin, expires_at=timezone.now() + timedelta(minutes=20))
        task.full_clean()
        task.save()
        binding = AgentBinding.objects.create(account=agent.account, installation=self.installation, environment='sandbox', task=task)
        binding.roles.add(self.role)
        credential, raw = issue_credential(binding, self.principal)
        result = self.api(f'{self.page.pk}/', token=raw)
        self.assertEqual(result.status_code, 200, result.content)
        platform_page = self.core.documentation_pages.get(slug='getting-started')
        self.assertEqual(self.api(f'{platform_page.pk}/', token=raw).status_code, 403)
        task.resources = {'documentation': []}
        task.save()
        self.assertEqual(self.api(f'{self.page.pk}/', token=raw).status_code, 403)
        credential.active = False
        credential.save()
        self.assertEqual(self.api(f'{self.page.pk}/', token=raw).status_code, 401)

    def test_page_placement_and_task_scopes_validate_org_environment(self):
        page = DocumentationPage(organization=self.other, installation=self.installation, title='Cross tenant', slug='bad', mode='replace', markdown='Bad')
        with self.assertRaises(ValidationError):
            page.full_clean()
        production = self.scoped(self.production)
        task = AgentTask(organization=self.org, name='Wrong environment', description='Denied', installation=self.installation,
            environment='sandbox', approved_by=self.admin, expires_at=timezone.now() + timedelta(minutes=10), resources={'documentation': [str(production.pk)]})
        with self.assertRaises(ValidationError):
            task.full_clean()
        self.page.environment = self.environment
        with self.assertRaises(ValidationError):
            save_page(self.page, self.principal, 1)

    def test_source_publication_authority_and_strict_bundle_validation(self):
        shared = self.core.package
        with self.assertRaises(PermissionDenied):
            publish_source(shared, self.values, self.principal)
        with self.assertRaises(PermissionDenied):
            publish_source(self.package, self.values, self.reader)
        for values in [{**self.values, 'version': {}}, {**self.values, 'markdown': '{{arbitrary_secret}}'},
                {**self.values, 'required_permissions': 'app-read'}, {**self.values, 'url': 'https://example.test'},
                {**self.values, 'markdown': 'x' * 131073}]:
            with self.subTest(values=list(values)):
                with self.assertRaises(ValidationError):
                    publish_source(self.package, values, self.principal)

    def test_sync_is_repeatable_and_preserves_organization_customizations(self):
        page = self.core.documentation_pages.get(slug='getting-started')
        page.mode, page.markdown = 'extend', 'Local onboarding steps'
        save_page(page, self.principal, 1)
        count = DocumentationSource.objects.count()
        sync_bundled_documentation()
        sync_bundled_documentation()
        self.assertEqual(DocumentationSource.objects.count(), count)
        page.refresh_from_db()
        self.assertEqual(page.markdown, 'Local onboarding steps')
        self.assertEqual(page.revision, 2)

    def test_profile_guide_migration_is_repeatable_and_preserves_edits_and_source_pins(self):
        page = self.core.documentation_pages.get(slug='user-profile')
        pinned_source = page.source
        page.mode, page.markdown = 'extend', 'Organization-reviewed profile instructions'
        save_page(page, self.principal, page.revision)
        replacement = DocumentationSource(package=pinned_source.package, version=pinned_source.version,
            slug=pinned_source.slug, title=pinned_source.title, summary=pinned_source.summary,
            category=pinned_source.category, audience=pinned_source.audience,
            markdown=pinned_source.markdown + '\n\nReviewed publisher revision.', revision=pinned_source.revision + 1)
        replacement.full_clean()
        replacement.save()
        missing = self.other.documentation_pages.get(installation__package__slug='core-platform', slug='user-profile')
        installation = missing.installation
        missing.revisions.all().delete()
        missing.delete()
        counts = (DocumentationSource.objects.count(), DocumentationPage.objects.count())
        seed = import_module('platform_services.migrations.0040_user_profile_documentation').seed
        seed(apps, None)
        seed(apps, None)
        self.assertEqual(DocumentationSource.objects.count(), counts[0])
        self.assertEqual(DocumentationPage.objects.count(), counts[1] + 1)
        page.refresh_from_db()
        self.assertEqual(page.source_id, pinned_source.pk)
        self.assertEqual(page.mode, 'extend')
        self.assertEqual(page.markdown, 'Organization-reviewed profile instructions')
        self.assertEqual(page.revision, 2)
        inherited = installation.documentation_pages.get(slug='user-profile')
        self.assertEqual(inherited.source_id, replacement.pk)
        self.assertEqual(inherited.revisions.count(), 1)
        self.assertEqual(inherited.source.digest, source_digest(replacement))

    def test_search_pagination_bounds_and_live_export_metadata(self):
        result = self.api(query={'limit': '1'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()['documents']), 1)
        self.assertTrue(result.json()['next_cursor'])
        for query in [{'limit': '101'}, {'limit': 'wrong'}, {'after': 'wrong'}, {'q': 'x' * 201}]:
            self.assertEqual(self.api(query=query).status_code, 400)
        value = self.api(f'{self.page.pk}/').json()
        exported = self.api(f'{self.page.pk}/', query={'format': 'jsonl'})
        self.assertEqual(exported.status_code, 200)
        rows = [json.loads(line) for line in exported.content.decode().splitlines()]
        self.assertEqual(rows, chunks(value))
        self.assertEqual(exported['Cache-Control'], 'no-store, private')
        self.assertEqual(exported['X-Core-Documentation-Citation'], value['citation'])
        self.assertEqual(exported['X-Core-Documentation-Digest'], value['digest'])

    def test_context_expansion_is_bounded_before_serving_or_indexing(self):
        Organization.objects.filter(pk=self.org.pk).update(name='Long name ' * 16)
        source = publish_source(self.package, {**self.values, 'slug': 'large-context', 'markdown': '{{organization_name}} ' * 5000}, self.principal)
        page = self.installation.documentation_pages.get(source=source)
        with self.assertRaises(PermissionDenied):
            render_document(page, self.reader)
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)
        self.assertFalse(any(row['id'] == str(page.pk) for row in self.api().json()['documents']))

    def test_audit_does_not_store_markdown_or_queries(self):
        render_document(self.page, self.reader)
        self.api(query={'q': 'Private search text'})
        event = AuditEvent.objects.filter(action='documentation.consumed', actor=self.member).latest('created')
        self.assertEqual(event.target, str(self.page.pk))
        self.assertNotIn('Private search text', str(event.__dict__))
        self.assertNotIn('approved records', str(event.__dict__))

    def test_human_reader_admin_editor_and_csrf(self):
        self.client.force_login(self.member)
        result = self.client.get(reverse('documentation', args=[self.org.slug]))
        self.assertContains(result, 'Documented application quickstart')
        self.assertEqual(self.client.get(reverse('documentation_admin', args=[self.org.slug])).status_code, 403)
        self.assertContains(self.client.get(reverse('documentation_page', args=[self.org.slug, self.page.pk])), 'Customer Workspace')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('documentation_admin', args=[self.org.slug])), 'Application documentation coverage', status_code=200, html=False)
        editor = self.client.get(reverse('documentation_edit', args=[self.org.slug, self.page.pk]))
        self.assertEqual(editor.status_code, 200)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.admin)
        self.assertEqual(guarded.post(reverse('documentation_edit', args=[self.org.slug, self.page.pk]), {}).status_code, 403)

    def test_human_organization_scope_and_export_policy(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('documentation', args=[self.other.slug])).status_code, 403)
        url = reverse('documentation_page', args=[self.org.slug, self.page.pk])
        self.assertEqual(self.client.get(url, {'format': 'markdown'}).status_code, 200)
        self.membership.roles.clear()
        self.assertEqual(self.client.get(url, {'format': 'markdown'}).status_code, 403)

    def test_browser_edit_and_publishing_are_persisted_and_reviewable(self):
        self.client.force_login(self.admin)
        url = reverse('documentation_edit', args=[self.org.slug, self.page.pk])
        values = {'installation': str(self.installation.pk), 'source': str(self.source.pk), 'slug': self.page.slug,
            'title': self.page.title, 'summary': self.page.summary, 'category': 'user-guide', 'audience': 'users',
            'required_permissions': '[]', 'mode': 'extend', 'markdown': 'Browser-reviewed instructions',
            'active': 'on', 'expected_revision': 1}
        result = self.client.post(url, values)
        self.assertEqual(result.status_code, 302, result.content[:1000])
        self.page.refresh_from_db()
        self.assertEqual(self.page.revision, 2)
        self.assertIn('Browser-reviewed instructions', render_document(self.page, self.reader)['markdown'])
        stale = self.client.post(url, {**values, 'markdown': 'Stale changes'})
        self.assertContains(stale, 'Documentation changed; reload before editing.')
        self.page.refresh_from_db()
        self.assertEqual(self.page.markdown, 'Browser-reviewed instructions')
        result = self.client.post(reverse('documentation_publish', args=[self.org.slug]),
            {'package': str(self.package.pk), 'bundle': json.dumps({**self.values, 'slug': 'browser-published', 'title': 'Browser-published guide'})})
        self.assertEqual(result.status_code, 302, result.content[:1000])
        self.assertTrue(self.installation.documentation_pages.filter(slug='browser-published').exists())
        Installation.objects.filter(pk=self.installation.pk).update(enabled=False)
        archived = self.client.post(url, {**values, 'expected_revision': 2, 'active': '', 'markdown': 'Browser-reviewed instructions'})
        self.assertEqual(archived.status_code, 302, archived.content[:1000])
        self.page.refresh_from_db()
        self.assertFalse(self.page.active)

    def test_organization_only_guidance_and_default_brand_are_consumable(self):
        Organization.objects.filter(pk=self.org.pk).update(identity_name='')
        page = DocumentationPage(organization=self.org, slug='organization-guide', title='Organization guide', mode='replace',
            markdown='Read {{platform_name}} guidance for {{organization_name}}.')
        save_page(page, self.principal)
        value = self.api(f'{page.pk}/')
        self.assertEqual(value.status_code, 200, value.content[:1000])
        self.assertIn('Core Platform guidance', value.json()['markdown'])
        self.assertIsNone(value.json()['source'])
        page.markdown = 'Value: {{platform_name}}. Syntax: {{!platform_name}}.'
        save_page(page, self.principal, 1)
        value = self.api(f'{page.pk}/').json()
        self.assertIn('Value: Core Platform', value['markdown'])
        self.assertIn('Syntax: {{platform_name}}', value['markdown'])

    def test_environment_disable_and_markdown_injection_are_handled(self):
        page = self.scoped()
        self.org.name = '<script>alert(1)</script> [Support](javascript:alert(2))'
        self.org.save()
        rendered = safe_html(render_document(page, self.principal)['markdown'])
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.environment.active = False
        self.environment.save()
        with self.assertRaises(PermissionDenied):
            render_document(page, self.principal)
        self.assertEqual(self.api(f'{page.pk}/').status_code, 403)

    def test_inherited_environment_tags_fix_context_and_disable_human_access(self):
        self.installation.permission_tags.add(self.environment.boundary_tag)
        value = render_document(self.page, self.principal)
        self.assertEqual(value['environment'], 'sandbox')
        with self.assertRaises(PermissionDenied):
            render_document(self.page, self.principal, DocumentationContext(self.production))
        self.environment.active = False
        self.environment.save()
        with self.assertRaises(PermissionDenied):
            render_document(self.page, self.principal)


class DocumentationContractTests(SimpleTestCase):
    def test_markdown_is_formatted_and_untrusted_html_is_sanitized(self):
        rendered = safe_html('# Guide\n\n**Instructions**\n\n<script>alert(1)</script><img src=x onerror=alert(2)><iframe src="https://example.test"></iframe>\n\n[Unsafe](javascript:alert(3))\n\n| Field | Type |\n| --- | --- |\n| title | string |')
        self.assertIn('<h1', rendered)
        self.assertIn('<strong>Instructions</strong>', rendered)
        self.assertIn('<table>', rendered)
        for forbidden in ['<script', '<img', '<iframe', 'onerror=', 'href="javascript:']:
            self.assertNotIn(forbidden, rendered)

    def test_ai_tools_cannot_choose_authority_or_write(self):
        core = Mock(organization_id='org', environment='sandbox')
        docs = OrganizationDocumentation(core)
        for name, arguments in [('core_docs_search', {'organization': 'other'}), ('core_docs_read', {'document_id': 'bad', 'token': 'model'}),
                ('core_docs_publish', {}), ('core_docs_search', {'limit': 21})]:
            with self.assertRaises(ValueError):
                docs.execute(name, arguments)
        core.request.assert_not_called()
        self.assertEqual({tool['function']['name'] for tool in docs.tools()}, {'core_docs_search', 'core_docs_read'})


class LiveDocumentationTests(LiveServerTestCase):
    def setUp(self):
        fixtures(self)

    def test_portable_sdk_live_retrieval_exports_and_revocation(self):
        core = CoreClient(self.live_server_url + '/api/platform/v1', self.token, str(self.org.pk), allow_local_http=True, environment='sandbox')
        docs = OrganizationDocumentation(core)
        result = docs.execute('core_docs_search', {'query': 'Documented application quickstart', 'limit': 5})
        self.assertEqual([row['id'] for row in result['documents']], [str(self.page.pk)])
        value = docs.execute('core_docs_read', {'document_id': str(self.page.pk)})
        self.assertEqual(value['source']['digest'], self.source.digest)
        self.assertIn('Customer Workspace', docs.export(self.page.pk, format='markdown'))
        self.assertEqual([json.loads(row) for row in docs.export(self.page.pk, format='jsonl').splitlines()], chunks(value))
        corrupted = copy.deepcopy(value)
        corrupted['organization_id'] = str(self.other.pk)
        with self.assertRaises(CoreServiceError):
            docs.verify(corrupted)
        self.installation.granted_capabilities = []
        self.installation.save()
        with self.assertRaises(CoreServiceError):
            docs.read(self.page.pk)
