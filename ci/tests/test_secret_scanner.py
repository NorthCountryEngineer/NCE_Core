from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import scan_secrets as s


class SecretScannerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        subprocess.run(['git', 'init', '-q', directory.name], check=True)
        shutil.copy(s.ROOT / '.secrets.baseline', self.root / '.secrets.baseline')
        self.password = secrets.token_urlsafe(48)
        self.file = self.root / 'fixture.py'

    def stage(self):
        subprocess.run(['git', '-C', str(self.root), 'add', 'fixture.py'], check=True)

    def scan(self, staged):
        output = io.StringIO()
        before = (self.root / '.secrets.baseline').read_bytes()
        with patch.object(s, 'ROOT', self.root), redirect_stdout(output), redirect_stderr(output):
            result = s.scan(staged=staged)
        self.assertNotIn(self.password, output.getvalue())
        self.assertEqual(before, (self.root / '.secrets.baseline').read_bytes())
        return result

    def test_scans_staged_secret_even_when_working_copy_was_sanitized(self):
        self.file.write_text('password = ' + repr(self.password) + '\n')
        self.stage()
        self.file.write_text('import secrets\npassword = secrets.token_urlsafe(48)\n')
        self.assertEqual(self.scan(staged=False), 0)
        self.assertEqual(self.scan(staged=True), 1)

    def test_runtime_generation_is_accepted(self):
        self.file.write_text('import secrets\npassword = secrets.token_urlsafe(48)\n')
        self.stage()
        self.assertEqual(self.scan(staged=True), 0)

    def test_credential_bearing_database_url_is_rejected(self):
        self.file.write_text('DATABASE_URL = ' + repr('postgresql://core:' + self.password + '@localhost/core') + '\n')
        self.stage()
        self.assertEqual(self.scan(staged=True), 1)
