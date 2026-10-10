from django.db import migrations


def backfill(apps, schema_editor):
    Config = apps.get_model('platform_services', 'DeploymentConfiguration')
    Environment = apps.get_model('platform_services', 'DeploymentEnvironment')
    Node = apps.get_model('platform_services', 'RuntimeNode')
    Deployment = apps.get_model('platform_services', 'Deployment')
    Credential = apps.get_model('platform_services', 'WorkloadCredential')
    Tag = apps.get_model('identity', 'PermissionTag')
    alias = schema_editor.connection.alias
    for config in Config.objects.using(alias).all().iterator():
        slug = 'environment-' + config.environment
        if Tag.objects.using(alias).filter(organization_id=config.organization_id, slug=slug).exists():
            # Preserve an existing customer tag rather than repurpose its policy.
            slug += '-' + str(config.organization_id)[:8]
        tag = Tag.objects.using(alias).create(organization_id=config.organization_id, slug=slug,
            name=config.environment.title() + ' environment', allow_workloads=True)
        environment = Environment.objects.using(alias).create(organization_id=config.organization_id, name=config.environment,
            namespace=config.namespace, application_origin=config.application_origin, ingress_class=config.ingress_class,
            tls_secret_name=config.tls_secret_name, auto_deploy=config.auto_deploy, boundary_tag_id=tag.pk)
        Node.objects.using(alias).filter(organization_id=config.organization_id, namespace=config.namespace).update(environment_id=environment.pk)
        Deployment.objects.using(alias).filter(installation__organization_id=config.organization_id, target='embedded').update(environment_id=environment.pk)
        for deployment in Deployment.objects.using(alias).filter(node__environment_id=environment.pk).iterator():
            deployment.environment_id = environment.pk
            deployment.save(using=alias, update_fields=['environment'])
            Credential.objects.using(alias).filter(installation_id=deployment.installation_id,
                name='deployment:' + str(deployment.pk)).update(environment_id=environment.pk, deployment_id=deployment.pk)


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0015_deployment_environments_and_modernization')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
