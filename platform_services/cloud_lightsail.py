"""Bounded SSH transport for operator-owned, existing Lightsail hosting.

Application source/browser inputs cannot select a host, command, path or service.
The web process reads only public bindings. Only the worker mounts the SSH key.
"""
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from django.conf import settings

from .cloud_aws import AWSAdapterError, _snapshot, encode_resource_credentials, require_ci

FIELDS = {'platform_id', 'organization_id', 'environment_id', 'repository', 'archetype',
          'origin', 'api_url', 'identity', 'runtime_digest'}
RUNNER_FIELDS = {'host', 'user', 'port', 'key_file', 'known_hosts_file'}
REMOTE_COMMAND = 'sudo -n /usr/local/sbin/nce-lightsail-release-protocol'
FARGATE = {'amplify-fargate', 'amplify-academy-fargate'}


class _NoHealthRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def current_hosting(app, *, opener=None):
    """Read current fixed hosting; historic AWS receipts remain audit evidence."""
    snapshot = {'app_identifier': str(app.pk), 'platform_id': str(app.platform_id),
                'organization_id': str(app.organization_id), 'environment_id': str(app.environment_id),
                'repository': app.repository, 'archetype': app.archetype, 'public_origin': app.public_origin}
    binding = binding_for(app.platform, snapshot, reviewed=False)
    if not binding:
        return None
    result = {'provider': 'Lightsail', 'status': 'Unknown', 'origin': binding['origin'], 'api_url': binding['api_url'],
              'checked_at': datetime.now(timezone.utc).isoformat(), 'services': [], 'container_report': 'Unknown'}
    kind = 'academy' if binding['archetype'] == 'amplify-academy-fargate' else 'proposal'
    names = ('academy-api',) if kind == 'academy' else ('proposal-api', 'proposal-worker', 'sam-browser')
    try:
        filename = Path(settings.CORE_LIGHTSAIL_STATUS_FILE)
        if filename.stat().st_size > 16384:
            raise ValueError
        report = json.loads(filename.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report['checked_at'])).total_seconds()
        if report['version'] != 1 or not -5 <= age <= 120:
            raise ValueError
        for name in names:
            value = report['services'][name]
            state, health = value['state'], value['health']
            if state not in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead', 'unavailable', 'unknown'} or health not in {'starting', 'healthy', 'unhealthy', 'unknown'}:
                raise ValueError
            result['services'].append({'name': name, 'state': state, 'health': health})
        result['container_report'] = 'Current'
        result['container_checked_at'] = report['checked_at']
        if report['maintenance'][kind] is True:
            result['status'] = 'Maintenance'
        elif all(value['state'] == 'running' and value['health'] == 'healthy' for value in result['services']):
            result['status'] = 'Healthy'
        else:
            result['status'] = 'Degraded'
    except (OSError, ValueError, TypeError, KeyError):
        result['services'] = []
        result['container_report'] = 'Unavailable or stale'
    try:
        open_url = opener or build_opener(ProxyHandler({}), _NoHealthRedirect()).open
        request = Request(binding['api_url'] + '/api/ready', headers={'Accept': 'application/json', 'User-Agent': 'NCE-Core-readiness'})
        with open_url(request, timeout=3) as response:
            result['api_readiness'] = 'Ready' if response.status == 200 else 'Unavailable'
    except HTTPError as error:
        result['api_readiness'] = 'Maintenance' if error.code == 503 else 'Unavailable'
    except (URLError, OSError, TimeoutError):
        result['api_readiness'] = 'Unavailable'
    if result['api_readiness'] == 'Maintenance':
        result['status'] = 'Maintenance'
    elif result['api_readiness'] != 'Ready' and result['status'] == 'Healthy':
        result['status'] = 'Degraded'
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def configuration():
    filename = getattr(settings, 'CORE_LIGHTSAIL_BINDINGS_FILE', '')
    if not filename:
        return {'version': 1, 'applications': {}, 'runner': {}}
    try:
        path = Path(filename)
        if path.stat().st_size > 131072:
            raise ValueError
        value = json.loads(path.read_text())
        if (set(value) != {'version', 'applications', 'runner'} or value['version'] != 1
                or not isinstance(value['applications'], dict) or len(value['applications']) > 20
                or not isinstance(value['runner'], dict) or set(value['runner']) != RUNNER_FIELDS):
            raise ValueError
        runner = value['runner']
        if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', runner['host'])
                or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', runner['user'])
                or not isinstance(runner['port'], int) or not 1 <= runner['port'] <= 65535
                or any(not isinstance(runner[key], str) or not runner[key].startswith('/')
                       or '\x00' in runner[key] for key in ('key_file', 'known_hosts_file'))):
            raise ValueError
        for app, binding in value['applications'].items():
            if not _uuid(app):
                raise ValueError
            validate_binding(binding)
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise AWSAdapterError('The operator Lightsail hosting bindings are invalid or unavailable.') from None


