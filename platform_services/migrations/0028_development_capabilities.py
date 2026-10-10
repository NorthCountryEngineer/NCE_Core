from django.db import migrations

CAPABILITIES = ['development.read', 'development.write', 'development.run']


def register(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Permission = apps.get_model('identity', 'ServicePermission')
    Installation = apps.get_model('platform_services', 'Installation')
    package = Package.objects.get(slug='core-workflows')
    package.description = 'Durable agents and composable development objects, data fusions, and local ML experiments.'
    package.manifest = {**package.manifest, 'capabilities': list(dict.fromkeys(package.manifest.get('capabilities', []) + CAPABILITIES))}
    package.save(update_fields=['manifest', 'description'])
    for installation in Installation.objects.filter(package=package):
        installation.manifest = {**installation.manifest, 'capabilities': list(dict.fromkeys(installation.manifest.get('capabilities', []) + CAPABILITIES))}
        installation.save(update_fields=['manifest'])
        for capability in CAPABILITIES:
            Permission.objects.get_or_create(service_id=installation.service_id, codename=capability.replace('.', '-'), defaults={'name': capability})
    # Existing workload, agent, and human role grants are deliberately unchanged.


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0027_development_objects')]
    operations = [migrations.RunPython(register, migrations.RunPython.noop)]
