"""Local startup must preserve data and never silently select production settings."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import local_dev

ROOT = Path(__file__).resolve().parents[2]


class LocalDevelopmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(local_dev, 'ROOT', self.root))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_local_mode_creates_private_keys_and_reuses_keys_and_database(self):
        origin, port = local_dev.configure_local()
        self.assertEqual((origin, port), ('http://localhost:8000', 8000))
        self.assertEqual(os.environ['CORE_DEBUG'], '1')
        self.assertEqual(os.environ['CORE_ALLOWED_HOSTS'], 'localhost,127.0.0.1,[::1]')
        directory = self.root / '.local'
        keys = {name: (directory / name).read_bytes() for name in ['django.key', 'oidc.key']}
        for name in keys:
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        database = directory / 'db.sqlite3'
        database.write_bytes(b'synthetic database preservation fixture')
        local_dev.configure_local()
        for name, content in keys.items():
            self.assertEqual((directory / name).read_bytes(), content)
        self.assertEqual(database.read_bytes(), b'synthetic database preservation fixture')

    def test_remote_database_is_rejected_before_creating_keys_and_without_echoing_credentials(self):
        credential = secrets.token_urlsafe(32)
        os.environ['DATABASE_URL'] = f'postgresql://operator:{credential}@database.example.test/core'
        with self.assertRaises(local_dev.LocalConfigurationError) as raised:
            local_dev.configure_local()
        self.assertNotIn(credential, str(raised.exception))
        self.assertFalse((self.root / '.local').exists())

    def test_production_configuration_is_not_overridden(self):
        for values in [{'CORE_DEBUG': '0'}, {'DJANGO_SETTINGS_MODULE': 'other.settings'},
                {'CORE_TRUST_PROXY': '1'}, {'CORE_OIDC_PREVIOUS_KEY_FILES': '/run/secrets/previous'},
                {'CORE_ALLOWED_HOSTS': '*'}, {'CORE_SECRET_KEY_FILE': '/run/secrets/core'},
                {'CORE_OIDC_KEY_FILE': '/run/secrets/oidc'}]:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(local_dev.LocalConfigurationError):
                    local_dev.configure_local()
                self.assertFalse((self.root / '.local').exists())

    def test_external_credentialed_or_non_origin_issuers_are_rejected_before_creating_keys(self):
        for origin in ['https://identity.example.test', 'http://localhost:8000/path',
                'http://localhost:8000?query=1', 'http://localhost:8000#fragment',
                'http://username@localhost:8000', 'http://localhost', 'http://0.0.0.0:8000']:
            with self.subTest(origin=origin), patch.dict(os.environ, {'CORE_ISSUER': origin}, clear=True):
                with self.assertRaises(local_dev.LocalConfigurationError):
                    local_dev.configure_local()
                self.assertFalse((self.root / '.local').exists())

    def test_custom_port_sets_matching_issuer_and_conflicts_fail_before_initialization(self):
        os.environ['CORE_ISSUER'] = 'http://localhost:8123'
        with self.assertRaises(local_dev.LocalConfigurationError):
            local_dev.configure_local(8000)
        self.assertFalse((self.root / '.local').exists())
        self.assertEqual(local_dev.configure_local(8123), ('http://localhost:8123', 8123))
        self.assertEqual(os.environ['CORE_ISSUER'], 'http://localhost:8123')

    def test_out_of_range_ports_do_not_initialize_local_keys(self):
        for port in [0, 80, 65536]:
            with self.subTest(port=port), self.assertRaises(argparse.ArgumentTypeError):
                local_dev.configure_local(port)
            self.assertFalse((self.root / '.local').exists())

    def test_normal_migrate_keeps_production_secret_requirement_and_gives_local_instructions(self):
        result = subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'migrate', '--plan'],
            cwd=self.root, env=dict(os.environ), capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Set CORE_SECRET_KEY_FILE to a mounted secret file for production.', result.stderr)
        self.assertIn('python manage.py --local migrate', result.stderr)

    def test_help_is_available_without_secrets_or_initialization(self):
        result = subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'dev', '--help'],
            cwd=self.root, env=dict(os.environ), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--port', result.stdout)
        self.assertIn('loopback development server', result.stdout)
        self.assertFalse((self.root / '.local').exists())

    def test_empty_and_unreadable_secret_files_have_actionable_errors(self):
        empty = self.root / 'empty.key'
        empty.touch()
        for path, message in [(empty, 'points to an empty secret file'),
                (self.root / 'missing.key', 'must point to a readable secret file')]:
            with self.subTest(path=path):
                environment = {**os.environ, 'CORE_DEBUG': '1', 'CORE_SECRET_KEY_FILE': str(path)}
                result = subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'check'],
                    cwd=self.root, env=environment, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_generic_local_runserver_refers_to_loopback_launcher_before_initializing(self):
        result = subprocess.run([sys.executable, str(ROOT / 'manage.py'), '--local', 'runserver', '0.0.0.0:8000'],
            cwd=self.root, env=dict(os.environ), capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('python manage.py dev', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
