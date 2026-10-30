#!/usr/bin/env python3
"""Fixed artifact-only CodeBuild producer and root-only verified staging client.

The producer runs application build code only in isolated on-demand CodeBuild.
The host never executes source or Dockerfiles; it pulls verified amd64 digests.
No infrastructure provisioning API is used by either mode.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from zipfile import ZipFile, ZIP_DEFLATED

HOME = Path('/srv/nce')
HEX = re.compile(r'[a-f0-9]{64}\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
SERVICES = {'proposal': ('proposal-api', 'proposal-worker', 'sam-browser'), 'academy': ('academy-api',)}
CONFIG_FIELDS = {'account_id', 'region', 'project_name', 'project_role_arn', 'artifact_bucket',
                 'trusted_source_key', 'trusted_source_version', 'trusted_bundle_sha256', 'connection_arn',
                 'monthly_build_minutes', 'max_build_minutes', 'recipes', 'project_buildspec_sha256'}


class Refused(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def git_object(kind, data):
    return hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def verify_source_archive(filename, commit):
    """Verify every tracked Git byte against the approved commit; never extract.

    A source build can write arbitrary artifacts, so its claimed archive hash
    alone does not prove source provenance. Git's commit/tree/blob object hashes
    bind the archive to the independently approved exact GitHub commit.
    """
    tree = {}
    with tarfile.open(filename, 'r:gz') as archive:
        raw = base64.b64decode(archive.pax_headers.get('NCE.git_commit', ''), validate=True)
        if not 0 < len(raw) <= 65536 or git_object('commit', raw) != commit:
            raise Refused('Source archive commit object differs from the approved commit')
        first = raw.split(b'\n', 1)[0]
        if not re.fullmatch(b'tree [a-f0-9]{40}', first):
            raise Refused('Source commit has no valid tracked tree')
        expected_tree = first[5:].decode()
        size, count = 0, 0
        for entry in archive:
            path = PurePosixPath(entry.name)
            count += 1
            if (path.is_absolute() or not path.parts or str(path) != entry.name or '..' in path.parts
                    or '.git' in path.parts or count > 20000 or not (entry.isfile() or entry.issym())):
                raise Refused('Tracked source archive has an invalid file layout')
            size += entry.size
            if size > 200 * 1024 * 1024:
                raise Refused('Tracked source archive exceeds its expanded bound')
            if entry.issym():
                data, mode = entry.linkname.encode('utf-8'), '120000'
            else:
                if entry.mode not in {0o644, 0o755}:
                    raise Refused('Tracked source file mode differs')
                data, mode = archive.extractfile(entry).read(), '100755' if entry.mode == 0o755 else '100644'
            parent = tree
            for part in path.parts[:-1]:
                parent = parent.setdefault(part, {})
                if not isinstance(parent, dict):
                    raise Refused('Tracked source directory conflicts with a file')
            if path.name in parent:
                raise Refused('Tracked source archive contains duplicate paths')
            parent[path.name] = (mode, git_object('blob', data))

    def tree_hash(entries):
        packed = b''
        for name, item in sorted(entries.items(), key=lambda pair: (pair[0] + ('/' if isinstance(pair[1], dict) else '')).encode('utf-8')):
            mode, value = ('40000', tree_hash(item)) if isinstance(item, dict) else item
            packed += mode.encode() + b' ' + name.encode('utf-8') + b'\0' + bytes.fromhex(value)
        return git_object('tree', packed)

    if tree_hash(tree) != expected_tree:
        raise Refused('Tracked source bytes differ from the approved Git tree')
    return expected_tree


def produce_source_archive(root, destination):
    raw_commit = subprocess.check_output(['git', 'cat-file', 'commit', 'HEAD'], cwd=root)
    listed = subprocess.check_output(['git', 'ls-tree', '-r', '-z', 'HEAD'], cwd=root)
    with tarfile.open(destination, 'w:gz', format=tarfile.PAX_FORMAT,
                      pax_headers={'NCE.git_commit': base64.b64encode(raw_commit).decode()}) as archive:
        for record in listed.split(b'\0'):
            if not record:
                continue
            metadata, name = record.split(b'\t', 1)
            mode, kind, object_id = metadata.split(b' ')
            if mode not in {b'100644', b'100755', b'120000'} or kind != b'blob' or not COMMIT.fullmatch(object_id.decode()):
                raise Refused('Only tracked files/symlinks, without submodules, are supported')
            data = subprocess.check_output(['git', 'cat-file', 'blob', object_id.decode()], cwd=root)
            entry = tarfile.TarInfo(name.decode('utf-8'))
            if mode == b'120000':
                entry.type, entry.linkname, entry.mode = tarfile.SYMTYPE, data.decode('utf-8'), 0o777
                archive.addfile(entry)
            else:
                entry.mode, entry.size = (0o755 if mode == b'100755' else 0o644), len(data)
                archive.addfile(entry, io.BytesIO(data))


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as target:
        target.write(json.dumps(value, sort_keys=True))
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)


def validate_config(config):
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise Refused('Invalid fixed build contract')
    account, region = config['account_id'], config['region']
    checks = ((account, r'[0-9]{12}'), (region, r'[a-z]{2}(?:-[a-z]+)+-[0-9]'),
              (config['project_name'], r'[A-Za-z0-9_-]{2,255}'),
              (config['project_role_arn'], rf'arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+'),
              (config['artifact_bucket'], r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]'),
              (config['connection_arn'], rf'arn:aws:(?:codeconnections|codestar-connections):{region}:{account}:connection/[a-f0-9-]+'),
              (config['trusted_source_key'], r'lightsail-build/trusted/[a-zA-Z0-9._/-]+\.zip'),
              (config['trusted_source_version'], r'[A-Za-z0-9_.+/=-]{1,1024}'),
              (config['trusted_bundle_sha256'], r'[a-f0-9]{64}'),
              (config['project_buildspec_sha256'], r'[a-f0-9]{64}'))
    if any(not isinstance(value, str) or not re.fullmatch(pattern, value) for value, pattern in checks):
        raise Refused('Invalid fixed build destination')
    if (type(config['max_build_minutes']) is not int or not 5 <= config['max_build_minutes'] <= 30
            or type(config['monthly_build_minutes']) is not int or not 30 <= config['monthly_build_minutes'] <= 300
            or not isinstance(config['recipes'], dict) or not 1 <= len(config['recipes']) <= 2):
        raise Refused('Build contract exceeds the fixed cost bounds')
    for app, recipe in config['recipes'].items():
        if (not re.fullmatch(r'[a-f0-9-]{36}', app) or not isinstance(recipe, dict)
                or set(recipe) != {'repository', 'kind', 'frontend', 'image_repositories', 'portability'}
                or recipe['kind'] not in SERVICES or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', recipe['repository'])
                or recipe['frontend'] != {'directory': 'apps/web', 'output': 'dist'}
                or set(recipe['image_repositories']) != set(SERVICES[recipe['kind']])
                or any(not re.fullmatch(rf'{account}\.dkr\.ecr\.{region}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]{{0,255}}', uri)
                       for uri in recipe['image_repositories'].values())
                or recipe['kind'] == 'proposal' and recipe['image_repositories']['proposal-api'] != recipe['image_repositories']['proposal-worker']
                or not isinstance(recipe['portability'], list) or len(recipe['portability']) > 3):
            raise Refused('Invalid fixed application build recipe')
        for patch in recipe['portability']:
            if (not isinstance(patch, dict) or set(patch) != {'path', 'before_sha256', 'after_sha256', 'bundle_path'}
                    or not re.fullmatch(r'apps/api/academy/[a-z_]+\.py', patch['path'])
                    or not re.fullmatch(r'patches/[a-z_]+\.py', patch['bundle_path'])
                    or any(not HEX.fullmatch(patch[key]) for key in ('before_sha256', 'after_sha256'))):
                raise Refused('Invalid reviewed portability overlay')
    return config


def public_values(snapshot, binding):
    values = dict(snapshot['public_config'])
    values['VITE_APPLICATION_ORIGIN'] = binding['origin']
    values['VITE_API_URL'] = binding['api_url']
    if any(not re.fullmatch(r'VITE_[A-Z0-9_]{1,100}', name) or re.search(r'SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY', name)
           or not isinstance(value, str) or len(value) > 4096 or '\x00' in value for name, value in values.items()):
        raise Refused('Only bounded public frontend values reach the builder')
    return values


class Stager:
    def __init__(self, home=HOME, *, run=subprocess.run, sleeper=time.sleep, enforce_owner=True):
        self.home, self.run, self.sleeper = Path(home), run, sleeper
        filename = self.home / 'config/release-bindings.json'
        if enforce_owner and (filename.stat().st_uid != 0 or filename.stat().st_mode & 0o077):
            raise Refused('Build configuration must be root-only')
        self.config = json.loads(filename.read_text())
        self.build = validate_config(self.config['build'])
        credentials = Path('/etc/nce-release/build-credentials')
        if enforce_owner and (credentials.stat().st_uid != 0 or credentials.stat().st_mode & 0o077):
            raise Refused('Build credentials must be root-only')
        self.env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'AWS_SHARED_CREDENTIALS_FILE': str(credentials),
                    'AWS_CONFIG_FILE': '/dev/null', 'AWS_PROFILE': 'nce-build', 'AWS_REGION': self.build['region'],
                    'AWS_DEFAULT_REGION': self.build['region'], 'AWS_EC2_METADATA_DISABLED': 'true',
                    'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': ''}

    def aws(self, service, operation, arguments):
        result = self.run(['aws', service, operation, '--cli-input-json', json.dumps(arguments),
                           '--output', 'json', '--no-cli-pager'], check=True, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=self.env, timeout=90)
        if len(result.stdout.encode()) > 1048576:
            raise Refused('Build API response exceeds its limit')
        return json.loads(result.stdout) if result.stdout else {}

    def download(self, key, path, *, limit):
        arguments = {'Bucket': self.build['artifact_bucket'], 'Key': key, 'ExpectedBucketOwner': self.build['account_id']}
        head = self.aws('s3api', 'head-object', arguments)
        if not 0 < head['ContentLength'] <= limit or not head.get('VersionId') or head['VersionId'] == 'null':
            raise Refused('Build artifacts must be bounded and versioned')
        # The CLI's streaming GetObject customization validates --bucket/--key
        # before merging --cli-input-json; supply these fixed fields explicitly.
        self.run(['aws', 's3api', 'get-object', '--bucket', arguments['Bucket'], '--key', key,
                  '--expected-bucket-owner', self.build['account_id'], '--version-id', head['VersionId'], str(path)],
                 check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 env=self.env, timeout=180)
        path.chmod(0o600)
        return head['VersionId']

    @contextmanager
    def build_lock(self):
        root = self.home / 'build-state'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / 'build.lock').open('a') as lock:
            # Wait outside database maintenance. No second build can reserve or
            # start concurrently, including another application's request.
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield root

    def validate_build(self, build, payload):
        expected_source = {'type': 'S3', 'location': self.build['artifact_bucket'] + '/' + self.build['trusted_source_key']}
        sources = build.get('secondarySources', [])
        versions = build.get('secondarySourceVersions', [])
        env = {x['name']: x['value'] for x in build.get('environment', {}).get('environmentVariables', [])}
        if (build.get('projectName') != self.build['project_name'] or build.get('serviceRole') != self.build['project_role_arn']
                or build.get('sourceVersion') != self.build['trusted_source_version']
                or any(build.get('source', {}).get(k) != v for k, v in expected_source.items())
                or hashlib.sha256(build.get('source', {}).get('buildspec', '').encode()).hexdigest() != self.build['project_buildspec_sha256']
                or build.get('environment', {}).get('computeType') != 'BUILD_GENERAL1_MEDIUM'
                or build.get('environment', {}).get('type') != 'LINUX_CONTAINER'
                or build.get('environment', {}).get('image') != 'aws/codebuild/standard:7.0'
                or build.get('environment', {}).get('privilegedMode') is not True
                or build.get('environment', {}).get('fleet')
                or build.get('vpcConfig') or build.get('timeoutInMinutes') != self.build['max_build_minutes']
                or len(sources) != 1 or sources[0].get('sourceIdentifier') != 'APPLICATION'
                or sources[0].get('location') != 'https://github.com/' + payload['repository'] + '.git'
                or sources[0].get('auth') != {'type': 'CODECONNECTIONS', 'resource': self.build['connection_arn']}
                or versions != [{'sourceIdentifier': 'APPLICATION', 'sourceVersion': payload['commit']}]
                or env.get('NCE_BUILD_REQUEST') != base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
                or not re.fullmatch(re.escape(self.build['project_name']) + r':[a-f0-9-]{36}', build.get('id', ''))
                or build.get('arn') != f"arn:aws:codebuild:{self.build['region']}:{self.build['account_id']}:build/{build.get('id')}"):
            raise Refused('CodeBuild attestation differs from the reviewed source contract')

    def stage(self, snapshot, token):
        if not HEX.fullmatch(token) or not COMMIT.fullmatch(snapshot['commit']):
            raise Refused('Invalid fixed staging identity')
        app = snapshot['app_identifier']
        binding, runtime = self.config['bindings'][app], self.config['runtimes'][app]
        recipe = self.build['recipes'][app]
        if (digest(binding) != snapshot['hosting_binding_digest'] or digest(runtime) != binding['runtime_digest']
                or runtime.get('build_contract_digest') != digest(self.build)
                or any(snapshot[key] != binding[key] for key in ('repository', 'platform_id', 'organization_id', 'environment_id', 'archetype'))
                or recipe['repository'] != snapshot['repository'] or recipe['kind'] != runtime['kind']):
            raise Refused('Automated build differs from the reviewed hosting contract')
        payload = {'version': 1, 'app_identifier': app, 'repository': snapshot['repository'], 'commit': snapshot['commit'],
                   'binding_digest': snapshot['hosting_binding_digest'], 'public_config': public_values(snapshot, binding),
                   'identity_digest': digest(binding['identity']), 'build_contract': self.build, 'run_token': token}
        payload['request_digest'] = digest(payload)
        root = self.home / 'releases' / app / snapshot['commit']
        with self.build_lock() as state_root:
            if root.is_symlink() or any(p.is_symlink() for p in root.parents if p != self.home.parent):
                raise Refused('Staging root contains a symlink')
            marker = root / 'automatic-build.json'
            if marker.is_file():
                previous = json.loads(marker.read_text())
                if previous['request_digest_without_token'] == digest({k: v for k, v in payload.items() if k not in {'run_token', 'request_digest'}}):
                    return previous['release']
                # A newly reviewed plan may rebuild the same commit with new
                # public registration settings. Admit a new bounded build;
                # never silently reuse another plan's frontend artifact.
            statefile = state_root / (token + '.json')
            if statefile.exists():
                state = json.loads(statefile.read_text())
                if state['request_digest'] != payload['request_digest'] or not state.get('build_id'):
                    raise Refused('Unknown prior build start requires operator reconciliation; no automatic resubmission')
            else:
                month = datetime.now(timezone.utc).strftime('%Y-%m')
                reservations = []
                for file in state_root.glob('*.json'):
                    item = json.loads(file.read_text())
                    if item['month'] == month:
                        reservations.append(item['reserved_minutes'])
                if sum(reservations) + self.build['max_build_minutes'] > self.build['monthly_build_minutes']:
                    raise Refused('The approved monthly build-minute reserve is exhausted')
                state = {'request_digest': payload['request_digest'], 'month': month,
                         'reserved_minutes': self.build['max_build_minutes'], 'start_intent': True}
                private_json(statefile, state)
                response = self.aws('codebuild', 'start-build', {
                    'projectName': self.build['project_name'], 'sourceVersion': self.build['trusted_source_version'],
                    'idempotencyToken': token,
                    'secondarySourcesOverride': [{'type': 'GITHUB', 'location': 'https://github.com/' + snapshot['repository'] + '.git',
                        'sourceIdentifier': 'APPLICATION', 'gitCloneDepth': 1, 'gitSubmodulesConfig': {'fetchSubmodules': False},
                        'auth': {'type': 'CODECONNECTIONS', 'resource': self.build['connection_arn']}}],
                    'secondarySourcesVersionOverride': [{'sourceIdentifier': 'APPLICATION', 'sourceVersion': snapshot['commit']}],
                    'environmentVariablesOverride': [{'name': 'NCE_BUILD_REQUEST', 'type': 'PLAINTEXT',
                        'value': base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()}],
                    'timeoutInMinutesOverride': self.build['max_build_minutes'], 'queuedTimeoutInMinutesOverride': 15})
                build = response['build']
                state.update(build_id=build['id'], build_arn=build['arn'])
                private_json(statefile, state)
                self.validate_build(build, payload)
            for _ in range(190):
                response = self.aws('codebuild', 'batch-get-builds', {'ids': [state['build_id']]})
                if len(response.get('builds', [])) != 1 or response.get('buildsNotFound'):
                    raise Refused('Recorded source build is unavailable')
                build = response['builds'][0]
                self.validate_build(build, payload)
                if build['arn'] != state['build_arn']:
                    raise Refused('Source build ARN changed')
                if (self.home / 'release-state' / token / 'cancel').exists():
                    self.aws('codebuild', 'stop-build', {'id': state['build_id']})
                    raise Refused('Source build cancelled')
                if build['buildStatus'] == 'SUCCEEDED':
                    break
                if build['buildStatus'] != 'IN_PROGRESS':
                    raise Refused('The exact source build did not succeed')
                self.sleeper(15)
            else:
                self.aws('codebuild', 'stop-build', {'id': state['build_id']})
                raise Refused('Source build exceeded its bounded wait')
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            prefix = 'lightsail-build/runs/' + token + '/'
            cache_root = self.home / 'build-cache'
            cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='verify-', dir=cache_root) as temporary:
                temporary = Path(temporary)
                self.download(prefix + 'manifest.json', temporary / 'manifest.json', limit=65536)
                manifest = json.loads((temporary / 'manifest.json').read_text())
                expected = {'version', 'request_digest', 'build_id', 'build_arn', 'source_commit', 'source_sha256', 'frontend_sha256', 'images', 'portability'}
                if (not isinstance(manifest, dict) or set(manifest) != expected or manifest['version'] != 1
                        or manifest['request_digest'] != payload['request_digest'] or manifest['build_id'] != state['build_id']
                        or manifest['build_arn'] != state['build_arn'] or manifest['source_commit'] != snapshot['commit']
                        or manifest['portability'] != recipe['portability']
                        or set(manifest['images']) != set(SERVICES[runtime['kind']])
                        or any(not re.fullmatch(re.escape(recipe['image_repositories'][service]) + r'@sha256:[a-f0-9]{64}', image)
                               for service, image in manifest['images'].items())):
                    raise Refused('Artifact manifest differs from its exact approved build')
                versions = {}
                for filename, field, limit in [('source.tar.gz', 'source_sha256', 200 * 1024 * 1024), ('frontend.zip', 'frontend_sha256', 100 * 1024 * 1024)]:
                    versions[filename] = self.download(prefix + filename, temporary / filename, limit=limit)
                    if not HEX.fullmatch(manifest[field]) or sha(temporary / filename) != manifest[field]:
                        raise Refused('Downloaded artifact checksum differs from its build manifest')
                verify_source_archive(temporary / 'source.tar.gz', snapshot['commit'])
                password = self.run(['aws', 'ecr', 'get-login-password'], env=self.env, capture_output=True, check=True,
                                    stdin=subprocess.DEVNULL, timeout=90).stdout
                docker_config = temporary / 'docker'
                docker_env = {'PATH': self.env['PATH'], 'DOCKER_CONFIG': str(docker_config)}
                self.run(['docker', 'login', '--username', 'AWS', '--password-stdin', self.build['account_id'] + '.dkr.ecr.' + self.build['region'] + '.amazonaws.com'],
                         input=password, env=docker_env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
                for image in set(manifest['images'].values()):
                    self.run(['docker', 'pull', '--platform', 'linux/amd64', image], env=docker_env, check=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                    inspected = self.run(['docker', 'image', 'inspect', image], check=True, capture_output=True, text=True,
                                         stdin=subprocess.DEVNULL, timeout=30)
                    info = json.loads(inspected.stdout)
                    if len(info) != 1 or info[0]['Architecture'] != 'amd64' or image not in info[0].get('RepoDigests', []):
                        raise Refused('The runtime image is not its approved amd64 digest')
                for filename in ('source.tar.gz', 'frontend.zip'):
                    os.replace(temporary / filename, root / filename)
                release = {key: manifest[key] for key in ('source_sha256', 'frontend_sha256', 'images')}
                private_json(marker, {'request_digest_without_token': digest({k: v for k, v in payload.items() if k not in {'run_token', 'request_digest'}}),
                                     'build_id': state['build_id'], 'build_arn': state['build_arn'], 'artifact_versions': versions,
                                     'build_contract_digest': digest(self.build), 'binding_digest': snapshot['hosting_binding_digest'],
                                     'public_config_digest': digest(snapshot['public_config']), 'release': release})
            state['staged'] = True
            private_json(statefile, state)
            return release


def codebuild_main():
    """Run only in the isolated privileged artifact project; never on the host."""
    request = json.loads(base64.b64decode(os.environ['NCE_BUILD_REQUEST'], validate=True))
    claimed = request.pop('request_digest')
    if claimed != digest(request):
        raise Refused('Build request digest differs')
    request['request_digest'] = claimed
    config = validate_config(request['build_contract'])
    recipe = config['recipes'][request['app_identifier']]
    root = Path(os.environ['CODEBUILD_SRC_DIR_APPLICATION']).resolve()
    trusted = Path(os.environ['CODEBUILD_SRC_DIR']).resolve()
    if (recipe['repository'] != request['repository'] or not COMMIT.fullmatch(request['commit'])
            or not HEX.fullmatch(request['run_token']) or os.environ['CODEBUILD_SOURCE_VERSION'] != config['trusted_source_version']
            or os.environ['CODEBUILD_SOURCE_REPO_URL_APPLICATION'] != 'https://github.com/' + request['repository'] + '.git'
            or os.environ['CODEBUILD_SOURCE_VERSION_APPLICATION'] != request['commit']):
        raise Refused('Build source differs from the approved versioned request')
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if actual != request['commit']:
        raise Refused('Application checkout differs from the approved commit')
    source = trusted / 'source.tar.gz'
    produce_source_archive(root, source)
    verify_source_archive(source, request['commit'])
    manifest_path = root / 'nce-deploy.json'
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16384:
        raise Refused('Source manifest exceeds its fixed boundary')
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('schema') != 1 or manifest.get('archetype') != ('amplify-fargate' if recipe['kind'] == 'proposal' else 'amplify-academy-fargate')
            or manifest.get('frontend') != recipe['frontend']):
        raise Refused('Source manifest differs from its fixed build recipe')
    # Identity is operator-owned: source must preserve the exact existing one.
    # Its digest is sent independently by the root's reviewed binding.
    if digest(manifest.get('identity')) != request.get('identity_digest'):
        raise Refused('Source identity differs from its existing application')
    for patch in recipe['portability']:
        target = root / patch['path']
        replacement = trusted / patch['bundle_path']
        if target.is_symlink() or replacement.is_symlink() or sha(replacement) != patch['after_sha256']:
            raise Refused('Portability overlay bytes differ from the approved bundle')
        current = sha(target)
        if current == patch['before_sha256']:
            target.write_bytes(replacement.read_bytes())
        elif current != patch['after_sha256']:
            raise Refused('Portability overlay requires source compatibility review')
    public = request['public_config']
    if any(not re.fullmatch(r'VITE_[A-Z0-9_]{1,100}', key) or re.search(r'SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY', key)
           or not isinstance(value, str) or '\x00' in value for key, value in public.items()):
        raise Refused('Nonpublic source build input')
    env = {**os.environ, **public}
    frontend_source = (root / 'apps/web').resolve()
    if not frontend_source.is_relative_to(root) or not frontend_source.is_dir():
        raise Refused('Frontend source must remain inside its exact checkout')
    subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=frontend_source, env=env, check=True)
    subprocess.run(['npm', 'run', 'build'], cwd=frontend_source, env=env, check=True)
    frontend = root / 'apps/web/dist'
    if not frontend.resolve().is_relative_to(root) or not (frontend / 'index.html').is_file():
        raise Refused('Frontend build is incomplete')
    archive = trusted / 'frontend.zip'
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as packed:
        size = 0
        for index, file in enumerate(sorted(frontend.rglob('*'))):
            if file.is_symlink() or index > 20000:
                raise Refused('Frontend build exceeds its fixed layout')
            if file.is_file():
                size += file.stat().st_size
                if size > 100 * 1024 * 1024 or file.name.startswith('.env'):
                    raise Refused('Frontend build exceeds its public artifact boundary')
                packed.write(file, str(file.relative_to(frontend)))
    registry = config['account_id'] + '.dkr.ecr.' + config['region'] + '.amazonaws.com'
    password = subprocess.check_output(['aws', 'ecr', 'get-login-password'])
    subprocess.run(['docker', 'login', '--username', 'AWS', '--password-stdin', registry], input=password, check=True)
    images = {}
    for service in SERVICES[recipe['kind']]:
        if service == 'proposal-worker':
            images[service] = images['proposal-api']
            continue
        dockerfile = 'apps/browser/Dockerfile' if service == 'sam-browser' else 'apps/api/Dockerfile'
        target = root / dockerfile
        if target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
            raise Refused('The fixed backend Dockerfile is unavailable')
        uri = recipe['image_repositories'][service]
        tag = uri + ':lightsail-' + service + '-' + request['commit'][:12] + '-' + request['run_token'][:12]
        subprocess.run(['docker', 'build', '--platform', 'linux/amd64', '--label', 'org.opencontainers.image.revision=' + request['commit'],
                        '--file', str(target), '--tag', tag, '.'], cwd=root, check=True)
        subprocess.run(['docker', 'push', tag], check=True)
        image = json.loads(subprocess.check_output(['aws', 'ecr', 'describe-images', '--repository-name', uri.split('/', 1)[1],
                '--image-ids', 'imageTag=' + tag.rsplit(':', 1)[1], '--output', 'json']))['imageDetails']
        if len(image) != 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', image[0]['imageDigest']):
            raise Refused('ECR did not return one immutable image digest')
        images[service] = uri + '@' + image[0]['imageDigest']
    result = {'version': 1, 'request_digest': request['request_digest'], 'build_id': os.environ['CODEBUILD_BUILD_ID'],
              'build_arn': os.environ['CODEBUILD_BUILD_ARN'], 'source_commit': actual,
              'source_sha256': sha(source), 'frontend_sha256': sha(archive), 'images': images, 'portability': recipe['portability']}
    (trusted / 'manifest.json').write_text(json.dumps(result, sort_keys=True))
    prefix = 's3://' + config['artifact_bucket'] + '/lightsail-build/runs/' + request['run_token'] + '/'
    for filename in ('source.tar.gz', 'frontend.zip', 'manifest.json'):
        subprocess.run(['aws', 's3', 'cp', str(trusted / filename), prefix + filename, '--sse', 'AES256', '--only-show-errors'], check=True)


def main():
    if sys.argv[1:] == ['--codebuild']:
        codebuild_main()
    elif len(sys.argv) == 1 and os.geteuid() == 0:
        request = json.loads(sys.stdin.buffer.read(65537))
        Stager().stage(request['snapshot'], request['token'])
    else:
        raise Refused('Only fixed root staging or isolated CodeBuild execution is supported')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # API diagnostics and app build output can contain private source; the
        # calling control plane receives a sanitized failure only.
        sys.exit(1)
