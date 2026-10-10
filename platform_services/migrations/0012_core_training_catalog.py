from django.db import migrations


def install_training(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Service = apps.get_model('identity', 'Service')
    Permission = apps.get_model('identity', 'ServicePermission')
    capabilities = ['training.run', 'training.read', 'training.manage', 'training.evaluate']
    manifest = {'dependencies': ['core-data', 'core-agents', 'core-workflows'], 'capabilities': capabilities}
    package, _ = Package.objects.get_or_create(slug='core-training', defaults={'name': 'Core Training', 'kind': 'core',
        'version': '0.1.0', 'description': 'Agent-led simulated pipeline data and reproducible training evaluations.', 'manifest': manifest})
    for core in Installation.objects.filter(package__slug='core-platform').iterator():
        service, _ = Service.objects.get_or_create(organization_id=core.organization_id, slug='core-training', defaults={'name': 'Core Training'})
        for capability in capabilities:
            Permission.objects.get_or_create(service=service, codename=capability.replace('.', '-'), defaults={'name': capability})
        installation, created = Installation.objects.get_or_create(organization_id=core.organization_id, package=package,
            defaults={'service': service, 'version': package.version, 'manifest': manifest, 'granted_capabilities': []})
        if created:
            parent = Deployment.objects.filter(installation=core, target__in=['embedded', 'kubernetes-controller']).first()
            Deployment.objects.create(installation=installation, name='core-training', target='embedded', parent=parent,
                endpoint=parent.endpoint if parent else '')
    apps.get_model('platform_services', 'AgentListing').objects.get_or_create(slug='training-coordinator', defaults={
        'name': 'Training Coordinator', 'kind': 'inspect', 'version': '0.1.0',
        'description': 'Agent account for AI scenario planning, synthetic-data simulations, result review, and evaluation feedback. Approve its application and service roles before use.'})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0011_datarecord_synthetic_datarecord_training_run_id_and_more')]
    operations = [migrations.RunPython(install_training, migrations.RunPython.noop)]
