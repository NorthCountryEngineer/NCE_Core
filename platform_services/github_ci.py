"""Read-only, exact-commit GitHub Actions admission for trusted release sources.

Policies belong to Core source, never application manifests or browser input.
Only a server-owned encrypted credential or operator token file authenticates private reads.
"""
from __future__ import annotations

import argparse
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
    document = _repository_workflow_runs(document, policy, commit, workflow_id)
    return _latest(document, policy, commit, workflow_id), True


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
    run, repository_mode = _lookup_latest(get, listing, repository_listing, policy, commit, workflow_id)
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
