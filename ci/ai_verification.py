"""Verify Core AI on disposable local PostgreSQL, including opt-in Chromium.

Secrets are generated in memory and passed through stdin/environment, never
stored in source, command arguments, reports, or the running Core database.
"""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.verify_with_postgres import docker


def verify(args):
    name = 'core-ai-verification-' + uuid.uuid4().hex
    password = secrets.token_urlsafe(48)
    # Require the operator's existing local image, with no implicit registry pull.
    docker('image', 'inspect', '--format', '{{.Id}}', 'postgres:17')
    created = False
    try:
        docker('run', '--detach', '--name', name, '--pull', 'never', '--publish', '127.0.0.1::5432',
            '--env-file', '/dev/stdin', 'postgres:17',
            data=f'POSTGRES_USER=core_ai_test\nPOSTGRES_DB=core_ai_test\nPOSTGRES_PASSWORD={password}\n')
        created = True
        port = docker('inspect', '--format', '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', name)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise RuntimeError('Invalid disposable database port.')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                docker('exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'core_ai_test', '-d', 'core_ai_test')
                break
            except RuntimeError:
                time.sleep(.2)
        else:
            raise RuntimeError('Disposable database did not become ready.')
        environment = os.environ.copy()
        environment['DATABASE_URL'] = f'postgresql://core_ai_test:{quote(password, safe="")}@127.0.0.1:{port}/core_ai_test'
        environment['CORE_DEBUG'] = '1'
        environment.pop('CORE_BROWSER_EXECUTABLE', None)
        if args.browser_executable:
            environment['CORE_BROWSER_EXECUTABLE'] = str(Path(args.browser_executable).resolve(strict=True))
        command = [sys.executable, 'manage.py', 'test', '--noinput', '--verbosity', '1', '--parallel', str(args.parallel)]
        if not args.full:
            command += args.labels or ['core_ai', 'core_fabric']
        return subprocess.run(command, cwd=ROOT, env=environment).returncode
    finally:
        if created:
            docker('rm', '--force', '--volumes', name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable', help='Path to an already-installed Chromium binary; never downloads browsers.')
    parser.add_argument('--full', action='store_true', help='Run the complete Django suite against the disposable database.')
    parser.add_argument('--parallel', type=int, choices=range(1, 9), default=1, help='Local Django test processes (1–8); no cloud resources are provisioned.')
    parser.add_argument('labels', nargs='*', help='Optional Django test labels (default: core_ai core_fabric).')
    try:
        sys.exit(verify(parser.parse_args()))
    except (RuntimeError, OSError):
        print('Core AI verification could not start; confirm local Docker, postgres:17, and the browser path.', file=sys.stderr)
        sys.exit(1)
