from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("identity", "0010_user_avatar_url")]

    operations = [
        migrations.CreateModel(
            name="ApplicationConsent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(max_length=1000)),
                ("registration_fingerprint", models.CharField(max_length=64)),
                ("granted_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("application", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_consents", to="identity.application")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="application_consents", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("user", "application"), name="unique_user_application_consent")]},
        ),
    ]
