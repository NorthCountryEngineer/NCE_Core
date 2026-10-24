from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0041_employee_directory_documentation')]
    operations = [migrations.AlterField(
        model_name='cloudapplication', name='archetype',
        field=models.CharField(max_length=30, choices=[('amplify-static', 'Amplify website'),
            ('amplify-serverless', 'Amplify website and Lambda API'),
            ('amplify-fargate', 'Amplify website and Fargate application'),
            ('amplify-academy-fargate', 'Academy website and Fargate API'),
            ('amplify-game-studio', 'Game Studio website and serverless API')]))]
