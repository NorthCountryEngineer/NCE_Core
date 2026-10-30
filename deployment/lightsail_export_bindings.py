"""Read only the restored Core graph; export nonsecret exact hosting bindings.

App/API/Amplify identifiers below came from the pre-migration AWS inventory.
Platform/environment/owner/origin/identity come from preserved Core records,
because those values must not be guessed from AWS resource names or primary
runtime organization env variables when an app has shared registrations.
"""
import hashlib
import json

from platform_services.cloud_contract import identity_contract
from platform_services.cloud_models import CloudApplication, CloudRelease


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


APPLICATIONS = {
    'd5ab80d1-6502-4f7d-a786-028651d6dc43': {
        'kind': 'proposal', 'api_url': 'https://api.proposalstudio.ai', 'amplify_app_id': 'd1f14xk20wy3vy'},
    'e72b972b-90a1-4c2d-ac2e-4a6e98886545': {
        'kind': 'academy', 'api_url': 'https://api.ncacademy.app', 'amplify_app_id': 'd370756z2vxc0d'},
}


bindings, runtimes, identities = {}, {}, {}
for identifier, known in APPLICATIONS.items():
    app = CloudApplication.objects.select_related('platform', 'environment', 'installation').get(pk=identifier)
    if CloudRelease.objects.filter(application=app, status__in=['draft', 'queued', 'running']).exists():
        raise RuntimeError('Resolve active application releases before enabling new hosting dispatch')
    expected = 'amplify-fargate' if known['kind'] == 'proposal' else 'amplify-academy-fargate'
    if app.archetype != expected or not app.installation_id:
        raise RuntimeError('Existing application does not match its preserved runtime contract')
    deployment = app.installation.deployments.select_related('application', 'resource_application').get(environment=app.environment, target='external')
    identity = identity_contract({key: app.installation.manifest[key] for key in ('callback_path', 'permissions')})
    runtime = {'kind': known['kind'], 'frontend': 'amplify', 'amplify_app_id': known['amplify_app_id'], 'amplify_branch': 'production'}
    value = {'platform_id': str(app.platform_id), 'organization_id': str(app.organization_id),
             'environment_id': str(app.environment_id), 'repository': app.repository, 'archetype': app.archetype,
             'origin': app.public_origin or deployment.endpoint, 'api_url': known['api_url'], 'identity': identity,
             'runtime_digest': digest(runtime)}
    bindings[identifier], runtimes[identifier] = value, runtime
    identities[identifier] = {'installation_id': str(app.installation_id), 'deployment_id': str(deployment.pk),
                              'service_id': str(app.installation.service_id), 'browser_client_id': deployment.application.client_id,
                              'current_source_commit': app.commit, 'canonical_origin': value['origin']}

print(json.dumps({'core_bindings': {'version': 1, 'applications': bindings, 'runner': {
    'host': '100.27.134.245', 'user': 'nce-release', 'port': 22, 'key_file': '/run/secrets/nce-release-key',
    'known_hosts_file': '/run/config/nce-release-known-hosts'}},
    'runner_bindings': {'version': 1, 'bindings': bindings, 'runtimes': runtimes, 'releases': {}},
    'preserved_public_identity_receipt': identities}, sort_keys=True, indent=2))
