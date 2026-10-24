"""Read-only, exact-commit GitHub Actions admission for trusted release sources.

Policies belong to Core source, never application manifests or browser input.
Only a server-owned encrypted credential or operator token file authenticates private reads.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class CIGateError(Exception):
    """A sanitized deployment refusal; response bodies and credentials stay private."""


class _NoPushEvidence(CIGateError):
    """Only a complete, repeatedly empty push index permits a proposed alternative."""


@dataclass(frozen=True)
class OwnerDispatchPolicy:
    repository: str
    repository_id: int
    owner_id: int
    workflow_id: int
    default_branch: str
    workflow_sha256: str
    approved_after: str | None = None


# Owner approved this exact Game Studio policy at the UTC cutoff below.
# Only NEW owner-dispatched runs created strictly after that approval qualify.
# Application input, environment variables and operators' tokens cannot change
# the repository, workflow, owner, reviewed workflow bytes or approval cutoff.
OWNER_DISPATCH_PROPOSAL = OwnerDispatchPolicy(
    repository='NorthCountryEngineer/Game_Studio', repository_id=1432213176,
    owner_id=132168834, workflow_id=395127780, default_branch='main',
    # Public SHA256 of the reviewed GitHub workflow bytes; no credential.
    workflow_sha256='3f0f05be8da896670a53642c3189b24a7bb81011745529720481842b8d7b75a2',  # pragma: allowlist secret
    approved_after='2026-10-24T14:42:20Z',
)
_DISPATCH_STEPS = (
    'Run actions/checkout@v4', 'Run actions/setup-node@v4', 'Run actions/setup-python@v5',
    'Run npm ci --no-audit --no-fund', 'Run npm run typecheck', 'Run npm test',
    "Run python3 -m unittest discover -s tests -p 'test_*.py'",
    "Run python3 -m unittest discover -s scripts/tests -p 'test_*.py'",
    'Run python3 scripts/validate-deployment.py', 'Run python3 scripts/package-api.py',
    'Verify shared registry source', 'Run npm run build:preview',
    'Build production with synthetic public registration',
)


@dataclass(frozen=True)
class Policy:
    repository: str
    workflow: str
    required_job: str


POLICIES = (
    Policy('NorthCountryEngineer/Game_Studio', '.github/workflows/ci.yml', 'Game Studio required checks'),
    Policy('NorthCountryEngineer/NCAcademy', '.github/workflows/ci.yml', 'NCAcademy required checks'),
    Policy('NorthCountryEngineer/Proposal_Studio', '.github/workflows/ci.yml', 'Proposal Studio required checks'),
    Policy('NorthCountryEngineer/NCE_Core', '.github/workflows/verify.yml', 'Core required checks'),
)
SHA = re.compile(r'[a-f0-9]{40}\Z')
MAX_RESPONSE = 2 * 1024 * 1024
ADMISSION_SECONDS = 40.0
MAX_REQUESTS = 20
EMPTY_RETRIES = 3


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CIGateError('GitHub CI verification refused a redirect.')


def _token():
    filename = os.environ.get('CORE_CI_GITHUB_TOKEN_FILE', '')
    if not filename:
        return ''
    try:
        with Path(filename).open(encoding='ascii') as stream:
            value = stream.read(513).strip()
        if not re.fullmatch(r'[A-Za-z0-9_]{20,512}', value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise CIGateError('The operator GitHub CI credential file is unavailable or invalid.') from None


def _read_json(path, *, token, timeout=10):
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'NCE-Core-CI-Gate', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    # No environment proxy, .netrc credential fallback, response URL, or log output.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        deadline = time.monotonic() + timeout
        with opener.open(Request('https://api.github.com' + path, headers=headers), timeout=timeout) as response:
            if response.status != 200:
                raise ValueError
            raw = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                # HTTPResponse.read1 makes at most one underlying body read.
                # Recheck the decision deadline between bounded body chunks;
                # transport inactivity retains the request's socket timeout.
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - len(raw)))
                if time.monotonic() >= deadline:
                    raise ValueError
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError
            if time.monotonic() >= deadline:
                raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError
        return document
    except CIGateError:
        raise
    except Exception:
        raise CIGateError('GitHub CI verification is unavailable. Private repositories require an operator credential with Actions read access; retry after checking GitHub access or rate limits.') from None


class _AdmissionReader:
    """One request/time allowance shared by both protected release sources."""

    def __init__(self, token, read_json):
        self.token, self.read_json = token, read_json
        self.deadline, self.requests = time.monotonic() + ADMISSION_SECONDS, 0

    def __call__(self, path, *, fresh=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= MAX_REQUESTS:
            raise CIGateError('GitHub CI verification exceeded its bounded request or time budget.')
        if fresh:
            # A distinct public query key requests another view of an index;
            # exact SHA/event/page filters and the fixed GitHub host stay intact.
            path += ('&' if '?' in path else '?') + '_nonce=' + secrets.token_hex(16)
        self.requests += 1
        if self.read_json is None:
            token = _token() if self.token is None else self.token
            if token and (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_]{20,512}', token)):
                raise CIGateError('The operator GitHub CI credential is invalid.')
            self.token = token
            document = _read_json(path, token=token, timeout=min(10, remaining))
        else:
            document = self.read_json(path)
        if time.monotonic() >= self.deadline:
            raise CIGateError('GitHub CI verification exceeded its bounded request or time budget.')
        return document


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _repo(value, expected):
    return isinstance(value, dict) and isinstance(value.get('full_name'), str) and value['full_name'].casefold() == expected.casefold()


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def _latest(document, policy, commit, workflow_id):
    if not isinstance(document, dict):
        raise CIGateError('No bounded, complete GitHub push CI result exists for this exact commit.')
    runs = document.get('workflow_runs')
    total = document.get('total_count')
    if not isinstance(runs, list) or not isinstance(total, int) or isinstance(total, bool) or total != len(runs) or not 1 <= total <= 100:
        raise CIGateError('No bounded, complete GitHub push CI result exists for this exact commit.')
    for run in runs:
        if (not isinstance(run, dict) or run.get('head_sha') != commit or run.get('event') != 'push'
                or not _positive(run.get('workflow_id')) or run.get('workflow_id') != workflow_id or run.get('path') != policy.workflow
                or not _repo(run.get('repository'), policy.repository) or not _repo(run.get('head_repository'), policy.repository)
                or not all(_positive(run.get(key)) for key in ('id', 'run_number', 'run_attempt'))):
            raise CIGateError('GitHub CI result does not match the trusted workflow, repository, and exact source commit.')
    try:
        # updated_at includes reruns of older runs. A newer pending/failed attempt
        # must supersede an old success instead of filtering only for success.
        latest = max(runs, key=lambda run: (_stamp(run.get('updated_at')), run['run_number'], run['id']))
    except (ValueError, TypeError):
        raise CIGateError('GitHub CI returned invalid workflow run metadata.') from None
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise CIGateError('The latest GitHub push CI run for this exact commit has not passed.')
    return latest


def _repository_workflow_runs(document, policy, commit, workflow_id):
    """Validate a complete repository index before narrowing to the trusted workflow."""
    if not isinstance(document, dict):
        raise CIGateError('No bounded, complete GitHub push CI result exists for this exact commit.')
    runs, total = document.get('workflow_runs'), document.get('total_count')
    if (not isinstance(runs, list) or not isinstance(total, int) or isinstance(total, bool)
            or total != len(runs) or not 1 <= total <= 100):
        raise CIGateError('No bounded, complete GitHub push CI result exists for this exact commit.')
    matching, seen = [], set()
    for run in runs:
        if (not isinstance(run, dict) or run.get('head_sha') != commit or run.get('event') != 'push'
                or not _repo(run.get('repository'), policy.repository) or not _repo(run.get('head_repository'), policy.repository)
                or not all(_positive(run.get(key)) for key in ('id', 'workflow_id', 'run_number', 'run_attempt'))
                or not isinstance(run.get('path'), str) or not 1 <= len(run['path']) <= 4096
                or run['id'] in seen):
            raise CIGateError('GitHub CI repository results have invalid or ambiguous exact-commit provenance.')
        try:
            _stamp(run.get('updated_at'))
        except (ValueError, TypeError):
            raise CIGateError('GitHub CI returned invalid workflow run metadata.') from None
        seen.add(run['id'])
        trusted_id, trusted_path = run['workflow_id'] == workflow_id, run['path'] == policy.workflow
        if trusted_id != trusted_path:
            raise CIGateError('GitHub CI result does not match the trusted workflow identity and path.')
        if trusted_id:
            matching.append(run)
    return {'total_count': len(matching), 'workflow_runs': matching}


def _empty(document):
    return (isinstance(document, dict) and isinstance(document.get('total_count'), int)
            and not isinstance(document['total_count'], bool) and document['total_count'] == 0
            and isinstance(document.get('workflow_runs'), list) and not document['workflow_runs'])


def _lookup_latest(get, listing, repository_listing, policy, commit, workflow_id, *, repository_mode=False, fresh=False):
    if not repository_mode:
        document = get(listing, fresh=fresh)
        # GitHub can expose an empty workflow index while its repository index
        # already contains the exact push run. Only an honest empty page enables
        # this alternate index; malformed, truncated and failed reads stay denied.
        repository_mode = _empty(document)
        if not repository_mode:
            return _latest(document, policy, commit, workflow_id), False
    for attempt in range(EMPTY_RETRIES + 1):
        document = get(repository_listing, fresh=fresh or attempt > 0)
        if not _empty(document):
            break
    if _empty(document):
        raise _NoPushEvidence('No bounded, complete GitHub push CI result exists for this exact commit.')
    document = _repository_workflow_runs(document, policy, commit, workflow_id)
    return _latest(document, policy, commit, workflow_id), True


def _dispatch_repository(document, approved):
    if (not _repo(document, approved.repository) or not _positive(document.get('id')) or document.get('id') != approved.repository_id
            or document.get('default_branch') != approved.default_branch
            or not isinstance(document.get('owner'), dict)
            or not _positive(document['owner'].get('id')) or document['owner'].get('id') != approved.owner_id or document['owner'].get('type') != 'User'):
        raise CIGateError('Owner-dispatched CI requires the pinned repository, owner and default branch.')


def _dispatch_head(document, approved, commit):
    if (not isinstance(document, dict) or document.get('ref') != 'refs/heads/' + approved.default_branch
            or not isinstance(document.get('object'), dict) or document['object'].get('type') != 'commit'
            or document['object'].get('sha') != commit):
        raise CIGateError('Owner-dispatched CI must test the current exact default-branch commit.')


def _dispatch_latest(document, policy, approved, commit):
    if not isinstance(document, dict):
        raise CIGateError('Owner-dispatched CI requires a complete exact-commit workflow index.')
    runs, total = document.get('workflow_runs'), document.get('total_count')
    if (not isinstance(runs, list) or not isinstance(total, int) or isinstance(total, bool)
            or total != len(runs) or not 1 <= total <= 100):
        raise CIGateError('Owner-dispatched CI requires a complete exact-commit workflow index.')
    seen = set()
    for row in runs:
        if (not isinstance(row, dict) or row.get('head_sha') != commit or row.get('event') != 'workflow_dispatch'
                or not _positive(row.get('workflow_id')) or row.get('workflow_id') != approved.workflow_id or row.get('path') != policy.workflow
                or not _repo(row.get('repository'), approved.repository) or not _repo(row.get('head_repository'), approved.repository)
                or not _positive(row['repository'].get('id')) or not _positive(row['head_repository'].get('id'))
                or row['repository'].get('id') != approved.repository_id or row['head_repository'].get('id') != approved.repository_id
                or not all(_positive(row.get(key)) for key in ('id', 'run_number', 'run_attempt', 'check_suite_id'))
                or row['id'] in seen):
            raise CIGateError('Owner-dispatched CI has foreign or ambiguous run provenance.')
        seen.add(row['id'])
    try:
        latest = max(runs, key=lambda row: (_stamp(row.get('updated_at')), row['run_number'], row['id']))
        created, updated, after = _stamp(latest.get('created_at')), _stamp(latest.get('updated_at')), _stamp(approved.approved_after)
        if created <= after or updated < created:
            raise ValueError
    except (ValueError, TypeError):
        raise CIGateError('Dispatch CI must be newly created after the recorded owner policy approval.') from None
    if (latest.get('head_branch') != approved.default_branch
            or any(not isinstance(latest.get(key), dict) or not _positive(latest[key].get('id')) or latest[key].get('id') != approved.owner_id
                   or latest[key].get('type') != 'User' for key in ('actor', 'triggering_actor'))):
        raise CIGateError('Dispatch CI must be created and triggered by the pinned owner on the default branch.')
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise CIGateError('The latest owner-dispatched attempt has not passed; older success is insufficient.')
    return latest


def _verify_owner_dispatch(get, policy, commit, workflow_id, repository_push_listing):
    approved = OWNER_DISPATCH_PROPOSAL
    if (approved.approved_after is None or policy.repository != approved.repository
            or workflow_id != approved.workflow_id):
        raise CIGateError('No approved owner-dispatch CI policy exists for this repository and workflow.')
    try:
        _stamp(approved.approved_after)
    except (ValueError, TypeError):
        raise CIGateError('The owner-dispatch policy approval timestamp is invalid.') from None
    prefix = '/repos/' + approved.repository
    _dispatch_repository(get(prefix), approved)
    reference = prefix + '/git/ref/heads/' + quote(approved.default_branch, safe='')
    _dispatch_head(get(reference), approved, commit)
    blob = get(prefix + '/contents/' + quote(policy.workflow, safe='/') + '?ref=' + commit)
    try:
        if (not isinstance(blob, dict) or blob.get('type') != 'file' or blob.get('path') != policy.workflow
                or blob.get('encoding') != 'base64' or not isinstance(blob.get('content'), str)
                or not _positive(blob.get('size')) or blob['size'] > 16384):
            raise ValueError
        raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
        if len(raw) != blob['size'] or hashlib.sha256(raw).hexdigest() != approved.workflow_sha256:
            raise ValueError
    except (ValueError, TypeError):
        raise CIGateError('The dispatched workflow differs from the exact reviewed workflow bytes.') from None
    query = urlencode({'head_sha': commit, 'event': 'workflow_dispatch', 'per_page': 100, 'page': 1})
    listing = prefix + f'/actions/workflows/{workflow_id}/runs?' + query
    run = _dispatch_latest(get(listing), policy, approved, commit)
    jobs = get(prefix + f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page=1")
    entries = jobs.get('jobs') if isinstance(jobs, dict) else None
    if (not isinstance(entries, list) or type(jobs.get('total_count')) is not int
            or jobs['total_count'] != 1 or len(entries) != 1):
        raise CIGateError('Dispatch CI requires the single reviewed successful checks job.')
    job = entries[0]
    if (not isinstance(job, dict) or not _positive(job.get('id')) or job.get('name') != policy.required_job
            or not _positive(job.get('run_id')) or job.get('run_id') != run['id'] or job.get('head_sha') != commit
            or job.get('status') != 'completed' or job.get('conclusion') != 'success'):
        raise CIGateError('Dispatch CI required checks did not pass for the exact attempt and source.')
    steps = job.get('steps')
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 100
            or any(not isinstance(step, dict) or not _positive(step.get('number')) or not isinstance(step.get('name'), str)
                   or step.get('status') != 'completed' or step.get('conclusion') not in ('success', 'skipped') for step in steps)):
        raise CIGateError('Dispatch CI contains a missing, skipped or unsuccessful required check.')
    names = [step['name'] for step in steps]
    numbers = [step['number'] for step in steps]
    if (numbers != sorted(set(numbers)) or any(names.count(name) != 1 for name in _DISPATCH_STEPS)
            or any(step['conclusion'] != 'success' for step in steps if step['name'] in _DISPATCH_STEPS)):
        raise CIGateError('Dispatch CI did not execute every reviewed checkout and verification step exactly once.')
    expected_url = 'https://api.github.com' + prefix + '/check-runs/' + str(job['id'])
    if job.get('check_run_url') != expected_url:
        raise CIGateError('Dispatch CI has an unexpected required-check identity.')
    check = get(prefix + '/check-runs/' + str(job['id']))
    app = check.get('app') if isinstance(check, dict) else None
    if (not isinstance(check, dict) or not _positive(check.get('id')) or check.get('id') != job['id'] or check.get('head_sha') != commit
            or check.get('name') != policy.required_job or check.get('status') != 'completed' or check.get('conclusion') != 'success'
            or not isinstance(check.get('check_suite'), dict) or not _positive(check['check_suite'].get('id')) or check['check_suite'].get('id') != run['check_suite_id']
            or not isinstance(app, dict) or not _positive(app.get('id')) or app.get('id') != 15368 or app.get('slug') != 'github-actions'
            or not isinstance(app.get('owner'), dict) or not _positive(app['owner'].get('id')) or app['owner'].get('id') != 9919 or app['owner'].get('login') != 'github'):
        raise CIGateError('Dispatch CI required checks were not produced by the pinned GitHub Actions identity.')
    refreshed = _dispatch_latest(get(listing, fresh=True), policy, approved, commit)
    if any(refreshed.get(key) != run.get(key) for key in ('id', 'run_attempt', 'updated_at', 'check_suite_id')):
        raise CIGateError('Dispatch CI changed during verification; verify the newest exact attempt again.')
    _dispatch_head(get(reference, fresh=True), approved, commit)
    _dispatch_repository(get(prefix, fresh=True), approved)
    # A new push run must return to the normal unchanged policy. No failed or
    # malformed push evidence may be bypassed by this alternate event.
    if not _empty(get(repository_push_listing, fresh=True)):
        raise CIGateError('Push evidence changed during dispatch verification; use the normal push policy.')
    return {'repository': policy.repository, 'commit': commit, 'workflow': policy.workflow,
            'run_id': run['id'], 'run_attempt': run['run_attempt'], 'required_job': policy.required_job,
            'event': 'workflow_dispatch', 'policy': 'owner-dispatch-exact-workflow-v1',
            'owner_id': approved.owner_id, 'workflow_sha256': approved.workflow_sha256,
            'policy_approved_after': approved.approved_after}


def verify_repository(repository, commit, *, token=None, read_json=None):
    """Verify known deployment policies; unrelated applications keep their policy.

    Inject read_json(path) only in offline tests. No result is cached across
    approval/start, so a failed rerun cannot reuse a previous success receipt.
    """
    policy = next((item for item in POLICIES if isinstance(repository, str) and item.repository.casefold() == repository.casefold()), None)
    if policy is None:
        return None
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise CIGateError('GitHub CI admission requires the exact 40-character source commit.')
    get = read_json if isinstance(read_json, _AdmissionReader) else _AdmissionReader(token, read_json)
    prefix = '/repos/' + policy.repository
    workflow = get(prefix + '/actions/workflows/' + quote(policy.workflow.rsplit('/', 1)[-1], safe=''))
    workflow_id = workflow.get('id')
    if not _positive(workflow_id) or workflow.get('path') != policy.workflow or workflow.get('state') != 'active':
        raise CIGateError('The required GitHub Actions workflow is missing, disabled, or has changed identity.')
    query = urlencode({'head_sha': commit, 'event': 'push', 'per_page': 100, 'page': 1})
    listing = prefix + f'/actions/workflows/{workflow_id}/runs?' + query
    repository_listing = prefix + '/actions/runs?' + query
    try:
        run, repository_mode = _lookup_latest(get, listing, repository_listing, policy, commit, workflow_id)
    except _NoPushEvidence:
        approved = OWNER_DISPATCH_PROPOSAL
        if approved.approved_after is None or policy.repository != approved.repository:
            raise
        return _verify_owner_dispatch(get, policy, commit, workflow_id, repository_listing)
    jobs = get(prefix + f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page=1")
    entries = jobs.get('jobs')
    total_jobs = jobs.get('total_count')
    if (not isinstance(entries, list) or not isinstance(total_jobs, int) or isinstance(total_jobs, bool)
            or total_jobs != len(entries) or not 1 <= len(entries) <= 100):
        raise CIGateError('The exact GitHub CI attempt has no complete required job result.')
    matches = [job for job in entries if isinstance(job, dict) and job.get('name') == policy.required_job]
    if len(matches) != 1:
        raise CIGateError('The required GitHub CI aggregate job is missing or ambiguous.')
    job = matches[0]
    if (not _positive(job.get('run_id')) or job.get('run_id') != run['id'] or job.get('head_sha') != commit or job.get('status') != 'completed'
            or job.get('conclusion') != 'success'):
        raise CIGateError('The required GitHub CI aggregate job has not passed for this exact commit and run.')
    # Once the alternate index was needed, recheck that full index. Switching
    # back to a lagging workflow index could hide a newly queued trusted run.
    refreshed, _ = _lookup_latest(get, listing, repository_listing, policy, commit, workflow_id,
                                  repository_mode=repository_mode, fresh=True)
    if any(refreshed.get(key) != run.get(key) for key in ('id', 'run_attempt', 'updated_at')):
        raise CIGateError('GitHub CI changed during verification. Verify the latest completed attempt again.')
    return {'repository': policy.repository, 'commit': commit, 'workflow': policy.workflow,
            'run_id': run['id'], 'run_attempt': run['run_attempt'], 'required_job': policy.required_job}


def verify_deployment(repository, commit, runner_repository, runner_commit, *, token=None, read_json=None):
    protected = any(isinstance(repository, str) and item.repository.casefold() == repository.casefold() for item in POLICIES)
    if protected and (not isinstance(runner_repository, str) or runner_repository.casefold() != 'northcountryengineer/nce_core'):
        raise CIGateError('Protected application releases require the CI-verified Core runner repository.')
    receipts = []
    get = read_json if isinstance(read_json, _AdmissionReader) else _AdmissionReader(token, read_json)
    for source in dict.fromkeys(((repository, commit), (runner_repository, runner_commit))):
        receipt = verify_repository(*source, token=token, read_json=get)
        if receipt:
            receipts.append(receipt)
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, choices=[item.repository for item in POLICIES])
    parser.add_argument('--commit', required=True)
    options = parser.parse_args()
    try:
        receipt = verify_repository(options.repository, options.commit)
    except CIGateError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
