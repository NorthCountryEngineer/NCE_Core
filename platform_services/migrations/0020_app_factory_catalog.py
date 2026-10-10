from django.db import migrations


def seed_factory(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Service = apps.get_model('identity', 'Service')
    Permission = apps.get_model('identity', 'ServicePermission')
    manifest = {'dependencies': ['core-agents', 'core-workflows'],
                'capabilities': ['appfactory.create', 'appfactory.read', 'appfactory.build', 'appfactory.release']}
    package, _ = Package.objects.get_or_create(slug='core-app-factory', defaults={'name': 'Core App Factory',
        'kind': 'core', 'version': '0.1.0', 'description': 'Description-led application development with scoped agents and inherited platform security.',
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
    for role in ['plan', 'backend', 'frontend', 'security', 'test', 'package']:
        apps.get_model('platform_services', 'AgentListing').objects.get_or_create(slug=f'app-developer-{role}', defaults={
            'name': f'App Developer: {role}', 'version': '0.1.0', 'kind': 'inspect',
            'description': 'App Factory creates a separate account and approved project-scoped tool from this listing. Direct installation grants no development permission.'})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0019_alter_agentdefinition_kind_alter_agentlisting_kind_and_more')]
    operations = [migrations.RunPython(seed_factory, migrations.RunPython.noop)]
