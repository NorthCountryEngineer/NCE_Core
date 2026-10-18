"""Actions provenance, stale-run, credentials and transport tests without network."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from platform_services import github_ci as gate


SHA = 'a' * 40
REPOSITORY = 'NorthCountryEngineer/Proposal_Studio'


def run(**changes):
    return {'id': 12, 'run_number': 4, 'run_attempt': 2, 'workflow_id': 7,
            'head_sha': SHA, 'event': 'push', 'path': '.github/workflows/ci.yml',
            'repository': {'full_name': REPOSITORY}, 'head_repository': {'full_name': REPOSITORY},
            'updated_at': '2026-10-16T12:30:00Z', 'status': 'completed', 'conclusion': 'success', **changes}


def documents():
    return [
        {'id': 7, 'path': '.github/workflows/ci.yml', 'state': 'active'},
        {'total_count': 1, 'workflow_runs': [run()]},
        {'total_count': 1, 'jobs': [{'id': 24, 'run_id': 12, 'head_sha': SHA,
            'name': 'Proposal Studio required checks', 'status': 'completed', 'conclusion': 'success'}]},
        {'total_count': 1, 'workflow_runs': [run()]},
    ]


class GitHubGateTests(unittest.TestCase):
    def test_academy_requires_its_own_exact_repository_and_job(self):
        values = documents()
        for listing in (values[1], values[3]):
            for item in listing['workflow_runs']:
                item['repository']['full_name'] = 'NorthCountryEngineer/NCAcademy'
                item['head_repository']['full_name'] = 'NorthCountryEngineer/NCAcademy'
        values[2]['jobs'][0]['name'] = 'NCAcademy required checks'
        receipt = gate.verify_repository('NorthCountryEngineer/NCAcademy', SHA, read_json=Mock(side_effect=values))
        self.assertEqual(receipt['repository'], 'NorthCountryEngineer/NCAcademy')
        values[2]['jobs'][0]['name'] = 'Proposal Studio required checks'
        with self.assertRaises(gate.CIGateError):
            gate.verify_repository('NorthCountryEngineer/NCAcademy', SHA, read_json=Mock(side_effect=values))

    def verify(self, values=None):
        get = Mock(side_effect=values or documents())
        result = gate.verify_repository(REPOSITORY, SHA, read_json=get)
        return result, get

    def test_exact_workflow_push_commit_and_attempt_specific_job_pass(self):
        receipt, get = self.verify()
        self.assertEqual(receipt['commit'], SHA)
        self.assertEqual(receipt['run_attempt'], 2)
        paths = [call.args[0] for call in get.call_args_list]
        self.assertIn('head_sha=' + SHA, paths[1])
        self.assertIn('event=push', paths[1])
        self.assertNotIn('status=success', paths[1])
        self.assertIn('/attempts/2/jobs?', paths[2])
        self.assertEqual(paths[1], paths[3])

    def test_unknown_application_does_not_silently_inherit_our_policy_or_read_credentials(self):
        with patch.object(gate, '_token', side_effect=AssertionError('No credential read')), patch.object(gate, '_read_json') as get:
            self.assertIsNone(gate.verify_repository('Other/app', SHA))
            get.assert_not_called()

    def test_invalid_sha_is_refused_before_network(self):
        for value in ['main', SHA.upper(), SHA + '\n', 'b' * 39]:
            with self.subTest(value=value), patch.object(gate, '_read_json') as get, self.assertRaises(gate.CIGateError):
                gate.verify_repository(REPOSITORY, value)
            get.assert_not_called()

    def test_workflow_identity_missing_disabled_or_wrong_path_fails_closed(self):
        for changes in [{'id': None}, {'state': 'disabled_manually'}, {'path': '.github/workflows/other.yml'}]:
            values = documents()
            values[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_run_provenance_wrong_sha_fork_repo_event_or_workflow_is_refused(self):
        for changes in [{'head_sha': 'b' * 40}, {'event': 'pull_request'}, {'workflow_id': 9},
                        {'path': '.github/workflows/copy.yml'}, {'repository': {'full_name': 'Other/app'}},
                        {'head_repository': {'full_name': 'Attacker/Proposal_Studio'}}, {'run_attempt': 0},
                        {'updated_at': 'invalid'}, {'updated_at': '2026-10-16T12:30:00'}]:
            values = documents()
            values[1]['workflow_runs'] = [run(**changes)]
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_missing_incomplete_and_oversize_run_listing_fails_closed(self):
        for changes in [{'total_count': 0, 'workflow_runs': []}, {'total_count': 101},
                        {'total_count': 2}, {'workflow_runs': None}]:
            values = documents()
            values[1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_newer_failed_pending_cancelled_or_skipped_run_overrides_old_success(self):
        for status, conclusion in [('queued', None), ('in_progress', None), ('completed', 'failure'),
                                   ('completed', 'cancelled'), ('completed', 'skipped'), ('completed', 'neutral')]:
            values = documents()
            values[1] = {'total_count': 2, 'workflow_runs': [run(), run(id=13, run_number=5,
                updated_at='2026-10-16T12:31:00Z', status=status, conclusion=conclusion)]}
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_recent_failed_rerun_of_older_run_beats_newer_successful_run(self):
        values = documents()
        values[1] = {'total_count': 2, 'workflow_runs': [run(), run(id=11, run_number=3, run_attempt=3,
            updated_at='2026-10-16T12:32:00Z', conclusion='failure')]}
        with self.assertRaises(gate.CIGateError):
            self.verify(values)

    def test_required_aggregate_must_be_unique_success_for_exact_run_and_sha(self):
        for changes in [{'name': 'Other checks'}, {'run_id': 13}, {'head_sha': 'b' * 40},
                        {'status': 'queued'}, {'conclusion': 'failure'}, {'conclusion': 'skipped'}]:
            values = documents()
            values[2]['jobs'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(values)
        for jobs in [[], documents()[2]['jobs'] * 2]:
            values = documents()
            values[2] = {'total_count': len(jobs), 'jobs': jobs}
            with self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_malformed_job_count_and_unapproved_runner_cannot_pass(self):
        values = documents()
        values[2]['total_count'] = True
        with self.assertRaises(gate.CIGateError):
            self.verify(values)
        with patch.object(gate, 'verify_repository') as verify, self.assertRaises(gate.CIGateError):
            gate.verify_deployment(REPOSITORY, SHA, 'Attacker/runner', 'b' * 40)
        verify.assert_not_called()

    def test_run_changed_during_job_lookup_requires_new_verification(self):
        for changes in [{'run_attempt': 3}, {'id': 13}, {'updated_at': '2026-10-16T12:32:00Z'}, {'status': 'in_progress'}]:
            values = documents()
            values[3]['workflow_runs'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_app_and_trusted_core_runner_both_require_passing_ci(self):
        with patch.object(gate, 'verify_repository', side_effect=[{'commit': SHA}, gate.CIGateError('runner failed')]) as verify:
            with self.assertRaisesRegex(gate.CIGateError, 'runner failed'):
                gate.verify_deployment(REPOSITORY, SHA, 'NorthCountryEngineer/NCE_Core', 'b' * 40, token='operator_token')
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args.args, ('NorthCountryEngineer/NCE_Core', 'b' * 40))

    def test_operator_file_or_explicit_server_token_only_never_ambient_gh_credentials(self):
        token = 'ghp_' + 'x' * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text(token)
            with patch.dict(os.environ, {'CORE_CI_GITHUB_TOKEN_FILE': str(path), 'GH_TOKEN': 'ignored', 'GITHUB_TOKEN': 'ignored'}):
                self.assertEqual(gate._token(), token)
                path.write_text('hidden secret\nwith spaces')
                with self.assertRaises(gate.CIGateError) as error:
                    gate._token()
                self.assertNotIn('hidden secret', str(error.exception))
        with patch.dict(os.environ, {'CORE_CI_GITHUB_TOKEN_FILE': '', 'GH_TOKEN': token, 'GITHUB_TOKEN': token}):
            self.assertEqual(gate._token(), '')
        with patch.object(gate, '_token', side_effect=AssertionError('No file fallback')), patch.object(gate, '_read_json', side_effect=documents()) as get:
            gate.verify_repository(REPOSITORY, SHA, token=token)
            self.assertTrue(all(call.kwargs['token'] == token for call in get.call_args_list))

    def test_transport_is_fixed_host_authenticated_bounded_no_redirect_or_proxy_and_closed(self):
        raw = io.BytesIO(b'{"id": 7}')
        response = Mock(status=200, read=raw.read)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(gate, 'build_opener', return_value=opener) as build:
            self.assertEqual(gate._read_json('/repos/approved/repo/actions/workflows/ci.yml', token='token'), {'id': 7})
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://api.github.com/repos/'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], gate._NoRedirect)
        response.__exit__.assert_called_once()
        with self.assertRaises(gate.CIGateError):
            gate._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_transport_failures_and_response_bodies_never_echo_secrets(self):
        for raw in [b'invalid response hidden-key', b'[]', b' ' * (gate.MAX_RESPONSE + 1)]:
            response = Mock(status=200, read=Mock(return_value=raw))
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.return_value = response
            with patch.object(gate, 'build_opener', return_value=opener), self.assertRaises(gate.CIGateError) as error:
                gate._read_json('/fixed/path', token='hidden-key')
            self.assertNotIn('hidden-key', str(error.exception))
        opener = Mock()
        opener.open.side_effect = RuntimeError('credential-hidden')
        with patch.object(gate, 'build_opener', return_value=opener), self.assertRaises(gate.CIGateError) as error:
            gate._read_json('/fixed/path', token='credential-hidden')
        self.assertNotIn('credential-hidden', str(error.exception))
