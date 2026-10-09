"""Run verification with a private, disposable database and fresh credentials.

The password is generated in memory, passed through Docker stdin, and never
written to Git, an artifact, a command argument, or the CI platform environment.
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, data=None):
    result = subprocess.run(['docker', *args], input=data, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Docker diagnostics/inspection can contain credentials.
        raise RuntimeError('Disposable PostgreSQL operation failed.')
    return result.stdout.strip()


def verify():
    name = 'core-ci-postgres-' + uuid.uuid4().hex
    password = secrets.token_urlsafe(48)
    try:
        docker('run', '--detach', '--name', name, '--publish', '127.0.0.1::5432',
               '--env-file', '/dev/stdin', 'postgres:17',
               data=f'POSTGRES_USER=core_ci\nPOSTGRES_DB=core_ci\nPOSTGRES_PASSWORD={password}\n')
        port = docker('inspect', '--format', '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', name)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise RuntimeError('Invalid disposable database port.')
        for _ in range(60):
            try:
                docker('exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'core_ci', '-d', 'core_ci')
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError('Disposable PostgreSQL did not become ready.')
        environment = os.environ.copy()
        environment['DATABASE_URL'] = f'postgresql://core_ci:{quote(password, safe="")}@127.0.0.1:{port}/core_ci'
        # This wrapper only verifies. Publication/deployment remain separate steps.
        environment['CORE_CI_MODE'] = 'verify'
        for key in ['REGISTRY_USERNAME', 'REGISTRY_PASSWORD', 'KUBECONFIG']:
            environment.pop(key, None)
        return subprocess.run(['bash', 'ci/run.sh'], cwd=ROOT, env=environment).returncode
    finally:
        # Remove the database's anonymous volume too, including failed runs.
        docker('rm', '--force', '--volumes', name)


if __name__ == '__main__':
    try:
        sys.exit(verify())
    except (RuntimeError, OSError):
        print('Disposable PostgreSQL verification failed.', file=sys.stderr)
        sys.exit(1)
