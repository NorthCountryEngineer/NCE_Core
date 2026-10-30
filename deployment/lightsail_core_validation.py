"""Read-only post-restore Core validation; emit counts/fingerprints, no secrets.

Run with the normal preserved production keys/database through the Core launcher:
    ... manage shell -c 'exec(open("deployment/lightsail_core_validation.py").read())'
This does not replace real browser authentication and integrated-app acceptance.
"""
import hashlib
import json
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from platform_services.secrets import decrypt_bundle


def validate():
    if settings.DEBUG or settings.ISSUER != 'https://core.nnytech.ai':
        raise RuntimeError('Production configuration does not match the canonical preserved Core issuer')
    call_command('check', '--deploy', '--fail-level', 'WARNING', verbosity=0)
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if pending:
        raise RuntimeError('The deployed image has unapplied database migrations')
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_database(), current_user, version()')
        database, user, version = cursor.fetchone()
    encrypted = {}
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if field.name.startswith('encrypted_'):
                count = 0
                for value in model.objects.exclude(**{field.name: ''}).exclude(**{field.name: None}).values_list(field.name, flat=True).iterator():
                    decrypt_bundle(value)
                    count += 1
                encrypted[model._meta.label + '.' + field.name] = count
    counts = {}
    for label in ('identity.User', 'identity.Organization', 'identity.Membership', 'identity.Role', 'identity.Application',
                  'platform_services.Installation', 'platform_services.Deployment', 'platform_services.CloudApplication',
                  'platform_services.CloudRelease', 'platform_services.IntegrationConnection', 'platform_services.ApplicationIntegration'):
        counts[label] = apps.get_model(label).objects.count()
    fingerprints = {'django_key_sha256': hashlib.sha256(settings.SECRET_KEY.encode()).hexdigest(),
                    'oidc_key_sha256': hashlib.sha256(settings.OIDC_PRIVATE_KEY.encode()).hexdigest()}
    if getattr(settings, 'CORE_ORIGIN_SECRET', ''):
        fingerprints['origin_key_sha256'] = hashlib.sha256(settings.CORE_ORIGIN_SECRET.encode()).hexdigest()
    return {'mode': 'read-only-production-validation', 'issuer': settings.ISSUER, 'database': database,
            'database_user': user, 'database_version': version, 'pending_migrations': 0, 'counts': counts,
            'decrypted_credential_counts': encrypted, 'key_fingerprints': fingerprints,
            'browser_authentication_verified': False}


print(json.dumps(validate(), sort_keys=True, indent=2))
