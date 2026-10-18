from django.db import migrations, models

import identity.validators


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0009_service_auth_name_service_auth_tagline_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="avatar_url",
            field=models.URLField(blank=True, default="", db_default="", help_text="Optional public HTTPS image URL. Core stores the URL without downloading the image.", max_length=2048, validators=[identity.validators.validate_avatar_url]),
        ),
    ]
