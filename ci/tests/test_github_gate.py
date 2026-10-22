"""Actions provenance, stale-run, credentials and transport tests without network."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit

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


def fallback_documents(runs=None):
    values = documents()
    listing = {'total_count':len(runs or [run()]), 'workflow_runs':runs or [run()]}
    return [values[0], {'total_count':0,'workflow_runs':[]}, copy.deepcopy(listing), values[2], copy.deepcopy(listing)]


def without_nonce(path):
    part = urlsplit(path)
    return part.path + ('?' + urlencode([(key,value) for key,value in parse_qsl(part.query) if key != '_nonce']) if part.query else '')


def core_documents():
    values = documents()
    values[0].update(id=8,path='.github/workflows/verify.yml')
    for value in (values[1],values[3]):
        value['workflow_runs'] = [run(id=112,workflow_id=8,head_sha='b'*40,path='.github/workflows/verify.yml',
            repository={'full_name':'NorthCountryEngineer/NCE_Core'},head_repository={'full_name':'NorthCountryEngineer/NCE_Core'})]
    values[2]['jobs'][0].update(run_id=112,head_sha='b'*40,name='Core required checks')
    return values


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
        self.assertEqual(paths[1], without_nonce(paths[3]))
        self.assertIn('_nonce=', paths[3])

    def test_empty_workflow_index_uses_bounded_repository_lookup_and_stays_on_that_index(self):
        other = run(id=40,workflow_id=8,path='.github/workflows/other.yml',run_number=8,
                    status='in_progress',conclusion=None,updated_at='2026-10-16T13:00:00Z')
        receipt, get = self.verify(fallback_documents([other,run()]))
        self.assertEqual(receipt['run_id'],12)
        paths = [call.args[0] for call in get.call_args_list]
        self.assertEqual(len(paths),5)
        self.assertIn('/actions/workflows/7/runs?',paths[1])
        self.assertIn('/actions/runs?head_sha='+SHA+'&event=push&per_page=100&page=1',paths[2])
        self.assertEqual(paths[2],without_nonce(paths[4]))  # Recheck the complete repository index.
        self.assertNotIn('status=success',paths[2])

    def test_only_well_formed_empty_workflow_listing_can_trigger_fallback(self):
        for invalid in [{'total_count':False,'workflow_runs':[]}, {'total_count':0,'workflow_runs':[run()]},
                        {'total_count':1,'workflow_runs':[]}, {'workflow_runs':[]},
                        {'total_count':0,'workflow_runs':None}]:
            get = Mock(side_effect=[documents()[0],invalid])
            with self.subTest(invalid=invalid),self.assertRaises(gate.CIGateError):
                gate.verify_repository(REPOSITORY,SHA,read_json=get)
            self.assertEqual(get.call_count,2)

    def test_fallback_validates_every_row_before_ignoring_other_workflows(self):
        other = run(id=40,workflow_id=8,path='.github/workflows/other.yml')
        for changes in [{'head_sha':'b'*40}, {'event':'pull_request'},
                        {'repository':{'full_name':'Other/app'}}, {'head_repository':{'full_name':'Attacker/app'}},
                        {'id':False}, {'workflow_id':0}, {'run_attempt':0}, {'run_number':0},
                        {'path':''}, {'updated_at':'invalid'}, {'updated_at':'2026-10-16T12:00:00'}]:
            with self.subTest(changes=changes),self.assertRaises(gate.CIGateError):
                self.verify(fallback_documents([run(),{**other,**changes}]))

    def test_fallback_rejects_trusted_workflow_identity_crossovers(self):
        for changes in [{'path':'.github/workflows/other.yml'}, {'workflow_id':8}]:
            with self.subTest(changes=changes),self.assertRaises(gate.CIGateError):
                self.verify(fallback_documents([run(),run(id=40,**changes)]))

    def test_fallback_unrelated_success_cannot_mask_newer_pending_or_failed_trusted_run(self):
        other = run(id=40,workflow_id=8,path='.github/workflows/other.yml',run_number=8,
                    updated_at='2026-10-16T14:00:00Z')
        for status,conclusion in [('queued',None),('in_progress',None),('completed','failure'),
                                  ('completed','cancelled'),('completed','skipped')]:
            latest = run(id=13,run_number=5,status=status,conclusion=conclusion,
                         updated_at='2026-10-16T13:00:00Z')
            with self.subTest(status=status,conclusion=conclusion),self.assertRaises(gate.CIGateError):
                self.verify(fallback_documents([run(),other,latest]))

    def test_fallback_requires_trusted_workflow_and_rejects_duplicate_ids_or_incomplete_listing(self):
        other = run(id=40,workflow_id=8,path='.github/workflows/other.yml')
        for runs in [[other], [run(),run()], [run(),run(conclusion='failure')]]:
            with self.subTest(runs=runs),self.assertRaises(gate.CIGateError):
                self.verify(fallback_documents(runs))
        for total in [False,0,2,101]:
            values = fallback_documents()
            values[2]['total_count'] = total
            with self.subTest(total=total),self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_fallback_recheck_sees_new_pending_run_and_changed_attempt(self):
        for changes in [{'id':13,'run_number':5,'status':'in_progress','conclusion':None},
                        {'run_attempt':3}, {'updated_at':'2026-10-16T13:00:00Z'}]:
            values = fallback_documents()
            values[4] = {'total_count':2,'workflow_runs':[run(),run(**changes)]}
            with self.subTest(changes=changes),self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_fallback_still_requires_successful_aggregate_for_exact_run_and_sha(self):
        for changes in [{'head_sha':'b'*40}, {'run_id':13}, {'name':'Other checks'},
                        {'status':'in_progress'}, {'conclusion':'failure'}, {'conclusion':'skipped'}]:
            values = fallback_documents()
            values[3]['jobs'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(gate.CIGateError):
                self.verify(values)

    def test_empty_repository_index_can_recover_on_third_fresh_read_then_requires_fresh_consistency(self):
        values = documents()
        empty = {'total_count':0,'workflow_runs':[]}
        receipt,get = self.verify([values[0],empty,empty,empty,empty,values[1],values[2],values[3]])
        self.assertEqual(receipt['run_id'],12)
        paths = [call.args[0] for call in get.call_args_list]
        self.assertEqual(len(paths),8)
        fresh = [path for path in paths if '_nonce=' in path]
        self.assertEqual(len(fresh),4)
        nonces = []
        for path in fresh:
            query = dict(parse_qsl(urlsplit(path).query))
            self.assertEqual(query['head_sha'],SHA)
            self.assertEqual(query['event'],'push')
            self.assertEqual(query['page'],'1')
            self.assertEqual(query['per_page'],'100')
            self.assertRegex(query['_nonce'],r'^[a-f0-9]{32}$')
            nonces.append(query['_nonce'])
            self.assertIn('/actions/runs?',path)
        self.assertEqual(len(set(nonces)),4)

    def test_persistent_empty_is_refused_after_exactly_three_fresh_rereads(self):
        empty = {'total_count':0,'workflow_runs':[]}
        get = Mock(side_effect=[documents()[0],empty,empty,empty,empty,empty])
        with self.assertRaisesRegex(gate.CIGateError,'No bounded, complete'):
            gate.verify_repository(REPOSITORY,SHA,read_json=get)
        self.assertEqual(get.call_count,6)
        paths = [call.args[0] for call in get.call_args_list]
        self.assertEqual(sum('_nonce=' in path for path in paths),3)
        self.assertFalse(any('/jobs?' in path for path in paths))

    def test_final_empty_recheck_recovers_without_reusing_the_initial_success(self):
        values = fallback_documents()
        empty = {'total_count':0,'workflow_runs':[]}
        receipt,get = self.verify(values[:4]+[empty,empty,values[4]])
        self.assertEqual(receipt['run_id'],12)
        self.assertEqual(get.call_count,7)
        self.assertTrue(all('_nonce=' in call.args[0] and '/actions/runs?' in call.args[0]
                            for call in get.call_args_list[4:]))
        get = Mock(side_effect=values[:4]+[empty]*4)
        with self.assertRaisesRegex(gate.CIGateError,'No bounded, complete'):
            gate.verify_repository(REPOSITORY,SHA,read_json=get)
        self.assertEqual(get.call_count,8)

    def test_empty_then_nonempty_pending_failed_malformed_or_transport_error_is_never_retried(self):
        empty = {'total_count':0,'workflow_runs':[]}
        failures = [None,[],{'total_count':False,'workflow_runs':[]},{'total_count':2,'workflow_runs':[run()]},
                    {'total_count':1,'workflow_runs':[run(status='queued',conclusion=None)]},
                    {'total_count':1,'workflow_runs':[run(conclusion='failure')]},
                    gate.CIGateError('transport unavailable')]
        for response in failures:
            get = Mock(side_effect=[documents()[0],empty,empty,response])
            with self.subTest(response=response),self.assertRaises(gate.CIGateError):
                gate.verify_repository(REPOSITORY,SHA,read_json=get)
            self.assertEqual(get.call_count,4)

    def test_empty_final_recheck_cannot_hide_a_new_pending_run_or_changed_attempt(self):
        values = fallback_documents()
        empty = {'total_count':0,'workflow_runs':[]}
        for changes in [{'status':'queued','conclusion':None},{'conclusion':'failure'},
                        {'run_attempt':3},{'id':13,'run_number':5},{'updated_at':'2026-10-16T13:00:00Z'}]:
            latest = {'total_count':1,'workflow_runs':[run(**changes)]}
            get = Mock(side_effect=values[:4]+[empty,latest])
            with self.subTest(changes=changes),self.assertRaises(gate.CIGateError):
                gate.verify_repository(REPOSITORY,SHA,read_json=get)
            self.assertEqual(get.call_count,6)

    def test_two_source_admission_shares_one_forty_second_deadline(self):
        values = documents()+core_documents()
        clock = [0.0]
        def read(path):
            clock[0] += 6
            return values.pop(0)
        get = Mock(side_effect=read)
        with patch.object(gate.time,'monotonic',side_effect=lambda:clock[0]),self.assertRaisesRegex(gate.CIGateError,'time budget'):
            gate.verify_deployment(REPOSITORY,SHA,'NorthCountryEngineer/NCE_Core','b'*40,read_json=get)
        self.assertEqual(get.call_count,7)
        self.assertEqual(clock[0],42)

    def test_two_source_admission_never_exceeds_twenty_requests(self):
        empty = {'total_count':0,'workflow_runs':[]}
        def worst(values):
            return [values[0],empty,empty,empty,empty,values[1],values[2],empty,empty,empty,values[3]]
        get = Mock(side_effect=worst(documents())+worst(core_documents()))
        with self.assertRaisesRegex(gate.CIGateError,'request or time budget'):
            gate.verify_deployment(REPOSITORY,SHA,'NorthCountryEngineer/NCE_Core','b'*40,read_json=get)
        self.assertEqual(get.call_count,20)

    def test_transport_timeout_uses_the_remaining_admission_budget_and_no_late_response_is_accepted(self):
        values = documents()
        clock = [0.0]
        def read(path,**kwargs):
            clock[0] += 12
            return values.pop(0)
        get = Mock(side_effect=read)
        with patch.object(gate.time,'monotonic',side_effect=lambda:clock[0]),patch.object(gate,'_read_json',get),self.assertRaisesRegex(gate.CIGateError,'time budget'):
            gate.verify_repository(REPOSITORY,SHA,token='ghp_'+'x'*40)
        self.assertEqual([call.kwargs['timeout'] for call in get.call_args_list],[10,10,10,4])

    def test_expired_budget_prevents_a_new_read_and_ordinary_two_source_admission_passes(self):
        clock = [0.0]
        get = Mock(side_effect=documents()+core_documents())
        with patch.object(gate.time,'monotonic',side_effect=lambda:clock[0]):
            reader = gate._AdmissionReader(None,get)
            clock[0] = 40.0
            with self.assertRaisesRegex(gate.CIGateError,'time budget'):
                reader('/fixed/path')
        get.assert_not_called()
        receipts = gate.verify_deployment(REPOSITORY,SHA,'NorthCountryEngineer/NCE_Core','b'*40,
                                           read_json=Mock(side_effect=documents()+core_documents()))
        self.assertEqual([row['repository'] for row in receipts],[REPOSITORY,'NorthCountryEngineer/NCE_Core'])

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
        response = Mock(status=200, read1=raw.read)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(gate, 'build_opener', return_value=opener) as build:
            self.assertEqual(gate._read_json('/repos/approved/repo/actions/workflows/ci.yml', token='token'), {'id': 7})
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://api.github.com/repos/'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer token')
        self.assertEqual(request.get_header('Cache-control'),'no-cache')
        self.assertEqual(request.get_header('Pragma'),'no-cache')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], gate._NoRedirect)
        response.__exit__.assert_called_once()
        with self.assertRaises(gate.CIGateError):
            gate._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_transport_failures_and_response_bodies_never_echo_secrets(self):
        for raw in [b'invalid response hidden-key', b'[]', b' ' * (gate.MAX_RESPONSE + 1)]:
            response = Mock(status=200, read1=Mock(side_effect=[raw,b'']))
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

    def test_transport_checks_deadline_between_body_chunks_and_refuses_a_late_response(self):
        clock = [0.0]
        chunks = [b'{"id":',b'7}',b'']
        def read(size):
            self.assertLessEqual(size,65536)
            clock[0] += 4
            return chunks.pop(0)
        response = Mock(status=200,read1=Mock(side_effect=read))
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock();opener.open.return_value = response
        with patch.object(gate.time,'monotonic',side_effect=lambda:clock[0]),patch.object(gate,'build_opener',return_value=opener),self.assertRaises(gate.CIGateError):
            gate._read_json('/fixed/path',token='secret-hidden',timeout=6)
        self.assertEqual(response.read1.call_count,2)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],6)
        response.__exit__.assert_called_once()
