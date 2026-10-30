"""Read-only exact current commit/public registration export with live CI gate.

Run through Core's management shell. Exports only public build inputs and CI
booleans; no resource bundle, provider secret, token or external ID is included.
"""
import json
from django.db import connection, transaction
from platform_services.cloud_models import CloudApplication
from platform_services.cloud_deployments import _snapshot
from platform_services.cloud_aws import require_ci
from platform_services.external_apps import external_public_configuration

APPS = {'d5ab80d1-6502-4f7d-a786-028651d6dc43': 'https://api.proposalstudio.ai',
        'e72b972b-90a1-4c2d-ac2e-4a6e98886545': 'https://api.ncacademy.app'}
values = {}
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
    for identifier, api_url in APPS.items():
        app = CloudApplication.objects.select_related('platform', 'environment', 'installation').get(pk=identifier)
        snap = _snapshot(app)
        require_ci(app.platform, snap)
        deployment = app.installation.deployments.select_related('application', 'resource_application').get(
            environment=app.environment, target='external')
        public = external_public_configuration(deployment)
        config = {'VITE_CORE_ISSUER': public['issuer'], 'VITE_CORE_ORGANIZATION_ID': public['organization_id'],
                  'VITE_CORE_SERVICE_ID': public['service_id'], 'VITE_CORE_BROWSER_CLIENT_ID': public['browser_client_id'],
                  'VITE_APPLICATION_ORIGIN': app.public_origin, 'VITE_API_URL': api_url,
                  'VITE_CORE_REDIRECT_URI': public['redirect_uri'], 'VITE_CORE_DEPLOYMENT_ID': public['deployment_id']}
        values[identifier] = {'ci_admission_passed': True, 'snapshot': {key: snap[key] for key in (
            'app_identifier', 'platform_id', 'organization_id', 'environment_id', 'repository', 'commit', 'archetype')},
            'public_config': config}
print(json.dumps({'read_only': True, 'application_data_changed': False, 'candidates': values}, sort_keys=True))
