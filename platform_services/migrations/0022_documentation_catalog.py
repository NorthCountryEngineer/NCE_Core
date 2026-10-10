"""Seed initial documentation using historical models; retain existing access grants."""
import hashlib
import json
from pathlib import Path

from django.db import migrations


INITIAL_GUIDES = [
    ('core-platform', 'getting-started', 'Organization getting started', 'getting-started', 'users', 'docs/organization-guide.md'),
    ('core-platform', 'platform', 'Platform administration and onboarding', 'administration', 'administrators', 'docs/platform.md'),
    ('core-platform', 'security', 'Identity, roles, and classification controls', 'security', 'administrators', 'docs/security.md'),
    ('core-platform', 'deployment', 'Deploy, operate, and recover Core', 'operations', 'administrators', 'docs/deployment.md'),
    ('core-platform', 'modernization', 'Environment isolation and application modernization', 'architecture', 'administrators', 'docs/modernization.md'),
    ('core-platform', 'cicd', 'CI/CD configuration and release controls', 'operations', 'administrators', 'docs/cicd.md'),
    ('core-platform', 'integration', 'OIDC and application security integration', 'api', 'administrators', 'docs/integration.md'),
    ('core-platform', 'application-integrations', 'Shared and application-owned integrations', 'api', 'administrators', 'docs/application-integrations.md'),
    ('core-platform', 'local-testing', 'Local development and troubleshooting', 'troubleshooting', 'administrators', 'docs/local-testing.md'),
    ('core-platform', 'documentation', 'Documentation inheritance and AI consumption', 'administration', 'administrators', 'docs/organization-documentation.md'),
    ('core-data', 'data-api', 'Core data ingestion and consumption', 'api', 'administrators', 'docs/platform.md'),
    ('core-agents', 'agent-accounts', 'Agent accounts, marketplace, and task permissions', 'administration', 'administrators', 'docs/agent-accounts.md'),
    ('core-workflows', 'workflows', 'Durable agent workflows', 'operations', 'administrators', 'docs/platform.md'),
    ('core-chat', 'chat', 'Central and federated chat', 'user-guide', 'administrators', 'docs/chat.md'),
    ('core-training', 'training', 'Synthetic training and evaluations', 'user-guide', 'administrators', 'docs/training.md'),
    ('core-fabric', 'data-fabric', 'Bilateral data federation', 'architecture', 'administrators', 'docs/data-fabric.md'),
    ('core-app-factory', 'app-factory', 'AI-led application creation', 'user-guide', 'administrators', 'docs/app-factory.md'),
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def seed_documentation(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Source = apps.get_model('platform_services', 'DocumentationSource')
    Page = apps.get_model('platform_services', 'DocumentationPage')
    Revision = apps.get_model('platform_services', 'DocumentationRevision')
    Permission = apps.get_model('identity', 'ServicePermission')
    platform = Package.objects.get(slug='core-platform')
    capabilities = ['docs.read', 'docs.admin', 'docs.manage']
    platform.manifest = {**platform.manifest, 'capabilities': list(dict.fromkeys(platform.manifest.get('capabilities', []) + capabilities))}
    platform.save(update_fields=['manifest'])
    for installation in Installation.objects.filter(package=platform):
        installation.manifest = {**installation.manifest, 'capabilities': list(dict.fromkeys(installation.manifest.get('capabilities', []) + capabilities))}
        installation.save(update_fields=['manifest'])
        for capability in capabilities:
            Permission.objects.get_or_create(service_id=installation.service_id, codename=capability.replace('.', '-'), defaults={'name': capability})
    root = Path(__file__).resolve().parents[2]
    context = ('# {{platform_name}} · {{organization_name}}\n\n'
        'Application: **{{application_name}}** · Version: **{{application_version}}**\n\n'
        'Environment: **{{environment_name}}** · Namespace: **{{namespace}}**\n\n'
        'Support: {{support_email}}\n\n---\n\n')
    for package_slug, slug, title, category, audience, filename in INITIAL_GUIDES:
        package = Package.objects.get(slug=package_slug)
        values = {'version': package.version, 'slug': slug, 'title': title, 'summary': title, 'category': category,
            'audience': audience, 'required_permissions': [], 'markdown': context + (root / filename).read_text(), 'revision': 1}
        source, _ = Source.objects.get_or_create(package=package, version=package.version, slug=slug, revision=1,
            defaults={**{key: value for key, value in values.items() if key not in {'version', 'slug', 'revision'}},
                'digest': digest({'package_id': str(package.pk), **values})})
        for installation in Installation.objects.filter(package=package, version=source.version, enabled=True):
            page, created = Page.objects.get_or_create(organization_id=installation.organization_id,
                scope_key=f'{installation.pk}:-:-', slug=source.slug, defaults={
                    'installation_id': installation.pk, 'source': source, 'title': source.title,
                    'summary': source.summary, 'category': source.category, 'audience': source.audience})
            if created:
                snapshot = {key: getattr(page, key) for key in ['title', 'summary', 'category', 'audience', 'required_permissions', 'mode', 'markdown', 'active']}
                snapshot['source_id'] = str(source.pk)
                Revision.objects.create(page=page, revision=page.revision, snapshot=snapshot, digest=digest(snapshot))
    for installation in Installation.objects.all():
        Permission.objects.get_or_create(service_id=installation.service_id, codename='docs-read', defaults={'name': 'Read application documentation'})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0021_organization_documentation')]
    operations = [migrations.RunPython(seed_documentation, migrations.RunPython.noop)]
