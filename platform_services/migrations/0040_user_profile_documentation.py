"""Seed the profile guide without replacing organization edits or source pins."""
from importlib import import_module
from pathlib import Path

from django.db import migrations


def seed(apps, schema_editor):
    digest = import_module('platform_services.migrations.0022_documentation_catalog').digest
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Source = apps.get_model('platform_services', 'DocumentationSource')
    Page = apps.get_model('platform_services', 'DocumentationPage')
    Revision = apps.get_model('platform_services', 'DocumentationRevision')
    Installation = apps.get_model('platform_services', 'Installation')
    database = schema_editor.connection.alias if schema_editor else 'default'
    package = Package.objects.using(database).get(slug='core-platform')
    context = ('# {{platform_name}} · {{organization_name}}\n\n'
        'Application: **{{application_name}}** · Version: **{{application_version}}**\n\n'
        'Environment: **{{environment_name}}** · Namespace: **{{namespace}}**\n\n'
        'Support: {{support_email}}\n\n---\n\n')
    title = 'Your Core account profile'
    values = {'version': package.version, 'slug': 'user-profile', 'title': title, 'summary': title,
        'category': 'user-guide', 'audience': 'users', 'required_permissions': [],
        'markdown': context + (Path(__file__).resolve().parents[2] / 'docs/user-profile.md').read_text(), 'revision': 1}
    Source.objects.using(database).get_or_create(package=package, version=package.version, slug=values['slug'], revision=1,
        defaults={**{key: value for key, value in values.items() if key not in {'version', 'slug', 'revision'}},
            'digest': digest({'package_id': str(package.pk), **values})})
    source = Source.objects.using(database).filter(package=package, version=package.version,
        slug=values['slug'], active=True).order_by('-revision').first()
    if source is None:
        return
    for installation in Installation.objects.using(database).filter(package=package, version=source.version, enabled=True):
        page, created = Page.objects.using(database).get_or_create(organization_id=installation.organization_id,
            scope_key=f'{installation.pk}:-:-', slug=source.slug, defaults={'installation_id': installation.pk,
                'source': source, 'title': source.title, 'summary': source.summary,
                'category': source.category, 'audience': source.audience})
        if created:
            snapshot = {key: getattr(page, key) for key in [
                'title', 'summary', 'category', 'audience', 'required_permissions', 'mode', 'markdown', 'active']}
            snapshot['source_id'] = str(source.pk)
            Revision.objects.using(database).get_or_create(page=page, revision=page.revision,
                defaults={'snapshot': snapshot, 'digest': digest(snapshot)})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0039_academy_fargate_archetype')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
