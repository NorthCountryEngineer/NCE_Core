from django.db import migrations


def add_permission(apps, schema_editor):
    Installation = apps.get_model('platform_services', 'Installation')
    Permission = apps.get_model('identity', 'ServicePermission')
    for installation in Installation.objects.filter(package__slug='nce-robotics').iterator():
        Permission.objects.get_or_create(service_id=installation.service_id, codename='device-control',
            defaults={'name': 'Open authenticated direct robot connections'})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0046_robot_command_console')]
    operations = [migrations.RunPython(add_permission, migrations.RunPython.noop)]
