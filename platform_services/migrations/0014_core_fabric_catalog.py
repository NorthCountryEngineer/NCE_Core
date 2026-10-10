from django.db import migrations


def install_fabric(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Service = apps.get_model('identity', 'Service')
    Permission = apps.get_model('identity', 'ServicePermission')
    capabilities = ['fabric.read', 'fabric.write', 'fabric.manage', 'fabric.peer']
    manifest = {'dependencies': ['core-data'], 'capabilities': capabilities}
    package, _ = Package.objects.get_or_create(slug='core-fabric', defaults={'name': 'Core Data Fabric', 'kind': 'core',
        'version': '0.1.0', 'description': 'Bilateral administrator-approved data federation between Core deployments.', 'manifest': manifest})
    for core in Installation.objects.filter(package__slug='core-platform').iterator():
        service, _ = Service.objects.get_or_create(organization_id=core.organization_id, slug='core-fabric', defaults={'name': 'Core Data Fabric'})
        for capability in capabilities:
            Permission.objects.get_or_create(service=service, codename=capability.replace('.', '-'), defaults={'name': capability})
        installation, created = Installation.objects.get_or_create(organization_id=core.organization_id, package=package,
            defaults={'service': service, 'version': package.version, 'manifest': manifest, 'granted_capabilities': []})
        if created:
            parent = Deployment.objects.filter(installation=core, target__in=['embedded', 'kubernetes-controller']).first()
            Deployment.objects.create(installation=installation, name='core-fabric', target='embedded', parent=parent,
                endpoint=parent.endpoint if parent else '')


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0013_datarecord_federation_alter_agenttask_resources')]
    operations = [migrations.RunPython(install_fabric, migrations.RunPython.noop)]
