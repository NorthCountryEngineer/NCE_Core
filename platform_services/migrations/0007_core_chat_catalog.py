from django.db import migrations


def install_chat(apps, schema_editor):
    Package = apps.get_model('platform_services', 'MarketplacePackage')
    Installation = apps.get_model('platform_services', 'Installation')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Service = apps.get_model('identity', 'Service')
    Permission = apps.get_model('identity', 'ServicePermission')
    capabilities = ['chat.read', 'chat.send', 'chat.manage', 'chat.ai', 'chat.federate']
    manifest = {'dependencies': [], 'capabilities': capabilities}
    package, _ = Package.objects.get_or_create(slug='core-chat', defaults={'name': 'Core Chat', 'kind': 'core',
        'version': '0.1.0', 'description': 'Shared conversations and permission-checked AI messaging across trusted Core instances.',
        'manifest': manifest})
    for core in Installation.objects.filter(package__slug='core-platform').iterator():
        service, _ = Service.objects.get_or_create(organization_id=core.organization_id, slug='core-chat', defaults={'name': 'Core Chat'})
        for capability in capabilities:
            Permission.objects.get_or_create(service=service, codename=capability.replace('.', '-'), defaults={'name': capability})
        installation, created = Installation.objects.get_or_create(organization_id=core.organization_id, package=package,
            defaults={'service': service, 'version': package.version, 'manifest': manifest, 'granted_capabilities': []})
        if created:
            parent = Deployment.objects.filter(installation=core, target__in=['embedded', 'kubernetes-controller']).first()
            Deployment.objects.create(installation=installation, name='core-chat', target='embedded', parent=parent,
                                      endpoint=parent.endpoint if parent else '')


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0006_alter_agentdefinition_kind')]
    operations = [migrations.RunPython(install_chat, migrations.RunPython.noop)]
