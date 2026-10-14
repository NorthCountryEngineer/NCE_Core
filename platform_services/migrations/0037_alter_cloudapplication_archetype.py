from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0036_cloudplatform_cloud_unique_aws_resource_prefix_and_more')]
    operations = [migrations.AlterField(
        model_name='cloudapplication', name='archetype',
        field=models.CharField(max_length=30, choices=[('amplify-static', 'Amplify website'),
            ('amplify-serverless', 'Amplify website and Lambda API'),
            ('amplify-fargate', 'Amplify website and Fargate application')]))]
