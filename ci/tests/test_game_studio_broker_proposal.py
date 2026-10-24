"""Offline fail-closed tests for the approved fixed-source broker policy."""
import base64
import copy
from dataclasses import replace
from datetime import datetime
import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from platform_services import github_ci as gate
from ci.tests.test_github_gate import core_documents
from ci.tests.test_owner_dispatch_proposal import documents as owner_documents

APP = gate.ONE_RELEASE_BROKER.app_commit
CORE = 'b' * 40
APPROVED = replace(gate.ONE_RELEASE_BROKER, workflow_id=98765,
                   approved_after='2026-10-24T15:06:30Z', expires_at='2026-10-25T15:06:30Z')
WORKFLOW = Path(__file__).resolve().parents[2] / APPROVED.workflow


def documents():
    old = owner_documents()
    native = [copy.deepcopy(old[i]) for i in (6, 7, 9)]
    native[1]['object']['sha'] = APP
    run = native[2]['workflow_runs'][0]
    run.update(head_sha=APP, status='queued', conclusion=None)
    native.append({'total_count': 0, 'jobs': []})
    core = [copy.deepcopy(old[i]) for i in range(6, 15)]
    core[0].update(id=APPROVED.repository_id, full_name=APPROVED.repository)
    core[1].update(ref='refs/heads/' + APPROVED.branch)
    core[1]['object']['sha'] = CORE
    raw = WORKFLOW.read_bytes()
    core[2].update(path=APPROVED.workflow, size=len(raw), content=base64.b64encode(raw).decode())
    for index in (3, 6):
        row = core[index]['workflow_runs'][0]
        row.update(head_sha=CORE, head_branch=APPROVED.branch, event='push', created_at='2026-10-24T15:07:00Z', updated_at='2026-10-24T15:08:00Z', workflow_id=APPROVED.workflow_id, path=APPROVED.workflow,
                   repository=copy.deepcopy(core[0]), head_repository=copy.deepcopy(core[0]))
    core[4]['jobs'][0].update(head_sha=CORE, name=APPROVED.required_job,
        check_run_url='https://api.github.com/repos/' + APPROVED.repository + '/check-runs/33')
    core[5].update(head_sha=CORE, name=APPROVED.required_job)
    core[7] = copy.deepcopy(core[1]); core[8] = copy.deepcopy(core[0])
    metadata = {'id': APPROVED.workflow_id, 'path': APPROVED.workflow, 'state': 'active'}
    return native + [metadata] + core + copy.deepcopy(native) + core_documents() + copy.deepcopy(native)


