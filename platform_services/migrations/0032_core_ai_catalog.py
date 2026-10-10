"""Install the retrieval foundation without granting new human or workload access."""
from django.db import migrations


def register(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Service = apps.get_model('identity', 'Service')
    Permission = apps.get_model('identity', 'ServicePermission')
    manifest = {'dependencies': ['core-data', 'core-fabric'], 'capabilities': ['ai.query', 'ai.manage']}
    package, _ = Package.objects.get_or_create(slug='core-ai', defaults={'name': 'Core AI', 'kind': 'core',
        'version': '0.1.0', 'description': 'Permission-aware unified application queries and evidence-first conversations; no provider key required.',
        'manifest': manifest})
    for core in Installation.objects.filter(package__slug='core-platform').iterator():
        service, _ = Service.objects.get_or_create(organization_id=core.organization_id, slug=package.slug, defaults={'name': package.name})
        for capability in manifest['capabilities']:
            Permission.objects.get_or_create(service=service, codename=capability.replace('.', '-'), defaults={'name': capability})
        installation, created = Installation.objects.get_or_create(organization_id=core.organization_id, package=package,
            defaults={'service': service, 'version': package.version, 'manifest': manifest, 'granted_capabilities': []})
        if created:
            parent = Deployment.objects.filter(installation=core, target__in=['embedded', 'kubernetes-controller']).first()
            Deployment.objects.create(installation=installation, name=package.slug, target='embedded', parent=parent,
                endpoint=parent.endpoint if parent else '')


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0031_header_documentation'), ('core_ai', '0001_initial'),
        ('core_fabric', '0002_fabriccontract_allowed_memberships_and_more')]
    operations = [migrations.RunPython(register, migrations.RunPython.noop)]