def validate_binding(binding):
    from .cloud_contract import identity_contract
    if (not isinstance(binding, dict) or set(binding) != FIELDS
            or any(not _uuid(binding[key]) for key in ('platform_id', 'organization_id', 'environment_id'))
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', binding['repository'])
            or binding['archetype'] not in FARGATE
            or not isinstance(binding['runtime_digest'], str) or not re.fullmatch(r'[a-f0-9]{64}', binding['runtime_digest'])
            or any(not re.fullmatch(r'https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', binding[key])
                   for key in ('origin', 'api_url'))):
        raise ValueError('Invalid public hosting binding')
    identity_contract(binding['identity'])
    return binding


def binding_for(platform, snapshot, *, reviewed=True):
    config = configuration()
    binding = config['applications'].get(snapshot.get('app_identifier'))
    if not binding:
        if (snapshot.get('archetype') in FARGATE
                and getattr(settings, 'CORE_BLOCK_UNBOUND_FARGATE_DEPLOYMENTS', False)):
            raise AWSAdapterError('This container application requires an operator Lightsail hosting binding.')
        return None
    expected = {key: snapshot.get(key) for key in ('organization_id', 'environment_id', 'repository', 'archetype')}
    expected['platform_id'] = str(platform.pk) if hasattr(platform, 'pk') else snapshot.get('platform_id')
    if any(binding[key] != value for key, value in expected.items()):
        raise AWSAdapterError('Lightsail hosting does not match this exact application placement.')
    if snapshot.get('public_origin') and snapshot['public_origin'] != binding['origin']:
        raise AWSAdapterError('Lightsail hosting would change the canonical application origin.')
    if reviewed and snapshot.get('hosting_binding_digest') != digest(binding):
        raise AWSAdapterError('Lightsail hosting changed. Review a new deployment plan.')
    return binding


def hosting_fingerprint(app):
    snapshot = {'app_identifier': str(app.pk), 'platform_id': str(app.platform_id),
                'organization_id': str(app.organization_id), 'environment_id': str(app.environment_id),
                'repository': app.repository, 'archetype': app.archetype, 'public_origin': app.public_origin}
    binding = binding_for(app.platform, snapshot, reviewed=False)
    return digest(binding) if binding else ''


def _token(key):
    return hashlib.sha256(str(key).encode()).hexdigest()


def _state(snapshot, token):
    return {'build_id': 'lightsail:' + token, 'run_token': token, 'provider': 'lightsail',
            'phase': snapshot.get('action', 'provision'), 'hosting_binding_digest': snapshot['hosting_binding_digest']}


def _transport(operation, snapshot, token, *, credentials=None):
    runner = configuration()['runner']
    payload = {'version': 1, 'operation': operation, 'token': token, 'snapshot': {key: snapshot.get(key) for key in (
        'app_identifier', 'platform_id', 'organization_id', 'environment_id', 'repository', 'commit', 'archetype',
        'action', 'plan_hash', 'public_config', 'hosting_binding_digest')}}
    if credentials is not None:
        payload['credentials'] = credentials
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode()) > 32768:
        raise AWSAdapterError('Lightsail deployment request exceeds its bounded transport limit.')
    arguments = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                 '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR', '-o', 'UserKnownHostsFile=' + runner['known_hosts_file'],
                 '-i', runner['key_file'], '-p', str(runner['port']), runner['user'] + '@' + runner['host'], REMOTE_COMMAND]
    try:
        result = subprocess.run(arguments, input=encoded, capture_output=True, text=True, timeout=45, check=False)
        if result.returncode or len(result.stdout.encode()) > 65536:
            raise ValueError
        response = json.loads(result.stdout)
        if (not isinstance(response, dict) or response.get('version') != 1 or response.get('token') != token
                or response.get('commit') != snapshot['commit'] or response.get('action') != snapshot.get('action', 'provision')
                or response.get('plan_hash') != snapshot.get('plan_hash')):
            raise ValueError
        return response
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # SSH/runner diagnostics may contain credentials, paths or deployment output.
        raise AWSAdapterError('The fixed Lightsail deployment runner could not confirm this exact request.') from None


