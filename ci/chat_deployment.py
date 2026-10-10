"""Cold-start a private PostgreSQL/web/worker deployment and test Chat in browsers.

Creates only local Docker resources, publishes only to loopback, and cleans up.
No registry push, external cluster, payment, or model-provider invocation exists.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DeploymentTestError(Exception):
    pass


def command(args, data=None, timeout=120):
    result = subprocess.run(args, input=data, text=True, cwd=ROOT, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Raw Docker/browser diagnostics can include mounted credentials or links.
        raise DeploymentTestError(f'{Path(args[0]).name} {args[1]} operation failed (exit {result.returncode}).')
    return result.stdout


def docker(*args, data=None, timeout=120):
    return command(['docker', *args], data=data, timeout=timeout).strip()


def build(image):
    args = ['docker', 'build', '--label', 'org.opencontainers.image.revision=' + command(['git', 'rev-parse', 'HEAD']).strip(), '-t', image]
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']:
        if os.environ.get(key):
            args += ['--build-arg', key]
    if os.environ.get('CORE_BUILD_CA_FILE'):
        args += ['--secret', 'id=build_ca,src=' + os.environ['CORE_BUILD_CA_FILE']]
    if os.environ.get('CORE_BUILD_NETWORK'):
        args += ['--network', os.environ['CORE_BUILD_NETWORK']]
    if os.environ.get('CORE_BUILD_PROXY_HOST'):
        args += ['--add-host', 'proxy:' + os.environ['CORE_BUILD_PROXY_HOST']]
    command([*args, '.'], timeout=1200)


def wait_ready(origin, web):
    import requests
    session = requests.Session()
    session.trust_env = False
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if docker('inspect', '--format', '{{.State.Running}}', web) != 'true':
            raise DeploymentTestError('Chat web container exited during startup.')
        try:
            response = session.get(origin + '/health/', timeout=2)
            if response.status_code == 200 and response.json() == {'status': 'ok'}:
                return
        except (requests.RequestException, ValueError):
            pass
        time.sleep(.2)
    raise DeploymentTestError('Chat health check timed out.')


@contextmanager
def deployment(image):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    prefix = 'nce-chat-test-' + uuid.uuid4().hex
    network, volume = prefix + '-network', prefix + '-secrets'
    database, web, worker = prefix + '-db', prefix + '-web', prefix + '-worker'
    password, account_password = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    database_name = 'chat_test_' + uuid.uuid4().hex
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    origin = f'http://localhost:{port}'
    environment = {
        'CORE_DEBUG': '1', 'CORE_ISSUER': origin, 'CORE_ALLOWED_HOSTS': 'localhost,127.0.0.1',
        'CORE_SECRET_KEY_FILE': '/run/chat-test/django.key', 'CORE_OIDC_KEY_FILE': '/run/chat-test/oidc.key',
        'DATABASE_URL': f'postgresql://chat_test:{quote(password, safe="")}@{database}:5432/{database_name}',
        'CORE_TRUST_PROXY': '0',
    }
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    tools = tempfile.TemporaryDirectory(prefix='nce-chat-tools-')
    tools_root = Path(tools.name)
    # A private checkout can have mode 0700. Stage only non-secret helper code
    # for the container UID; never change the owner's checkout permissions.
    tools_root.chmod(0o755)
    shutil.copyfile(ROOT / 'ci/chat_instance.py', tools_root / 'chat_instance.py')
    (tools_root / 'chat_instance.py').chmod(0o644)
    common = ['--network', network, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
              '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', '--mount', f'type=volume,src={volume},dst=/run/chat-test,readonly',
              '--mount', f'type=bind,src={tools_root},dst=/test-tools,readonly']
    helper = [image, '.venv/bin/python', '/test-tools/chat_instance.py']
    jobs = [prefix + '-secrets-init']
    try:
        # A separate bridge keeps fixtures apart from other deployments and
        # permits a loopback-published web port for the host browser. Docker
        # internal networks suppress published ports on some engines.
        docker('network', 'create', network)
        docker('volume', 'create', volume)
        storage = docker('run', '--rm', '-i', '--name', jobs[0], '--network', 'none', '--user', '0', '--read-only',
               '--mount', f'type=volume,src={volume},dst=/run/chat-test',
               '--mount', f'type=bind,src={tools_root},dst=/test-tools,readonly', *helper, 'secrets',
               data=json.dumps({'django.key': secrets.token_urlsafe(64), 'oidc.key': private_key, 'runtime.json': environment}))
        if json.loads(storage)['available_bytes'] < 3 * 1024 ** 3:
            raise DeploymentTestError('Docker needs at least 3 GiB of free space after the image build for the disposable deployment.')
        docker('run', '--detach', '--name', database, '--network', network, '--memory', '512m', '--cpus', '2',
               '--env-file', '/dev/stdin', 'postgres:17', data=f'POSTGRES_USER=chat_test\nPOSTGRES_DB={database_name}\nPOSTGRES_PASSWORD={password}\n')
        for _ in range(60):
            try:
                docker('exec', database, 'pg_isready', '-h', '127.0.0.1', '-U', 'chat_test', '-d', database_name)
                break
            except DeploymentTestError:
                time.sleep(.2)
        else:
            raise DeploymentTestError('Disposable PostgreSQL did not become ready.')
        def job(mode, data=None, timeout=180):
            name = prefix + '-job-' + uuid.uuid4().hex
            jobs.append(name)
            return docker('run', '--rm', '-i', '--name', name, '--memory', '768m', '--cpus', '2',
                          *common, *helper, mode, data=data, timeout=timeout)
        job('initialize', json.dumps({'email': 'operator@example.test', 'password': account_password}))
        docker('run', '--detach', '--name', web, '--memory', '512m', '--cpus', '2', '--publish', f'127.0.0.1:{port}:8000',
               *common, *helper, 'web')
        docker('run', '--detach', '--name', worker, '--memory', '256m', '--cpus', '1', *common, *helper, 'worker')
        wait_ready(origin, web)
        yield {'origin': origin, 'web': web, 'worker': worker, 'job': job, 'password': account_password}
    finally:
        failures = []
        for name in [*reversed(jobs), worker, web, database]:
            try:
                docker('rm', '--force', '--volumes', name, timeout=20)
            except (DeploymentTestError, subprocess.TimeoutExpired, OSError):
                # An absent container is harmless; an existing one must be removed.
                try:
                    result = subprocess.run(['docker', 'container', 'inspect', name], timeout=10,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if result.returncode == 0:
                        failures.append(name)
                except (subprocess.TimeoutExpired, OSError):
                    failures.append(name)
        for kind, name in [('volume', volume), ('network', network)]:
            try:
                result = subprocess.run(['docker', kind, 'rm', name], timeout=20,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode:
                    existing = subprocess.run(['docker', kind, 'inspect', name], timeout=10,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if existing.returncode == 0:
                        failures.append(name)
            except (subprocess.TimeoutExpired, OSError):
                failures.append(name)
        tools.cleanup()
        if failures:
            raise DeploymentTestError('Could not remove all disposable Chat resources.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='nce-core-chat:test')
    parser.add_argument('--build', action='store_true', help='Build a local image before testing; never push it.')
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit'], action='append', help='Repeat for multiple engines; default Chromium.')
    parser.add_argument('--browser-executable', help='Existing Chromium executable; other engines use Playwright installations.')
    parser.add_argument('--report', default=str(ROOT / 'artifacts/chat-deployment.json'))
    args = parser.parse_args()
    report = {'status': 'failed', 'checks': [], 'browsers': {}, 'image': args.image,
              'harness_commit': command(['git', 'rev-parse', 'HEAD']).strip(),
              'dirty': bool(command(['git', 'status', '--porcelain']).strip())}
    stage = 'browser prerequisites'
    try:
        from ci.chat_browser import ChatBrowserChecks
        from playwright.sync_api import sync_playwright
        # Fail before creating resources when a requested browser cannot launch.
        with sync_playwright() as playwright:
            for engine in args.browser or ['chromium']:
                options = {'executable_path': args.browser_executable} if args.browser_executable and engine == 'chromium' else {}
                browser = getattr(playwright, engine).launch(headless=True, **options)
                browser.close()
        stage = 'image'
        print('Chat deployment: checking/building the local image.', flush=True)
        if args.build:
            build(args.image)
        report['image_id'] = docker('image', 'inspect', '--format', '{{.Id}}', args.image)
        report['image_commit'] = docker('image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', args.image)
        if docker('image', 'inspect', '--format', '{{.Config.User}}', args.image) not in ['core', '10001', '10001:10001']:
            raise DeploymentTestError('Chat image must use the non-root Core service account.')
        stage = 'cold deployment'
        print('Chat deployment: migrating a disposable database and starting web/worker.', flush=True)
        with deployment(args.image) as instance:
            report['checks'].append('cold migrations, mounted keys, PostgreSQL, non-root read-only web and worker, loopback health')
            stage = 'backend regression'
            print('Chat deployment: running backend tests inside the image.', flush=True)
            output = instance['job']('tests', timeout=300)
            report['checks'].append('backend API, RBAC, CSRF, agents, tags, federation and portal regression')
            # Django writes its summary to stderr; the container helper emits count separately.
            count = re.search(r'Found (\d+) test', output)
            report['backend_tests'] = int(count.group(1)) if count else None
            if not report['backend_tests']:
                raise DeploymentTestError('Backend runner did not confirm that tests executed.')
            for engine in args.browser or ['chromium']:
                stage = engine + ' browser'
                print(f'Chat deployment: testing {engine} administrator/member sessions.', flush=True)
                with ChatBrowserChecks(instance, engine, args.browser_executable) as checks:
                    report['browsers'][engine] = checks.checks
                    report['browsers'][engine] = checks.run()
            stage = 'worker health and durable restart'
            print('Chat deployment: checking worker heartbeats and restarting web/worker.', flush=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                before = json.loads(instance['job']('state'))
                if before['chat'] and all(item['healthy'] and item['observed_state'] == 'running' for item in before['chat']):
                    break
                time.sleep(.2)
            else:
                raise DeploymentTestError('Embedded Chat instance did not receive worker heartbeats.')
            docker('restart', instance['web'], instance['worker'])
            wait_ready(instance['origin'], instance['web'])
            after = json.loads(instance['job']('state'))
            if (before['messages'], before['conversations']) != (after['messages'], after['conversations']):
                raise DeploymentTestError('Restart lost durable Chat records.')
            if not before['messages']:
                raise DeploymentTestError('Browser tests did not create durable messages.')
            for engine in args.browser or ['chromium']:
                stage = engine + ' restarted browser'
                with ChatBrowserChecks(instance, engine, args.browser_executable) as checks:
                    checks.check_restart()
            report['checks'].append('worker heartbeat and persisted conversations/messages after web/worker restart')
        report['checks'].append('disposable containers, database volumes, secret volume and private network removed')
        report['status'] = 'passed'
    except Exception as error:
        report['failure'] = {'stage': stage, 'type': type(error).__name__}
        if isinstance(error, DeploymentTestError):
            report['failure']['reason'] = str(error)
        elif type(error) is AssertionError and str(error).startswith('Form returned HTTP '):
            report['failure']['reason'] = str(error)
        print(f'Chat deployment tests failed during {stage} ({type(error).__name__}).', file=sys.stderr)
    finally:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
