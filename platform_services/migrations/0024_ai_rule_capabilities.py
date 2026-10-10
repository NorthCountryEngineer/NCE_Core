from django.db import migrations

CAPABILITIES = ['rules.read', 'rules.manage', 'usage.reserve', 'usage.report', 'usage.read']


def register(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Permission = apps.get_model('identity', 'ServicePermission')
    Installation = apps.get_model('platform_services', 'Installation')
    package = Package.objects.get(slug='core-platform')
    package.manifest = {**package.manifest, 'capabilities': list(dict.fromkeys(package.manifest.get('capabilities', []) + CAPABILITIES))}
    package.save(update_fields=['manifest'])
    # Register permissions without granting new capabilities to existing workloads or agent roles.
    for installation in Installation.objects.filter(package=package):
        installation.manifest = {**installation.manifest, 'capabilities': list(dict.fromkeys(installation.manifest.get('capabilities', []) + CAPABILITIES))}
        installation.save(update_fields=['manifest'])
        for capability in CAPABILITIES:
            Permission.objects.get_or_create(service_id=installation.service_id, codename=capability.replace('.', '-'),
                defaults={'name': capability.replace('.', ' ').title()})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0023_ai_rules_and_shared_usage')]
    operations = [migrations.RunPython(register, migrations.RunPython.noop)]