def start_run(platform, snapshot, idempotency_key):
    binding_for(platform, snapshot)
    _snapshot(platform, snapshot)
    require_ci(platform, snapshot)
    token = _token(idempotency_key)
    response = _transport('start', snapshot, token)
    if response.get('status') not in {'running', 'succeeded'}:
        raise AWSAdapterError('The fixed Lightsail runner refused this reviewed release. Inspect its bounded source build and hosting configuration.')
    return {**_state(snapshot, token), 'status': 'running'}


def recover_run(platform, snapshot, idempotency_key):
    binding_for(platform, snapshot)
    token = _token(idempotency_key)
    response = _transport('recover', snapshot, token)
    if response.get('status') not in {'running', 'succeeded', 'failed', 'cancelled'}:
        raise AWSAdapterError('Lightsail start outcome is ambiguous. Inspect the recorded token before another release.')
    return _state(snapshot, token)


def _validated_state(snapshot, state):
    token = state.get('run_token', '')
    if (not re.fullmatch(r'[a-f0-9]{64}', token) or state.get('build_id') != 'lightsail:' + token
            or state.get('hosting_binding_digest') != snapshot.get('hosting_binding_digest')):
        raise AWSAdapterError('Invalid Lightsail runner state.')
    return token


def poll_run(platform, snapshot, provider_state):
    binding = binding_for(platform, snapshot)
    token = _validated_state(snapshot, provider_state)
    response = _transport('poll', snapshot, token)
    status = response.get('status')
    if status not in {'running', 'succeeded', 'failed', 'cancelled'}:
        raise AWSAdapterError('The Lightsail runner returned an invalid release status.')
    outputs = {}
    if status == 'succeeded':
        expected = {'origin': binding['origin'], 'api_url': binding['api_url'], 'commit': snapshot['commit'],
                    'identity': json.dumps(binding['identity'], sort_keys=True, separators=(',', ':'))}
        if response.get('outputs') != expected:
            raise AWSAdapterError('The Lightsail receipt does not match the approved hosting and identity contract.')
        outputs = expected
    return {'status': 'failed' if status == 'cancelled' else status, 'outputs': outputs,
            'steps': [{'name': 'lightsail-runner', 'status': 'failed' if status in {'failed', 'cancelled'} else status,
                       'message': 'Fixed Lightsail deployment runner.'}], 'provider_state': dict(provider_state)}


def stop_run(platform, snapshot, provider_state):
    binding_for(platform, snapshot)
    token = _validated_state(snapshot, provider_state)
    response = _transport('stop', snapshot, token)
    if response.get('status') not in {'running', 'succeeded', 'failed', 'cancelled'}:
        raise AWSAdapterError('Lightsail cancellation could not be confirmed.')
    return {'message': 'Lightsail cancellation requested; active migration transactions finish before rollback.'}


def store_credentials(platform, name, value, *, snapshot):
    binding_for(platform, snapshot)
    expected = f'/{platform.prefix}/apps/{snapshot["app_identifier"]}/core-resource-client'
    if name != expected:
        raise AWSAdapterError('Lightsail credentials are outside the exact application namespace.')
    encoded = encode_resource_credentials(value)
    token = _token(snapshot['app_identifier'] + ':' + snapshot['commit'] + ':' + snapshot.get('plan_hash', '') + ':credentials')
    response = _transport('credentials', snapshot, token, credentials=json.loads(encoded))
    if response.get('status') != 'succeeded':
        raise AWSAdapterError('Lightsail could not preserve the exact existing application resource credentials.')
    return {'parameter': name, 'version': 1}
