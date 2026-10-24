"""Fail-closed, offline tests for the approved Game Studio owner-dispatch policy."""
import base64
import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from platform_services import github_ci as gate
from ci.tests.test_github_gate import core_documents

SHA = 'a' * 40
REPOSITORY = 'NorthCountryEngineer/Game_Studio'
FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/game-studio-dispatch-ci.yml'
APPROVED = gate.OWNER_DISPATCH_PROPOSAL
EMPTY = {'total_count': 0, 'workflow_runs': []}


def documents():
    repo = {'id': APPROVED.repository_id, 'full_name': REPOSITORY, 'default_branch': 'main',
            'owner': {'id': APPROVED.owner_id, 'type': 'User'}}
    reference = {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': SHA}}
    raw = FIXTURE.read_bytes()
    blob = {'type': 'file', 'path': '.github/workflows/ci.yml', 'encoding': 'base64',
            'size': len(raw), 'content': base64.b64encode(raw).decode()}
    run = {'id': 22, 'run_number': 3, 'run_attempt': 2, 'workflow_id': APPROVED.workflow_id,
           'head_sha': SHA, 'head_branch': 'main', 'event': 'workflow_dispatch',
           'path': '.github/workflows/ci.yml', 'repository': repo, 'head_repository': repo,
           'actor': {'id': APPROVED.owner_id, 'type': 'User'},
           'triggering_actor': {'id': APPROVED.owner_id, 'type': 'User'},
           'created_at': '2026-10-24T14:43:00Z', 'updated_at': '2026-10-24T14:44:00Z',
           'status': 'completed', 'conclusion': 'success', 'check_suite_id': 44}
    listing = {'total_count': 1, 'workflow_runs': [run]}
    job = {'id': 33, 'run_id': 22, 'head_sha': SHA, 'name': 'Game Studio required checks',
           'status': 'completed', 'conclusion': 'success',
           'check_run_url': 'https://api.github.com/repos/' + REPOSITORY + '/check-runs/33',
           'steps': [{'name': name, 'number': i + 1, 'status': 'completed', 'conclusion': 'success'}
                     for i, name in enumerate(gate._DISPATCH_STEPS)]}
    check = {'id': 33, 'head_sha': SHA, 'name': job['name'], 'status': 'completed', 'conclusion': 'success',
             'check_suite': {'id': 44}, 'app': {'id': 15368, 'slug': 'github-actions',
             'owner': {'id': 9919, 'login': 'github'}}}
    return [copy.deepcopy(value) for value in [
        {'id': APPROVED.workflow_id, 'path': '.github/workflows/ci.yml', 'state': 'active'},
        EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, repo, reference, blob, listing,
        {'total_count': 1, 'jobs': [job]}, check, listing, reference, repo, EMPTY,
    ]]


class OwnerDispatchProposalTests(unittest.TestCase):
    def verify(self, values=None):
        get = Mock(side_effect=values or documents())
        with patch.object(gate, 'OWNER_DISPATCH_PROPOSAL', APPROVED):
            result = gate.verify_repository(REPOSITORY, SHA, read_json=get)
        return result, get

    def refused(self, mutate, index):
        values = documents()
        mutate(values[index])
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_policy_records_the_actual_owner_approval_cutoff(self):
        self.assertEqual(gate.OWNER_DISPATCH_PROPOSAL.approved_after, '2026-10-24T14:42:20Z')

    def test_policy_is_inactive_without_a_reviewed_owner_approval_timestamp(self):
        get = Mock(side_effect=documents())
        with patch.object(gate, 'OWNER_DISPATCH_PROPOSAL', replace(APPROVED, approved_after=None)):
            with self.assertRaises(gate.CIGateError): gate.verify_repository(REPOSITORY, SHA, read_json=get)
        self.assertEqual(get.call_count, 6)
        self.assertFalse(any('workflow_dispatch' in call.args[0] for call in get.call_args_list))

    def test_approved_owner_dispatch_passes_only_after_bounded_push_absence(self):
        receipt, get = self.verify()
        self.assertEqual(receipt['event'], 'workflow_dispatch')
        self.assertEqual(receipt['workflow_sha256'], APPROVED.workflow_sha256)
        self.assertEqual(receipt['owner_id'], 132168834)
        self.assertEqual(get.call_count, 16)
        self.assertEqual(sum('event=push' in call.args[0] for call in get.call_args_list), 6)
        paths = [call.args[0] for call in get.call_args_list]
        dispatch = [p for p in paths if 'event=workflow_dispatch' in p]
        self.assertEqual(len(dispatch), 2)
        for path in dispatch:
            query = parse_qs(urlsplit(path).query)
            self.assertEqual(query['head_sha'], [SHA]); self.assertEqual(query['page'], ['1'])
        self.assertTrue(paths[-1].find('event=push') > 0)
        self.assertTrue('_nonce=' in paths[-1])

    def test_combined_owner_dispatch_and_normal_core_source_fit_unchanged_twenty_read_budget(self):
        get = Mock(side_effect=documents() + core_documents())
        with patch.object(gate, 'OWNER_DISPATCH_PROPOSAL', APPROVED):
            receipts = gate.verify_deployment(REPOSITORY, SHA, 'NorthCountryEngineer/NCE_Core', 'b' * 40, read_json=get)
        self.assertEqual(get.call_count, 20)
        self.assertEqual(len(receipts), 2)

    def test_failed_malformed_or_pending_push_never_enters_dispatch_path(self):
        cases = [None, {'total_count': False, 'workflow_runs': []},
                 {'total_count': 1, 'workflow_runs': [{'event': 'push'}]}]
        for bad in cases:
            values = documents(); values[1] = bad
            with self.subTest(bad=bad), self.assertRaises(gate.CIGateError): self.verify(values)
        # A known failing push remains authoritative even with a dispatch proposal.
        from ci.tests.test_github_gate import documents as push_documents
        values = push_documents()
        for row in (values[1], values[3]):
            row['workflow_runs'][0].update(repository={'full_name': REPOSITORY}, head_repository={'full_name': REPOSITORY})
        values[1]['workflow_runs'][0]['conclusion'] = 'failure'
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_dispatch_exception_cannot_apply_to_other_repositories_or_workflow_ids(self):
        values = documents(); get = Mock(side_effect=values)
        with patch.object(gate, 'OWNER_DISPATCH_PROPOSAL', APPROVED), self.assertRaises(gate.CIGateError):
            gate.verify_repository('NorthCountryEngineer/NCAcademy', SHA, read_json=get)
        self.assertLessEqual(get.call_count, 6)
        values = documents(); values[0]['id'] += 1
        with self.assertRaises(gate.CIGateError): self.verify(values)

    def test_repository_numeric_id_owner_default_branch_and_current_head_are_bound(self):
        for key, bad in [('id', 123), ('id', float(APPROVED.repository_id)), ('full_name', 'Attacker/Game_Studio'), ('default_branch', 'development')]:
            self.refused(lambda value, key=key, bad=bad: value.update({key: bad}), 6)
        for key, bad in [('id', 123), ('id', float(APPROVED.owner_id)), ('type', 'Organization')]:
            self.refused(lambda value, key=key, bad=bad: value['owner'].update({key: bad}), 6)
        for key, bad in [('sha', 'b' * 40), ('type', 'tag')]:
            self.refused(lambda value, key=key, bad=bad: value['object'].update({key: bad}), 7)

    def test_exact_reviewed_workflow_blob_is_pinned_and_has_unconditional_checkout_and_checks(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), APPROVED.workflow_sha256)
        lines = raw.decode().splitlines()
        self.assertIn('  required:', lines)
        self.assertIn('    name: Game Studio required checks', lines)
        self.assertEqual(next(line.strip() for line in lines if 'uses:' in line), '- uses: actions/checkout@v4')
        self.assertFalse(any(line.lstrip().startswith(('if:', 'continue-on-error:')) for line in lines))
        for key, bad in [('path', '.github/workflows/other.yml'), ('type', 'symlink'), ('encoding', 'utf8'), ('size', True)]:
            self.refused(lambda value, key=key, bad=bad: value.update({key: bad}), 8)
        self.refused(lambda value: value.update(content=base64.b64encode(raw + b'\n').decode(), size=len(raw) + 1), 8)
        self.refused(lambda value: value.update(content='invalid base64!!'), 8)

    def test_actor_and_triggering_actor_must_both_be_exact_owner_user(self):
        for field in ['actor', 'triggering_actor']:
            for bad in [None, {'id': 999, 'type': 'User'}, {'id': float(APPROVED.owner_id), 'type': 'User'}, {'id': APPROVED.owner_id, 'type': 'Bot'}]:
                self.refused(lambda value, field=field, bad=bad: value['workflow_runs'][0].update({field: bad}), 9)

    def test_runs_created_before_equal_or_without_approval_timezone_are_refused_even_after_rerun(self):
        for created in ['2026-10-24T14:42:19Z', APPROVED.approved_after, '2026-10-24T14:43:00', 'invalid']:
            self.refused(lambda value, created=created: value['workflow_runs'][0].update(created_at=created), 9)
        with patch.object(gate, 'OWNER_DISPATCH_PROPOSAL', replace(APPROVED, approved_after='invalid')):
            with self.assertRaises(gate.CIGateError): gate.verify_repository(REPOSITORY, SHA, read_json=Mock(side_effect=documents()))

    def test_foreign_event_commit_repo_branch_workflow_or_duplicate_run_is_refused(self):
        for changes in [{'event': 'push'}, {'head_sha': 'b' * 40}, {'head_branch': 'other'},
                        {'workflow_id': 1}, {'workflow_id': float(APPROVED.workflow_id)}, {'path': '.github/workflows/other.yml'},
                        {'repository': {'full_name': REPOSITORY, 'id': 999}},
                        {'head_repository': {'full_name': 'Attacker/Game_Studio', 'id': APPROVED.repository_id}}]:
            self.refused(lambda value, changes=changes: value['workflow_runs'][0].update(changes), 9)
        self.refused(lambda value: value.update(total_count=2, workflow_runs=value['workflow_runs'] * 2), 9)
        self.refused(lambda value: value.update(total_count=101), 9)

    def test_latest_pending_failed_skipped_cancelled_or_untrusted_attempt_blocks_older_success(self):
        for status, conclusion in [('queued', None), ('in_progress', None), ('completed', 'failure'), ('completed', 'cancelled'), ('completed', 'skipped')]:
            values = documents(); latest = copy.deepcopy(values[9]['workflow_runs'][0]); latest.update(id=23, run_number=4, updated_at='2026-10-24T14:45:00Z', status=status, conclusion=conclusion)
            values[9].update(total_count=2, workflow_runs=[values[9]['workflow_runs'][0], latest])
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(gate.CIGateError): self.verify(values)
        self.refused(lambda value: value['workflow_runs'][0].update(triggering_actor={'id': 999, 'type': 'User'}), 12)

    def test_every_required_job_and_step_must_execute_successfully_for_the_exact_attempt(self):
        for changes in [{'conclusion': 'failure'}, {'head_sha': 'b' * 40}, {'run_id': 999}, {'name': 'Other checks'}]:
            self.refused(lambda value, changes=changes: value['jobs'][0].update(changes), 10)
        for status in ['skipped', 'failure', 'cancelled', 'neutral']:
            self.refused(lambda value, status=status: value['jobs'][0]['steps'][0].update(conclusion=status), 10)
        self.refused(lambda value: value['jobs'][0]['steps'].pop(0), 10)
        self.refused(lambda value: value['jobs'][0]['steps'].append(copy.deepcopy(value['jobs'][0]['steps'][0])), 10)
        self.refused(lambda value: value.update(total_count=True), 10)
        self.refused(lambda value: value.update(total_count=2, jobs=value['jobs'] * 2), 10)

    def test_optional_runner_cleanup_may_be_skipped_but_required_checks_cannot(self):
        values = documents()
        values[10]['jobs'][0]['steps'].append({'name': 'Post Run optional cache cleanup', 'number': 99,
                                               'status': 'completed', 'conclusion': 'skipped'})
        receipt, _ = self.verify(values)
        self.assertEqual(receipt['event'], 'workflow_dispatch')

    def test_required_check_must_be_github_actions_not_a_forged_check_provider(self):
        for changes in [{'id': 999}, {'head_sha': 'b' * 40}, {'conclusion': 'failure'}, {'check_suite': {'id': 999}}]:
            self.refused(lambda value, changes=changes: value.update(changes), 11)
        for changes in [{'id': 123}, {'id': 15368.0}, {'slug': 'other-actions'}, {'owner': {'id': 123, 'login': 'github'}}]:
            self.refused(lambda value, changes=changes: value['app'].update(changes), 11)
        self.refused(lambda value: value['jobs'][0].update(check_run_url='https://other.example/checks/33'), 10)

    def test_fresh_recheck_refuses_new_attempt_updated_run_branch_owner_or_new_push(self):
        for changes in [{'run_attempt': 3}, {'id': 23}, {'updated_at': '2026-10-24T14:45:00Z'}]:
            self.refused(lambda value, changes=changes: value['workflow_runs'][0].update(changes), 12)
        self.refused(lambda value: value['object'].update(sha='b' * 40), 13)
        self.refused(lambda value: value.update(default_branch='other'), 14)
        self.refused(lambda value: value.update(total_count=1, workflow_runs=[{'event': 'push'}]), 15)


if __name__ == '__main__': unittest.main()
