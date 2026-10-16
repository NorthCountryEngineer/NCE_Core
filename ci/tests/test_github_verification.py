"""Contracts for the automatic workflow, parsed as the JSON subset of YAML.

Keep automatic verification independent of release credentials and runner
environments. The live deployment gate relies on this workflow and job identity.
"""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GitHubVerificationTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads((ROOT / '.github/workflows/verify.yml').read_text())
        self.job = self.workflow['jobs']['verify']

    def test_every_push_and_pull_request_runs_the_same_required_checks(self):
        self.assertEqual(self.workflow['name'], 'Core CI')
        self.assertEqual(self.workflow['on'], {'pull_request': {}, 'push': {}, 'workflow_dispatch': {}})
        self.assertEqual(set(self.workflow['jobs']), {'verify'})
        self.assertEqual(self.job['name'], 'Core required checks')
        self.assertNotIn('if', self.job)
        self.assertNotIn('continue-on-error', self.job)
        steps = self.job['steps']
        self.assertEqual([step['run'] for step in steps if 'run' in step], [
            'python3 deployment/aws-platform/check_template.py',
            'python3 ci/verify_with_postgres.py',
        ])
        for step in steps:
            self.assertNotIn('if', step)
            self.assertNotIn('continue-on-error', step)

    def test_forks_use_an_ephemeral_bounded_runner_without_release_authority(self):
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        self.assertEqual(self.job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(self.job['timeout-minutes'], 30)
        self.assertEqual(self.workflow['concurrency'], {
            'group': 'core-verify-${{ github.event_name }}-${{ github.ref }}',
            'cancel-in-progress': True,
        })
        self.assertNotIn('environment', self.job)
        self.assertNotIn('permissions', self.job)
        self.assertEqual(self.job['env'], {
            'CORE_CI_CONFIG': 'ci/environments/example.json',
            'CORE_CI_MODE': 'verify',
            'CORE_CI_ALLOW_PUBLISH': '0',
            'CORE_CI_ALLOW_DEPLOY': '0',
            'AWS_EC2_METADATA_DISABLED': 'true',
        })
        # Do not expose protected settings, caches, stored artifacts, or cloud
        # authentication to source code from an arbitrary pull request.
        serialized = json.dumps(self.workflow)
        for forbidden in ['secrets.', 'vars.', 'self-hosted', 'pull_request_target',
                          'upload-artifact', 'actions/cache', 'id-token',
                          'configure-aws-credentials', 'workflow_run']:
            self.assertNotIn(forbidden, serialized)

    def test_checkout_and_python_are_pinned_without_persisted_git_credentials(self):
        actions = [step for step in self.job['steps'] if 'uses' in step]
        self.assertEqual(len(actions), 2)
        for step in actions:
            self.assertRegex(step['uses'], re.compile(r'^actions/(?:checkout|setup-python)@[a-f0-9]{40}$'))
        self.assertEqual(actions[0]['with'], {'fetch-depth': 0, 'persist-credentials': False})
        self.assertEqual(actions[1]['with'], {'python-version': '3.12'})

    def test_publication_workflow_remains_manual(self):
        release = (ROOT / '.github/workflows/pipeline.yml').read_text()
        self.assertRegex(release, r'(?m)^  workflow_dispatch:$')
        self.assertNotRegex(release, r'(?m)^  (?:push|pull_request|pull_request_target|workflow_run):')


if __name__ == '__main__':
    unittest.main()
