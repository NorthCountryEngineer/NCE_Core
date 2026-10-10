"""Register the shared user-tools guide for fresh and existing organizations."""
from importlib import import_module
from pathlib import Path
from django.db import migrations


def seed(apps, schema_editor):
    previous = import_module('platform_services.migrations.0022_documentation_catalog')
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Source = apps.get_model('platform_services', 'DocumentationSource')
    Page = apps.get_model('platform_services', 'DocumentationPage')
    Revision = apps.get_model('platform_services', 'DocumentationRevision')
    Installation = apps.get_model('platform_services', 'Installation')
    package = Package.objects.get(slug='core-platform')
    context = ('# {{platform_name}} · {{organization_name}}\n\n'
        'Application: **{{application_name}}** · Version: **{{application_version}}**\n\n'
        'Environment: **{{environment_name}}** · Namespace: **{{namespace}}**\n\n'
        'Support: {{support_email}}\n\n---\n\n')
    title = 'Shared application header, pins, and Chat'
    values = {'version': package.version, 'slug': 'unified-header', 'title': title, 'summary': title,
        'category': 'user-guide', 'audience': 'users', 'required_permissions': [],
        'markdown': context + (Path(__file__).resolve().parents[2] / 'docs/unified-header.md').read_text(), 'revision': 1}
    source, _ = Source.objects.get_or_create(package=package, version=package.version, slug=values['slug'], revision=1,
        defaults={**{key: value for key, value in values.items() if key not in {'version', 'slug', 'revision'}},
            'digest': previous.digest({'package_id': str(package.pk), **values})})
    for installation in Installation.objects.filter(package=package, version=source.version, enabled=True):
        page, created = Page.objects.get_or_create(organization_id=installation.organization_id,
            scope_key=f'{installation.pk}:-:-', slug=source.slug, defaults={'installation_id': installation.pk,
                'source': source, 'title': title, 'summary': title, 'category': source.category, 'audience': source.audience})
        if created:
            snapshot = {key: getattr(page, key) for key in ['title', 'summary', 'category', 'audience', 'required_permissions', 'mode', 'markdown', 'active']}
            snapshot['source_id'] = str(source.pk)
            Revision.objects.create(page=page, revision=page.revision, snapshot=snapshot, digest=previous.digest(snapshot))


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0030_installation_header_mode_applicationpin')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
