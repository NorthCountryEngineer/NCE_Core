"""Fixed publisher policy deltas and unknown outcomes, without AWS calls."""
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from deployment import repair_frontend_publisher_policy as repair


class FakeAWS:
    def __init__(self):
        self.before = {'Version': '2012-10-17', 'Statement': [
            {'Sid': 'Existing', 'Effect': 'Allow', 'Action': ['amplify:GetJob'], 'Resource': 'fixed-existing-resource'}]}
        self.current = copy.deepcopy(self.before)
        self.account = repair.ACCOUNT
        self.calls = []
        self.fail_write = False

    def identity(self): self.calls.append('identity'); return {'Account': self.account, 'Arn': 'synthetic-operator'}
    def policy(self): self.calls.append('policy'); return copy.deepcopy(self.current)
    def put(self, value):
        self.calls.append(('put', copy.deepcopy(value)))
        if self.fail_write: raise TimeoutError('Synthetic unknown outcome')
        self.current = copy.deepcopy(value)


class PublisherPolicyTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.receipt = Path(self.directory.name) / 'receipt.json'
        self.aws = FakeAWS()
        self.plan = repair.make_plan(self.aws.before)

    def test_exact_delta_is_only_two_actions_on_one_existing_production_deployment_path(self):
        self.assertEqual(self.plan['after']['Statement'][:-1], self.aws.before['Statement'])
        self.assertEqual(self.plan['after']['Statement'][-1], {
            'Sid': 'ProposalProductionManualDeployments', 'Effect': 'Allow',
            'Action': ['amplify:CreateDeployment', 'amplify:StartDeployment'],
            'Resource': 'arn:aws:amplify:us-east-1:015454941623:apps/d1f14xk20wy3vy/branches/production/deployments/*'})
        self.assertEqual(self.aws.current, self.aws.before)

    def test_reviewed_apply_rechecks_and_verifies_exact_after_policy(self):
        result = repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(self.aws.current, self.plan['after'])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len([x for x in self.aws.calls if isinstance(x, tuple)]), 1)

    def test_tampered_plan_is_rejected_without_api_calls(self):
        self.plan['after']['Statement'][-1]['Resource'] = '*'
        with self.assertRaises(ValueError): repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertEqual(self.aws.calls, [])

    def test_changed_existing_policy_is_rejected_without_write(self):
        self.aws.current['Statement'].append({'Effect': 'Deny', 'Action': '*', 'Resource': '*'})
        with self.assertRaises(ValueError): repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertFalse(any(isinstance(x, tuple) for x in self.aws.calls)); self.assertFalse(self.receipt.exists())

    def test_wrong_account_is_rejected_without_write(self):
        self.aws.account = '000000000000'
        with self.assertRaises(ValueError): repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertFalse(any(isinstance(x, tuple) for x in self.aws.calls))

    def test_unknown_write_retains_intent_and_never_retries(self):
        self.aws.fail_write = True
        with self.assertRaises(TimeoutError): repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertEqual(json.loads(self.receipt.read_text())['state'], 'updating')
        count = len(self.aws.calls)
        with self.assertRaises(ValueError): repair.apply_plan(self.plan, self.aws, self.receipt)
        self.assertEqual(len(self.aws.calls), count)

    def test_exclusive_intent_cannot_overwrite_a_concurrent_start(self):
        self.receipt.write_text('{"state":"updating"}')
        with self.assertRaises(FileExistsError): repair.record(self.receipt, {}, initial=True)
        self.assertEqual(json.loads(self.receipt.read_text()), {'state': 'updating'})

    def test_cli_fixes_destination_and_disables_all_sdk_cli_retries(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return type('Result', (), {'stdout': '{}'})()
        with patch.dict(repair.os.environ, {'AWS_MAX_ATTEMPTS': '99'}):
            repair.AWS(run).put(self.plan['after'])
        command, options = calls[0]
        self.assertIn(repair.USER, command); self.assertIn(repair.POLICY, command)
        self.assertEqual(options['env']['AWS_MAX_ATTEMPTS'], '1')
        self.assertEqual(json.loads(command[-1]), self.plan['after'])
        with self.assertRaises(ValueError): repair.AWS(run).call('iam', 'attach-user-policy')
