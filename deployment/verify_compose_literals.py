#!/usr/bin/env python3
"""Local real-Compose regression: synthetic literal dollars survive container env.

Runs an isolated Python process with no network, database or production secrets.
Supply an already-local Core image; its application entrypoint is overridden.
"""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from lightsail_release_protocol import compose_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Already-local Core image with /app/.venv/bin/python')
    args = parser.parse_args()
    literal = 'literal-${NCE_EXPR}-$NCE_WORD-$$-end'
    credentials = json.dumps({'client_id': 'synthetic-client', 'client_secret': literal}, separators=(',', ':'))
    expected = {'CORE_RESOURCE_CREDENTIALS_JSON': credentials,
                'DATABASE_URL': 'postgresql://owner:' + literal + '@postgres/academy',
                'ACADEMY_RUNTIME_DB_PASSWORD': literal}
    document = {'services': {'probe': {'image': args.image, 'pull_policy': 'never', 'network_mode': 'none', 'read_only': True,
                 'entrypoint': ['/app/.venv/bin/python'], 'command': ['-c',
                 'import os,json; print(json.dumps({key:os.environ[key] for key in ' + repr(list(expected)) + '}))'],
                 'environment': {key: 'base-value-must-be-overridden' for key in expected}}}}
    override = {'services': {'probe': {'environment': expected}}}
    with tempfile.TemporaryDirectory(prefix='core-compose-literals-') as directory:
        root = Path(directory)
        base_file, override_file = root / 'base.json', root / 'override.json'
        base_file.write_text(json.dumps(document))
        override_file.write_text(compose_json(override))
        env = {**os.environ, 'NCE_EXPR': 'CORRUPTED_IF_INTERPOLATED', 'NCE_WORD': 'CORRUPTED_IF_INTERPOLATED'}
        result = subprocess.run(['docker', 'compose', '-p', 'nce-core-literal-regression', '-f', str(base_file),
                                 '-f', str(override_file), 'run', '--rm', '-T', '--no-deps', 'probe'],
                                capture_output=True, text=True, check=True, env=env, timeout=60)
        actual = json.loads(result.stdout)
        if actual != expected:
            raise RuntimeError('Compose changed a literal credential or owner migration environment value')
    print(json.dumps({'passed': True, 'real_container_environment_verified': True,
                      'literal_patterns': ['${NCE_EXPR}', '$NCE_WORD', '$$'], 'production_secrets_used': False,
                      'network': 'none', 'application_process_started': False}))


if __name__ == '__main__':
    main()