class BrokerProposalTests(unittest.TestCase):
    def verify(self, values=None, approved=APPROVED, app=APP, runner=CORE):
        get = Mock(side_effect=documents() if values is None else values)
        clock = Mock(wraps=datetime); clock.now.return_value = datetime.fromisoformat('2026-10-24T15:10:00+00:00')
        with patch.object(gate, 'ONE_RELEASE_BROKER', approved), patch.object(gate, 'datetime', clock):
            receipts = gate.verify_deployment('NorthCountryEngineer/Game_Studio', app,
                'NorthCountryEngineer/NCE_Core', runner, read_json=get)
        return receipts, get

    def test_policy_records_actual_approval_and_bounded_expiry(self):
        self.assertEqual(gate.ONE_RELEASE_BROKER.approved_after, '2026-10-24T15:06:30Z')
        self.assertEqual(gate.ONE_RELEASE_BROKER.expires_at, '2026-10-25T15:06:30Z')
        self.assertIn('ref: ' + gate.ONE_RELEASE_BROKER.app_commit, WORKFLOW.read_text())
        self.assertIsNone(gate.ONE_RELEASE_BROKER.workflow_id)

    def test_unregistered_or_unapproved_policy_still_fails_closed(self):
        for changes in [{'workflow_id': None}, {'approved_after': None}, {'expires_at': None}]:
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(approved=replace(APPROVED, **changes))

    def test_exact_release_requires_normal_core_push_proof_and_fits_scoped_bound(self):
        receipts, get = self.verify()
        self.assertEqual(receipts[0]['commit'], APP)
        self.assertEqual(receipts[0]['execution_commit'], CORE)
        self.assertEqual(receipts[0]['policy'], 'one-release-core-broker-v1')
        self.assertEqual(receipts[1]['repository'], APPROVED.repository)
        self.assertEqual(get.call_count, 26)
        self.assertEqual(gate.MAX_REQUESTS, 20)
        self.assertEqual(gate.BROKER_MAX_REQUESTS, 32)

    def test_invalid_expired_future_or_overlong_window_is_refused(self):
        for changes in [{'expires_at': None}, {'approved_after': 'invalid'}, {'approved_after': '2026-10-24T16:00:00Z'},
                        {'expires_at': '2026-10-24T15:09:00Z'}, {'expires_at': '2026-10-26T14:42:20Z'}]:
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(approved=replace(APPROVED, **changes))

    def test_literal_checkout_and_workflow_bytes_are_pinned_without_inputs_secrets_or_cache(self):
        raw = WORKFLOW.read_bytes(); text = raw.decode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), APPROVED.workflow_sha256)
        self.assertIn('ref: ' + APP, text)
        self.assertIn('repository: NorthCountryEngineer/Game_Studio', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('contents: read', text)
        self.assertIn('  push:', text); self.assertIn('branches: [codex/game-studio-onboarding]', text)
        self.assertNotIn('  pull_request:', text); self.assertNotIn('workflow_dispatch:', text)
        for denied in ['inputs:', 'secrets.', 'id-token:', 'cache:', 'if:', 'continue-on-error:', 'aws-actions']:
            self.assertNotIn(denied, text)
        values = documents(); values[7]['content'] = base64.b64encode(raw + b'\n').decode(); values[7]['size'] += 1
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_failed_push_native_failure_or_started_jobs_are_never_bypassed(self):
        for changes in [{'event': 'push'}, {'status': 'completed', 'conclusion': 'failure'},
                        {'status': 'completed', 'conclusion': 'cancelled'}, {'status': 'in_progress'},
                        {'head_sha': 'c' * 40}, {'workflow_id': 1}, {'created_at': 'invalid'}, {'check_suite_id': True}]:
            values = documents(); values[2]['workflow_runs'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError): self.verify(values)
        values = documents(); values[3].update(total_count=1, jobs=[{'status': 'queued'}])
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_native_rechecks_catch_new_failed_run_started_jobs_main_or_owner_race(self):
        for index in [2, 16, 24]:
            values = documents(); values[index]['workflow_runs'][0].update(status='completed', conclusion='failure')
            with self.assertRaises(gate.CIGateError): self.verify(values)
        for index in [3, 17, 25]:
            values = documents(); values[index].update(total_count=1, jobs=[{}])
            with self.assertRaises(gate.CIGateError): self.verify(values)
        values = documents(); values[15]['object']['sha'] = 'c' * 40
        with self.assertRaises(gate.CIGateError): self.verify(values)
        values = documents(); values[14]['owner']['id'] = 1
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_core_head_actor_check_provider_and_steps_are_still_enforced(self):
        cases = [(6, lambda x: x['object'].update(sha='c' * 40)),
                 (8, lambda x: x['workflow_runs'][0]['triggering_actor'].update(id=1)),
                 (9, lambda x: x['jobs'][0]['steps'][0].update(conclusion='skipped')),
                 (10, lambda x: x['app'].update(id=1)),
                 (11, lambda x: x['workflow_runs'][0].update(run_attempt=3))]
        for index, mutate in cases:
            values = documents(); mutate(values[index])
            with self.subTest(index=index), self.assertRaises(gate.CIGateError): self.verify(values)

    def test_core_push_failure_still_blocks_after_valid_broker(self):
        values = documents(); values[19]['workflow_runs'][0]['conclusion'] = 'failure'
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_invalid_core_commit_and_exhausted_request_allowance_fail_closed(self):
        for bad in [None, 123, '', 'c' * 39]:
            with self.subTest(commit=bad), self.assertRaises(gate.CIGateError): self.verify(runner=bad)
        get = Mock(side_effect=documents())
        reader = gate._AdmissionReader(None, get, max_requests=25)
        clock = Mock(wraps=datetime); clock.now.return_value = datetime.fromisoformat('2026-10-24T15:10:00+00:00')
        with patch.object(gate, 'ONE_RELEASE_BROKER', APPROVED), patch.object(gate, 'datetime', clock):
            with self.assertRaisesRegex(gate.CIGateError, 'bounded request or time budget'):
                gate.verify_deployment('NorthCountryEngineer/Game_Studio', APP, APPROVED.repository, CORE, read_json=reader)
        self.assertEqual(get.call_count, 25)

    def test_alternative_is_never_selected_for_other_app_commits_or_repositories(self):
        get = Mock(side_effect=RuntimeError('normal path observed'))
        with patch.object(gate, 'ONE_RELEASE_BROKER', APPROVED):
            with self.assertRaisesRegex(RuntimeError, 'normal path observed'):
                gate.verify_deployment('NorthCountryEngineer/Game_Studio', 'c' * 40, APPROVED.repository, CORE, read_json=get)
        values = documents(); values[2]['total_count'] = 101
        with self.assertRaises(gate.CIGateError): self.verify(values)


if __name__ == '__main__': unittest.main()
