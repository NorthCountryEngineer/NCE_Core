"""Shared CI/CD: PostgreSQL verification, commit images, digest releases, Kubernetes.

No shell evaluation, cloud resource provisioning, or secret values in artifacts.
External mutations require separate explicit publication/deployment switches.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread
import uuid

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / 'artifacts'
PYTHON = str(ROOT / '.venv/bin/python')


class PipelineError(Exception):
    pass


def command(args, *, data=None, capture=False, env=None, timeout=None):
    result = subprocess.run(args, cwd=ROOT, input=data, text=True,
        stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None,
        env=env, timeout=timeout)
    if result.returncode:
        # Captured kubectl/config/registry output can contain credentials. Do not echo it.
        raise PipelineError(f'{Path(args[0]).name} operation failed (exit {result.returncode}).')
    return result.stdout.strip() if capture else ''


def load_config(path):
    from jsonschema import Draft202012Validator
    value = json.loads(Path(path).read_text())
    schema = json.loads((ROOT / 'ci/config.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # Report fields, not values: a misplaced secret must not be printed.
        raise PipelineError('Invalid configuration fields: ' + ', '.join('/'.join(map(str, e.path)) or '<root>' for e in errors))
    k = value['kubernetes']
    targets = [(kind, item['organization_id'].lower(), item['filename']) for kind in ['provider_secrets', 'chat_federation_secrets'] for item in k.get(kind, [])]
    if len(set(targets)) != len(targets):
        raise PipelineError('Duplicate provider secret mount path.')
    if '..' in value['image_repository'].split('/') or '..' in value['release_branch'].split('/'):
        raise PipelineError('Invalid repository or branch path.')
    return value


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def revision():
    sha = command(['git', 'rev-parse', '--verify', 'HEAD^{commit}'], capture=True)
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise PipelineError('A Git checkout with a full commit SHA is required.')
    return sha


def dirty():
    return bool(command(['git', 'status', '--porcelain'], capture=True))


def write_artifact(name, value):
    ARTIFACTS.mkdir(exist_ok=True)
    path = ARTIFACTS / name
    path.write_text(json.dumps(value, indent=2) + '\n')
    return path


def test_environment():
    environment = {**os.environ, 'CORE_DEBUG': '1', 'CORE_ISSUER': 'http://localhost:8000',
        'CORE_ALLOWED_HOSTS': 'localhost,127.0.0.1,testserver', 'CORE_TRUST_PROXY': '0',
        'CORE_SECRET_KEY_FILE': str(ROOT / '.local/django.key'), 'CORE_OIDC_KEY_FILE': str(ROOT / '.local/oidc.key'),
        'CORE_IDP_SECRET_ROOT': str(ROOT / '.local/idp-secrets'),
        'CORE_CHAT_SECRET_ROOT': str(ROOT / '.local/chat-federation'), 'CORE_CHAT_FEDERATION_ORIGINS': ''}
    environment.pop('CORE_OIDC_PREVIOUS_KEY_FILES', None)
    for name in ['REGISTRY_USERNAME', 'REGISTRY_PASSWORD', 'KUBECONFIG', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                 'AWS_SESSION_TOKEN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS']:
        environment.pop(name, None)
    return environment


def check(config):
    command([PYTHON, 'ci/scan_secrets.py'])
    environment = test_environment()
    command([PYTHON, 'scripts/init_dev.py'], env=environment)
    command([PYTHON, 'manage.py', 'check'], env=environment)
    command([PYTHON, 'manage.py', 'makemigrations', '--check', '--dry-run'], env=environment)


def test(config):
    if not os.environ.get('DATABASE_URL', '').startswith(('postgres://', 'postgresql://')):
        raise PipelineError('CI tests require an isolated PostgreSQL DATABASE_URL, never a customer database.')
    command([PYTHON, 'manage.py', 'test', 'identity.tests', 'platform_services.tests', 'core_chat.tests', '--noinput'],
            env=test_environment())
    command([PYTHON, '-m', 'unittest', 'discover', '-s', 'ci/tests', '-v'])


def build(config):
    for name in ['build.json', 'release.json', 'deployment.json', 'deployed.json']:
        (ARTIFACTS / name).unlink(missing_ok=True)
    sha = revision()
    image = config['image_repository'] + ':' + sha
    args = ['docker', 'build', '--label', 'org.opencontainers.image.revision=' + sha, '-t', image]
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']:
        if os.environ.get(name):
            args.extend(['--build-arg', name])
    if os.environ.get('CORE_BUILD_CA_FILE'):
        args.extend(['--secret', 'id=build_ca,src=' + os.environ['CORE_BUILD_CA_FILE']])
    # Network/host overrides are runner-specific, never part of customer deployment config.
    if os.environ.get('CORE_BUILD_NETWORK'):
        args.extend(['--network', os.environ['CORE_BUILD_NETWORK']])
    if os.environ.get('CORE_BUILD_PROXY_HOST'):
        args.extend(['--add-host', 'proxy:' + os.environ['CORE_BUILD_PROXY_HOST']])
    args.append('.')
    command(args)
    result = {'schema_version': 1, 'commit': sha, 'image_tag': image, 'dirty': dirty(), 'config_hash': config_hash(config)}
    write_artifact('build.json', result)
    return result


def require_switch(name):
    if os.environ.get(name) != '1':
        raise PipelineError(f'{name}=1 is required for this external mutation. Obtain cost approval before enabling paid services.')


def trusted_release(config):
    protected = os.environ.get('CORE_CI_ENVIRONMENT')
    if protected and protected != config['environment']:
        raise PipelineError('CI approval environment does not match the deployment configuration.')
    if dirty():
        raise PipelineError('Release requires a clean checkout.')
    remote = command(['git', 'ls-remote', 'origin', 'refs/heads/' + config['release_branch']], capture=True)
    if len(remote.split()) != 2 or remote.split()[0] != revision():
        raise PipelineError('Release must match the current remote release branch tip.')


def publish(config):
    require_switch('CORE_CI_ALLOW_PUBLISH')
    trusted_release(config)
    build_info = json.loads((ARTIFACTS / 'build.json').read_text())
    expected = config['image_repository'] + ':' + revision()
    if build_info != {'schema_version': 1, 'commit': revision(), 'image_tag': expected, 'dirty': False, 'config_hash': config_hash(config)}:
        raise PipelineError('Build artifact does not match this clean checkout and environment configuration.')
    username, password = os.environ.get('REGISTRY_USERNAME'), os.environ.get('REGISTRY_PASSWORD')
    if bool(username) != bool(password):
        raise PipelineError('Provide both registry username and password, or use a runner credential helper.')
    with tempfile.TemporaryDirectory(prefix='core-registry-') as directory:
        environment = os.environ.copy()
        if username:
            environment['DOCKER_CONFIG'] = directory
            command(['docker', 'login', config['image_repository'].split('/')[0], '--username', username, '--password-stdin'],
                    data=password + '\n', capture=True, env=environment)
        command(['docker', 'push', expected], env=environment)
        manifest = json.loads(command(['docker', 'buildx', 'imagetools', 'inspect', expected, '--format', '{{json .Manifest}}'], capture=True, env=environment))
    digest = manifest.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise PipelineError('Registry did not return an immutable image digest.')
    result = {'schema_version': 1, 'commit': revision(), 'image': config['image_repository'] + '@' + digest, 'config_hash': config_hash(config)}
    write_artifact('release.json', result)
    return result


def validate_image(config, image):
    if not re.fullmatch(re.escape(config['image_repository']) + r'@sha256:[0-9a-f]{64}', image):
        raise PipelineError('Deployment requires a digest from the configured image repository.')


def manifests(config, image, sha):
    validate_image(config, image)
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise PipelineError('A full commit SHA is required.')
    k = config['kubernetes']
    name, namespace = k['release_name'], k['namespace']
    common = {'app.kubernetes.io/part-of': name, 'app.kubernetes.io/managed-by': 'core-ci'}
    if k.get('organization_id'):
        common['core.platform/organization'] = k['organization_id']
    def metadata(resource_name):
        return {'name': resource_name, 'namespace': namespace, 'labels': common.copy(), 'annotations': {'core.platform/commit': sha}}
    configmap = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(name + '-config'), 'data': {
        'CORE_DEBUG': '0', 'CORE_ISSUER': k['issuer'], 'CORE_ALLOWED_HOSTS': k['issuer'].split('://')[1].split(':')[0],
        'CORE_TRUST_PROXY': '1', 'CORE_SECRET_KEY_FILE': '/run/secrets/core/django.key',
        'CORE_OIDC_KEY_FILE': '/run/secrets/core/oidc.key', 'CORE_IDP_SECRET_ROOT': '/run/secrets/identity-providers',
        'CORE_CHAT_SECRET_ROOT': '/run/secrets/chat-federation',
        **k.get('extra_environment', {})}}
    volumes = [{'name': 'runtime', 'secret': {'secretName': k['runtime_secret'], 'defaultMode': 288}},
               {'name': 'scratch', 'emptyDir': {'sizeLimit': '128Mi'}}]
    mounts = [{'name': 'runtime', 'mountPath': '/run/secrets/core', 'readOnly': True}, {'name': 'scratch', 'mountPath': '/tmp'}]
    for index, item in enumerate(k.get('provider_secrets', [])):
        volume = 'provider-' + str(index)
        volumes.append({'name': volume, 'secret': {'secretName': item['secret_name'], 'defaultMode': 288}})
        mounts.append({'name': volume, 'mountPath': '/run/secrets/identity-providers/' + item['organization_id'].lower() + '/' + item['filename'],
                       'subPath': item['key'], 'readOnly': True})
    for index, item in enumerate(k.get('chat_federation_secrets', [])):
        volume = 'chat-federation-' + str(index)
        volumes.append({'name': volume, 'secret': {'secretName': item['secret_name'], 'defaultMode': 288}})
        mounts.append({'name': volume, 'mountPath': '/run/secrets/chat-federation/' + item['organization_id'].lower() + '/' + item['filename'],
                       'subPath': item['key'], 'readOnly': True})
    container = {'name': 'core', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'envFrom': [{'configMapRef': {'name': name + '-config'}}],
        'env': [{'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': k['database_secret'], 'key': 'DATABASE_URL'}}}],
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'allowPrivilegeEscalation': False,
                            'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '1000m', 'memory': '512Mi'}}, 'volumeMounts': mounts}
    pod = {'automountServiceAccountToken': False, 'securityContext': {'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
           'containers': [container], 'volumes': volumes}
    if k.get('pull_secrets'):
        pod['imagePullSecrets'] = [{'name': value} for value in k['pull_secrets']]
    migration_pod = deepcopy(pod)
    migration_pod['restartPolicy'] = 'Never'
    migration_pod['containers'][0]['command'] = [
        '.venv/bin/python', '-c', 'import subprocess; subprocess.run([".venv/bin/python", "manage.py", "check", "--deploy", "--fail-level", "WARNING"], check=True); subprocess.run([".venv/bin/python", "manage.py", "migrate", "--noinput"], check=True)']
    migration = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata(name + '-migrate-' + sha[:12]),
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': k.get('migration_timeout_seconds', 600), 'ttlSecondsAfterFinished': 86400,
                 'template': {'metadata': {'labels': common.copy()}, 'spec': migration_pod}}}
    workloads = []
    for component in ['web', 'worker']:
        labels = {**common, 'app.kubernetes.io/component': component}
        spec = deepcopy(pod)
        c = spec['containers'][0]
        if component == 'web':
            c['command'] = ['.venv/bin/gunicorn', 'core.wsgi:application', '--bind', '0.0.0.0:8000', '--workers', '2', '--worker-tmp-dir', '/tmp']
            c['ports'] = [{'containerPort': 8000}]
            probe = {'httpGet': {'path': '/health/', 'port': 8000, 'httpHeaders': [
                {'name': 'Host', 'value': configmap['data']['CORE_ALLOWED_HOSTS']}, {'name': 'X-Forwarded-Proto', 'value': 'https'}]},
                'periodSeconds': 5, 'timeoutSeconds': 3}
            c['readinessProbe'] = deepcopy(probe)
            c['startupProbe'] = {**deepcopy(probe), 'failureThreshold': 24}
            c['livenessProbe'] = {'tcpSocket': {'port': 8000}, 'periodSeconds': 15}
            strategy = {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}}
        else:
            c['command'] = ['.venv/bin/python', 'manage.py', 'core_worker']
            strategy = {'type': 'Recreate'}
        workloads.append({'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(name + '-' + component),
            'spec': {'replicas': k.get(component + '_replicas', 1), 'strategy': strategy,
                     'selector': {'matchLabels': {'app.kubernetes.io/part-of': name, 'app.kubernetes.io/component': component}},
                     'template': {'metadata': {'labels': labels, 'annotations': {'core.platform/commit': sha, 'core.platform/config': config_hash(config)}}, 'spec': spec}}})
    workloads.append({'apiVersion': 'v1', 'kind': 'Service', 'metadata': metadata(name), 'spec': {'type': 'ClusterIP',
        'selector': {'app.kubernetes.io/part-of': name, 'app.kubernetes.io/component': 'web'}, 'ports': [{'port': 80, 'targetPort': 8000}]}})
    host = configmap['data']['CORE_ALLOWED_HOSTS']
    workloads.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'Ingress', 'metadata': metadata(name), 'spec': {
        'ingressClassName': k['ingress_class'], 'tls': [{'hosts': [host], 'secretName': k['tls_secret']}],
        'rules': [{'host': host, 'http': {'paths': [{'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': name, 'port': {'number': 80}}}}]}}]}})
    return configmap, migration, workloads


def kubectl(config, args, *, data=None, timeout=2000):
    k = config['kubernetes']
    return command([os.environ.get('CORE_KUBECTL', 'kubectl'), '--context', k['context'], '--namespace', k['namespace'], *args],
                   data=json.dumps(data) if data is not None else None, capture=True, timeout=timeout)


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


@contextmanager
def release_lock(config):
    """CAS Lease serializes releases across CI vendors; failed renewal stops advancement."""
    name = config['kubernetes']['release_name'] + '-release'
    holder = str(uuid.uuid4())
    lease = {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
        'metadata': {'name': name, 'namespace': config['kubernetes']['namespace']},
        'spec': {'holderIdentity': holder, 'leaseDurationSeconds': 120, 'renewTime': timestamp(now())}}
    try:
        kubectl(config, ['create', '-f', '-'], data=lease, timeout=25)
    except PipelineError:
        old = json.loads(kubectl(config, ['get', 'lease', name, '-o', 'json'], timeout=25))
        spec = old.get('spec', {})
        expires = datetime.fromisoformat(spec.get('renewTime', '1970-01-01T00:00:00Z').replace('Z', '+00:00')) + timedelta(seconds=spec.get('leaseDurationSeconds', 120))
        if spec.get('holderIdentity') and expires > now():
            raise PipelineError('Another release holds the Kubernetes deployment lease.')
        lease['metadata']['resourceVersion'] = old['metadata']['resourceVersion']
        lease['spec']['renewTime'] = timestamp(now())
        kubectl(config, ['replace', '-f', '-'], data=lease, timeout=25)
    stopped, lost = Event(), Event()
    def renew():
        while not stopped.wait(30):
            try:
                current = json.loads(kubectl(config, ['get', 'lease', name, '-o', 'json'], timeout=25))
                if current['spec'].get('holderIdentity') != holder:
                    raise PipelineError('Release lease ownership changed.')
                current['spec']['renewTime'] = timestamp(now())
                kubectl(config, ['replace', '-f', '-'], data=current, timeout=25)
            except Exception:
                lost.set()
                return
    worker = Thread(target=renew, daemon=True)
    worker.start()
    def assert_owned():
        if lost.is_set():
            raise PipelineError('Release lease renewal failed; refusing to advance.')
        current = json.loads(kubectl(config, ['get', 'lease', name, '-o', 'json'], timeout=25))
        spec = current['spec']
        expires = datetime.fromisoformat(spec['renewTime'].replace('Z', '+00:00')) + timedelta(seconds=spec['leaseDurationSeconds'])
        if spec.get('holderIdentity') != holder or expires <= now():
            raise PipelineError('Release lease ownership changed; refusing to advance.')
    try:
        yield assert_owned
    finally:
        stopped.set()
        worker.join(timeout=5)
        try:
            current = json.loads(kubectl(config, ['get', 'lease', name, '-o', 'json'], timeout=25))
            if current['spec'].get('holderIdentity') == holder:
                current['spec'].update(holderIdentity='', leaseDurationSeconds=1, renewTime=timestamp(now()))
                kubectl(config, ['replace', '-f', '-'], data=current, timeout=25)
        except (PipelineError, ValueError, subprocess.TimeoutExpired):
            pass  # The bounded lease expires even if the runner loses network access.


def deploy(config):
    require_switch('CORE_CI_ALLOW_DEPLOY')
    trusted_release(config)
    (ARTIFACTS / 'deployed.json').unlink(missing_ok=True)
    release = json.loads((ARTIFACTS / 'release.json').read_text())
    if release.get('schema_version') != 1 or release.get('commit') != revision() or release.get('config_hash') != config_hash(config):
        raise PipelineError('Release artifact does not match this checkout and configuration.')
    configmap, migration, workloads = manifests(config, release['image'], release['commit'])
    k = config['kubernetes']
    insecure = kubectl(config, ['config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.insecure-skip-tls-verify}'])
    if insecure == 'true':
        raise PipelineError('Kubernetes TLS verification must be enabled.')
    secrets = {k['runtime_secret'], k['database_secret'], k['tls_secret'], *k.get('pull_secrets', []),
               *[item['secret_name'] for item in k.get('provider_secrets', [])],
               *[item['secret_name'] for item in k.get('chat_federation_secrets', [])]}
    for name in sorted(secrets):
        kubectl(config, ['get', 'secret', name, '-o', 'name'])
    all_items = [configmap, migration, *workloads]
    write_artifact('deployment.json', {'apiVersion': 'v1', 'kind': 'List', 'items': all_items})
    with release_lock(config) as assert_owned:
        kubectl(config, ['apply', '--dry-run=server', '-f', '-'], data={'apiVersion': 'v1', 'kind': 'List', 'items': all_items})
        assert_owned()
        kubectl(config, ['apply', '-f', '-'], data=configmap)
        kubectl(config, ['apply', '-f', '-'], data=migration)
        kubectl(config, ['wait', '--for=condition=complete', 'job/' + migration['metadata']['name'], '--timeout=' + str(k.get('migration_timeout_seconds', 600)) + 's'])
        assert_owned()
        kubectl(config, ['apply', '-f', '-'], data={'apiVersion': 'v1', 'kind': 'List', 'items': workloads})
        for component in ['web', 'worker']:
            assert_owned()
            kubectl(config, ['rollout', 'status', 'deployment/' + k['release_name'] + '-' + component,
                             '--timeout=' + str(k.get('rollout_timeout_seconds', 600)) + 's'])
        assert_owned()
        smoke(config)
        assert_owned()
    write_artifact('deployed.json', {**release, 'environment': config['environment'], 'completed_at': timestamp(now())})
    print('Deployment completed: migration, web/worker rollouts and HTTPS smoke checks passed.')


def smoke(config):
    import requests
    issuer = config['kubernetes']['issuer']
    trust = os.environ.get('CORE_CI_CA_BUNDLE') or True
    for attempt in range(5):
        try:
            def get(path, expected=200):
                response = requests.get(issuer + path, allow_redirects=False, timeout=10, verify=trust)
                if response.status_code != expected:
                    raise PipelineError('HTTPS smoke check returned an unexpected status.')
                return response
            if get('/health/').json() != {'status': 'ok'}:
                raise PipelineError('Deployment health response did not match.')
            document = get('/.well-known/openid-configuration').json()
            if document.get('issuer') != issuer or document.get('jwks_uri') != issuer + '/oauth/jwks/':
                raise PipelineError('OIDC discovery does not match the configured issuer.')
            keys = get('/oauth/jwks/').json().get('keys', [])
            if not keys or any(key.get('kty') != 'RSA' or key.get('alg') != 'RS256' for key in keys):
                raise PipelineError('Deployment signing keys are unavailable or unexpected.')
            get('/accounts/login/')
            get('/api/platform/v1/integration/', expected=401)
            return
        except (requests.RequestException, ValueError, AttributeError, PipelineError):
            if attempt == 4:
                raise PipelineError('HTTPS smoke checks failed; inspect ingress, TLS, issuer, keys and runtime health.') from None
            time.sleep(2)


def rbac(config):
    k = config['kubernetes']
    name = k['release_name'] + '-ci'
    metadata = {'name': name, 'namespace': k['namespace']}
    secret_names = sorted({k['runtime_secret'], k['database_secret'], k['tls_secret'], *k.get('pull_secrets', []),
                          *[item['secret_name'] for item in k.get('provider_secrets', [])],
                          *[item['secret_name'] for item in k.get('chat_federation_secrets', [])]})
    permissions = ['get', 'list', 'watch', 'create', 'update', 'patch']
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': metadata},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': metadata, 'rules': [
            {'apiGroups': [''], 'resources': ['configmaps', 'services'], 'verbs': permissions},
            {'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': secret_names, 'verbs': ['get']},
            {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': permissions},
            {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': permissions},
            {'apiGroups': ['networking.k8s.io'], 'resources': ['ingresses'], 'verbs': permissions},
            {'apiGroups': ['coordination.k8s.io'], 'resources': ['leases'], 'verbs': ['get', 'create', 'update']}]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': metadata,
         'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': k['namespace']}],
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name}}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['run', 'check', 'test', 'build', 'publish', 'render', 'deploy', 'rbac', 'smoke'])
    parser.add_argument('--config', default=os.environ.get('CORE_CI_CONFIG', 'ci/environments/example.json'))
    parser.add_argument('--mode', choices=['verify', 'publish', 'deploy'], default='verify')
    parser.add_argument('--image')
    parser.add_argument('--commit')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.stage == 'run':
            # Reject unauthorized release modes before registry/deployment mutations.
            if args.mode in {'publish', 'deploy'}:
                require_switch('CORE_CI_ALLOW_PUBLISH')
                trusted_release(config)
            if args.mode == 'deploy':
                require_switch('CORE_CI_ALLOW_DEPLOY')
            check(config)
            test(config)
            build(config)
            if args.mode in {'publish', 'deploy'}:
                publish(config)
            if args.mode == 'deploy':
                deploy(config)
        elif args.stage == 'render':
            if not args.image:
                raise PipelineError('--image is required for rendering.')
            items = manifests(config, args.image, args.commit or revision())
            print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [items[0], items[1], *items[2]]}, indent=2))
        elif args.stage == 'rbac':
            print(json.dumps(rbac(config), indent=2))
        else:
            globals()[args.stage](config)
    except (PipelineError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        # Never print a JSON decoder's document or a subprocess command containing runtime values.
        print(str(error) if isinstance(error, PipelineError) else 'Pipeline input, file, or command could not be processed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
