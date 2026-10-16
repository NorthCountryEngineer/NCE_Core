from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0037_alter_cloudapplication_archetype')]
    operations = [migrations.AddField(model_name='cloudplatform', name='encrypted_ci_github_token',
                                     field=models.TextField(blank=True, editable=False))]
