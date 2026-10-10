"""Seed the reviewed Core AI guide; no credentials or provider calls."""
from importlib import import_module
from pathlib import Path
from django.db import migrations


def seed(apps, schema_editor):
    previous = import_module('platform_services.migrations.0022_documentation_catalog')
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Source = apps.get_model('platform_services', 'DocumentationSource')
    package = Package.objects.get(slug='core-ai')
    context = ('# {{platform_name}} · {{organization_name}}\n\n'
        'Application: **{{application_name}}** · Version: **{{application_version}}**\n\n'
        'Environment: **{{environment_name}}** · Namespace: **{{namespace}}**\n\n'
        'Support: {{support_email}}\n\n---\n\n')
    title = 'Query accessible application data with Core AI'
    values = {'version': package.version, 'slug': 'central-ai', 'title': title, 'summary': title,
        'category': 'user-guide', 'audience': 'users', 'required_permissions': [],
        'markdown': context + (Path(__file__).resolve().parents[2] / 'docs/central-ai.md').read_text(), 'revision': 1}
    Source.objects.get_or_create(package=package, version=package.version, slug='central-ai', revision=1,
        defaults={**{key: value for key, value in values.items() if key not in {'version', 'slug', 'revision'}},
            'digest': previous.digest({'package_id': str(package.pk), **values})})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0032_core_ai_catalog')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
