"""Offline contracts for read-only, fixed-repository application verification."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class GameStudioOnboardingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/game-studio-onboarding-checks.yml').read_text()
        self.broker = (ROOT / '.github/workflows/game-studio-release-checks.yml').read_text()
        self.guard = textwrap.dedent(self.workflow.split('        run: |\n', 1)[1].split('      - uses: actions/checkout@v4', 1)[0])
        self.default = re.search(r'DEFAULT_GAME_COMMIT: ([a-f0-9]{40}) ', self.workflow)[1]

    def execute_guard(self, event, requested):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            sentinel = Path(directory) / 'unexpected'
            result = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', self.guard],
                cwd=directory, env={**os.environ, 'GAME_EVENT': event,
                    'REQUESTED_GAME_COMMIT': requested, 'DEFAULT_GAME_COMMIT': self.default,
                    'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
            self.assertFalse(sentinel.exists(), 'Untrusted input executed as shell code.')
            return result.returncode, output.read_text() if output.exists() else ''

    def test_manual_dispatch_requires_a_commit_and_permissions_remain_read_only(self):
        self.assertEqual(self.workflow.split('    steps:\n', 1)[0], '''name: Game Studio onboarding checks
on:
  push:
    branches: [codex/game-studio-canonical-20261024]
  workflow_dispatch:
    inputs:
      game_commit:
        description: Immutable Game Studio commit to verify (40 lowercase hex characters)
        required: true
        type: string
permissions:
  contents: read
jobs:
  required:
    name: Game Studio onboarding required checks
    runs-on: ubuntu-latest
''')

    def test_all_thirteen_application_checks_and_fixed_repository_are_preserved(self):
        marker = '      - uses: actions/checkout@v4'
        expected = self.broker.split(marker, 1)[1].replace(
            '          # Public immutable application commit; not a credential.\n'
            '          ref: 55450955f8acfe88171289ec262b15f17e1c5e84  # pragma: allowlist secret',
            '          ref: ${{ steps.source.outputs.commit }}')
        self.assertEqual(self.workflow.split(marker, 1)[1], expected)
        self.assertEqual(len(re.findall(r'^      - (?:uses:|run:|name:)', marker + expected, re.M)), 13)
        self.assertIn('repository: NorthCountryEngineer/Game_Studio\n', expected)
        self.assertIn('persist-credentials: false\n', expected)

    def test_validation_precedes_checkout_and_inputs_enter_only_as_environment_data(self):
        before = self.workflow.split('      - uses: actions/checkout@v4', 1)[0]
        self.assertIn('      - name: Validate immutable Game Studio source\n        id: source\n        shell: bash\n', before)
        self.assertIn('          GAME_EVENT: ${{ github.event_name }}\n', before)
        self.assertIn('          REQUESTED_GAME_COMMIT: ${{ inputs.game_commit }}\n', before)
        self.assertNotIn('${{', self.guard)
        self.assertNotIn('continue-on-error', self.workflow)
        self.assertNotIn('if:', before)

    def test_valid_immutable_manual_commit_is_returned_unchanged(self):
        commit = 'a1' * 20
        self.assertEqual(self.execute_guard('workflow_dispatch', commit), (0, 'commit=' + commit + '\n'))

    def test_missing_mutable_malformed_and_shell_input_are_refused(self):
        for value in ['', 'main', 'refs/heads/main', 'a' * 39, 'a' * 41, 'A' * 40,
                      'g' * 40, 'a' * 40 + '\n', 'a' * 40 + '\ncommit=main',
                      '$(touch unexpected)', '`touch unexpected`', ';touch unexpected']:
            with self.subTest(value=value):
                code, output = self.execute_guard('workflow_dispatch', value)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, '')

    def test_push_uses_reviewed_default_and_other_events_are_refused(self):
        self.assertEqual(self.execute_guard('push', '$(touch unexpected)'), (0, 'commit=' + self.default + '\n'))
        for event in ['pull_request', 'workflow_run', '']:
            with self.subTest(event=event):
                code, output = self.execute_guard(event, self.default)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, '')

    def test_approved_deployment_broker_is_not_modified(self):
        # Public immutable workflow digest, not a credential.
        self.assertEqual(hashlib.sha256(self.broker.encode()).hexdigest(),
            '3d8e4a2dc12bff65407900b63c5ebe82d8300af7215c13341018c32aebaab44d')  # pragma: allowlist secret


if __name__ == '__main__':
    unittest.main()
