"""Artifact admission, cost bounds and crash recovery with disposable files only."""
import base64
import copy
import importlib.util
import json
import io
import shutil
from pathlib import Path
import subprocess
import tempfile
import tarfile
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

FILE = Path(__file__).resolve().parents[2] / 'deployment/lightsail_build_artifacts.py'
SPEC = importlib.util.spec_from_file_location('build_artifacts_test', FILE)
artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact)
APP = 'd5ab80d1-6502-4f7d-a786-028651d6dc43'
TOKEN = '1' * 64
TRACKED = b'approved tracked bytes\n'
TREE = artifact.git_object('tree', b'100644 file.txt\0' + bytes.fromhex(artifact.git_object('blob', TRACKED)))
RAW_COMMIT = ('tree ' + TREE + '\nauthor Synthetic <synthetic@example.invalid> 1 +0000\ncommitter Synthetic <synthetic@example.invalid> 1 +0000\n\nApproved source\n').encode()
COMMIT = artifact.git_object('commit', RAW_COMMIT)
BUCKET = 'synthetic-private-artifacts'
ACCOUNT, REGION = '015454941623', 'us-east-1'
URI = ACCOUNT + '.dkr.ecr.' + REGION + '.amazonaws.com/synthetic-builds'


class AutomatedArtifactTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode='w:gz', format=tarfile.PAX_FORMAT,
                          pax_headers={'NCE.git_commit': base64.b64encode(RAW_COMMIT).decode()}) as archive:
            entry = tarfile.TarInfo('file.txt')
            entry.mode, entry.size = 0o644, len(TRACKED)
            archive.addfile(entry, io.BytesIO(TRACKED))
        self.source, self.frontend = source.getvalue(), b'approved-frontend-zip'
        self.buildspec = 'fixed reviewed buildspec'
        self.contract = {'account_id': ACCOUNT, 'region': REGION, 'project_name': 'fixed-artifact-project',
            'project_role_arn': 'arn:aws:iam::' + ACCOUNT + ':role/fixed-build', 'artifact_bucket': BUCKET,
            'trusted_source_key': 'lightsail-build/trusted/bundle.zip', 'trusted_source_version': 'immutable-s3-version',
            'trusted_bundle_sha256': 'b' * 64, 'project_buildspec_sha256': artifact.hashlib.sha256(self.buildspec.encode()).hexdigest(),
            'connection_arn': 'arn:aws:codeconnections:us-east-1:' + ACCOUNT + ':connection/' + 'a' * 36,
            'monthly_build_minutes': 30, 'max_build_minutes': 30,
            'recipes': {APP: {'repository': 'Approved/Proposal', 'kind': 'proposal',
              'frontend': {'directory': 'apps/web', 'output': 'dist'}, 'portability': [],
              'image_repositories': {service: URI for service in artifact.SERVICES['proposal']}}}}
        self.runtime = {'kind': 'proposal', 'frontend': 'amplify', 'amplify_app_id': 'd12345', 'amplify_branch': 'production',
                        'build_contract_digest': artifact.digest(self.contract)}
        self.binding = {'repository': 'Approved/Proposal', 'platform_id': APP, 'organization_id': APP, 'environment_id': APP,
                        'archetype': 'amplify-fargate', 'identity': {'callback_path': '/callback', 'permissions': []},
                        'origin': 'https://app.example', 'api_url': 'https://api.example', 'runtime_digest': artifact.digest(self.runtime)}
        self.snapshot = {**{key: self.binding[key] for key in ('repository', 'platform_id', 'organization_id', 'environment_id', 'archetype')},
                         'hosting_binding_digest': artifact.digest(self.binding), 'app_identifier': APP, 'commit': COMMIT,
                         'public_config': {'VITE_LABEL': '${literal}$word$$'}}
        (self.home / 'config').mkdir()
        (self.home / 'config/release-bindings.json').write_text(json.dumps({'build': self.contract,
            'bindings': {APP: self.binding}, 'runtimes': {APP: self.runtime}}))
        self.calls = []
        self.payload = self.build = None
        self.mutate_manifest = lambda value: None
        self.mutate_build = lambda value: None
        self.fail_start = self.fail_poll_once = False
        self.architecture = 'amd64'
        self.stager = artifact.Stager(self.home, run=self.run_command, sleeper=lambda _: None, enforce_owner=False)
        self.stager.aws = self.aws
        self.stager.download = self.download

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        if command[:3] == ['aws', 'ecr', 'get-login-password']:
            return subprocess.CompletedProcess(command, 0, b'synthetic-token')
        if command[:3] == ['docker', 'image', 'inspect']:
            return subprocess.CompletedProcess(command, 0, json.dumps([{'Architecture': self.architecture, 'RepoDigests': [command[3]]}]))
        return subprocess.CompletedProcess(command, 0, '')

    def aws(self, service, operation, arguments):
        self.calls.append([service, operation, arguments])
        if operation == 'start-build':
            if self.fail_start:
                raise TimeoutError('uncertain start, never retry')
            self.payload = json.loads(base64.b64decode(arguments['environmentVariablesOverride'][0]['value']))
            self.build = {'projectName': self.contract['project_name'], 'serviceRole': self.contract['project_role_arn'],
                'sourceVersion': self.contract['trusted_source_version'], 'source': {'type': 'S3', 'location': BUCKET + '/' + self.contract['trusted_source_key'],
                  'buildspec': self.buildspec}, 'environment': {'computeType': 'BUILD_GENERAL1_MEDIUM', 'type': 'LINUX_CONTAINER',
                  'privilegedMode': True, 'image': 'aws/codebuild/standard:7.0', 'environmentVariables': arguments['environmentVariablesOverride']},
                'timeoutInMinutes': 30, 'secondarySources': arguments['secondarySourcesOverride'],
                'secondarySourceVersions': arguments['secondarySourcesVersionOverride'],
                'id': self.contract['project_name'] + ':' + 'a' * 36, 'buildStatus': 'SUCCEEDED'}
            self.build['arn'] = 'arn:aws:codebuild:' + REGION + ':' + ACCOUNT + ':build/' + self.build['id']
            self.mutate_build(self.build)
            return {'build': self.build}
        if operation == 'batch-get-builds':
            if self.fail_poll_once:
                self.fail_poll_once = False
                raise TimeoutError('poll interrupted after durable build id')
            return {'builds': [self.build]}
        raise AssertionError('Unexpected cloud API: ' + operation)

    def download(self, key, path, *, limit):
        if key.endswith('manifest.json'):
            manifest = {'version': 1, 'request_digest': self.payload['request_digest'], 'build_id': self.build['id'],
                'build_arn': self.build['arn'], 'source_commit': COMMIT, 'source_sha256': artifact.hashlib.sha256(self.source).hexdigest(),
                'frontend_sha256': artifact.hashlib.sha256(self.frontend).hexdigest(),
                'images': {service: URI + '@sha256:' + 'c' * 64 for service in artifact.SERVICES['proposal']}, 'portability': []}
            self.mutate_manifest(manifest)
            path.write_text(json.dumps(manifest))
        else:
            path.write_bytes(self.source if key.endswith('source.tar.gz') else self.frontend)
        return 'immutable-object-version'

    def starts(self):
        return sum(command[:2] == ['codebuild', 'start-build'] for command in self.calls)

    def test_versioned_download_uses_cli_streaming_required_flags_and_fixed_owner(self):
        path = self.home / 'download.bin'
        def aws(service, operation, arguments):
            self.assertEqual((service, operation), ('s3api', 'head-object'))
            self.assertEqual(arguments, {'Bucket': BUCKET, 'Key': 'lightsail-build/runs/approved/source.tar.gz', 'ExpectedBucketOwner': ACCOUNT})
            return {'ContentLength': 4, 'VersionId': 'exact-version'}
        def run(command, **kwargs):
            self.assertEqual(command, ['aws', 's3api', 'get-object', '--bucket', BUCKET,
                '--key', 'lightsail-build/runs/approved/source.tar.gz', '--expected-bucket-owner', ACCOUNT,
                '--version-id', 'exact-version', str(path)])
            self.assertEqual(kwargs['env'], self.stager.env)
            path.write_bytes(b'data')
        self.stager.aws, self.stager.run = aws, run
        self.assertEqual(artifact.Stager.download(self.stager, 'lightsail-build/runs/approved/source.tar.gz', path, limit=4), 'exact-version')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_exact_source_build_stages_pinned_images_without_database_or_application_process(self):
        result = self.stager.stage(self.snapshot, TOKEN)
        self.assertEqual(set(result['images']), set(artifact.SERVICES['proposal']))
        self.assertEqual(self.payload['commit'], COMMIT)
        self.assertEqual(self.payload['identity_digest'], artifact.digest(self.binding['identity']))
        self.assertEqual(self.payload['public_config']['VITE_LABEL'], '${literal}$word$$')
        self.assertEqual(self.starts(), 1)
        self.assertFalse(any('compose' in command or 'cloudformation' in command or 'ecs' in command for command in self.calls))
        state = json.loads((self.home / 'build-state' / (TOKEN + '.json')).read_text())
        self.assertTrue(state['staged'])
        self.assertEqual((self.home / 'releases' / APP / COMMIT / 'source.tar.gz').read_bytes(), self.source)

    def test_two_phase_reuses_same_approved_artifacts_under_new_token(self):
        first = self.stager.stage(self.snapshot, TOKEN)
        self.assertEqual(self.stager.stage(self.snapshot, '2' * 64), first)
        self.assertEqual(self.starts(), 1)

    def test_artifact_download_failure_resumes_recorded_build_without_new_paid_start(self):
        self.stager.download = Mock(side_effect=OSError('synthetic interrupted download'))
        with self.assertRaises(OSError):
            self.stager.stage(self.snapshot, TOKEN)
        recorded = json.loads((self.home / 'build-state' / (TOKEN + '.json')).read_text())
        self.assertEqual(recorded['build_id'], self.build['id'])
        self.stager.download = self.download
        self.stager.stage(self.snapshot, TOKEN)
        self.assertEqual(self.starts(), 1)
        self.assertTrue(json.loads((self.home / 'build-state' / (TOKEN + '.json')).read_text())['staged'])

    def test_uncertain_start_is_never_automatically_resubmitted(self):
        self.fail_start = True
        with self.assertRaises(TimeoutError): self.stager.stage(self.snapshot, TOKEN)
        with self.assertRaisesRegex(artifact.Refused, 'reconciliation'): self.stager.stage(self.snapshot, TOKEN)
        self.assertEqual(self.starts(), 1)

    def test_known_start_resumes_polling_without_second_paid_build(self):
        self.fail_poll_once = True
        with self.assertRaises(TimeoutError): self.stager.stage(self.snapshot, TOKEN)
        self.stager.stage(self.snapshot, TOKEN)
        self.assertEqual(self.starts(), 1)

    def test_monthly_reservations_refuse_extra_build_before_start(self):
        self.stager.stage(self.snapshot, TOKEN)
        changed = {**self.snapshot, 'commit': 'd' * 40}
        with self.assertRaisesRegex(artifact.Refused, 'reserve is exhausted'): self.stager.stage(changed, '2' * 64)
        self.assertEqual(self.starts(), 1)

    def test_changed_review_public_config_does_not_reuse_artifacts(self):
        self.stager.stage(self.snapshot, TOKEN)
        with self.assertRaisesRegex(artifact.Refused, 'reserve is exhausted'):
            self.stager.stage({**self.snapshot, 'public_config': {'VITE_LABEL': 'changed'}}, '2' * 64)

    def test_newly_reviewed_public_registration_settings_rebuild_same_commit_when_budget_permits(self):
        self.contract['monthly_build_minutes'] = 60
        self.runtime['build_contract_digest'] = artifact.digest(self.contract)
        self.binding['runtime_digest'] = artifact.digest(self.runtime)
        self.snapshot['hosting_binding_digest'] = artifact.digest(self.binding)
        (self.home / 'config/release-bindings.json').write_text(json.dumps({'build': self.contract,
            'bindings': {APP: self.binding}, 'runtimes': {APP: self.runtime}}))
        self.stager = artifact.Stager(self.home, run=self.run_command, sleeper=lambda _: None, enforce_owner=False)
        self.stager.aws, self.stager.download = self.aws, self.download
        self.stager.stage(self.snapshot, TOKEN)
        self.stager.stage({**self.snapshot, 'public_config': {'VITE_CORE_BROWSER_CLIENT_ID': 'new-public-client'}}, '2' * 64)
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.payload['public_config']['VITE_CORE_BROWSER_CLIENT_ID'], 'new-public-client')

    def test_build_attestation_tampering_refused_before_artifact_or_docker_use(self):
        mutations = [lambda b: b.update(projectName='other-project'), lambda b: b.update(serviceRole='other-role'),
            lambda b: b.update(sourceVersion='other-s3-version'), lambda b: b['source'].update(buildspec='arbitrary source command'),
            lambda b: b['environment'].update(privilegedMode=False), lambda b: b.update(vpcConfig={'vpcId': 'expensive-nat'}),
            lambda b: b['secondarySourceVersions'][0].update(sourceVersion='e' * 40), lambda b: b.update(arn='other-build-arn')]
        # Independent fixtures: each start reserves its month budget before validation.
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.setUp()
                self.mutate_build = mutation
                with self.assertRaises(artifact.Refused): self.stager.stage(self.snapshot, TOKEN)
                self.assertFalse(any(command[0] == 'docker' for command in self.calls))

    def test_manifest_wrong_build_or_commit_or_image_refused_before_pull(self):
        for key, value in [('build_arn', 'another-build'), ('source_commit', 'f' * 40), ('request_digest', 'f' * 64),
                           ('images', {service: 'unapproved.example/repo@sha256:' + 'a' * 64 for service in artifact.SERVICES['proposal']})]:
            with self.subTest(key=key):
                self.setUp()
                self.mutate_manifest = lambda manifest, k=key, v=value: manifest.update({k: v})
                with self.assertRaises(artifact.Refused): self.stager.stage(self.snapshot, TOKEN)
                self.assertFalse(any(command[0] == 'docker' for command in self.calls))

    def test_artifact_checksum_mismatch_and_wrong_cpu_refuse_staging(self):
        self.mutate_manifest = lambda manifest: manifest.update(frontend_sha256='f' * 64)
        with self.assertRaisesRegex(artifact.Refused, 'checksum'): self.stager.stage(self.snapshot, TOKEN)
        self.setUp()
        self.architecture = 'arm64'
        with self.assertRaisesRegex(artifact.Refused, 'amd64'): self.stager.stage(self.snapshot, TOKEN)
        self.assertFalse((self.home / 'releases' / APP / COMMIT / 'automatic-build.json').exists())

    def test_nonpublic_environment_and_invalid_destination_contract_refused(self):
        with self.assertRaisesRegex(artifact.Refused, 'public'):
            artifact.public_values({'public_config': {'VITE_PRIVATE_KEY': 'must-never-build'}}, self.binding)
        bad = copy.deepcopy(self.contract)
        bad['monthly_build_minutes'] = 301
        with self.assertRaises(artifact.Refused): artifact.validate_config(bad)

    @skipUnless(shutil.which('git'), 'producer fixture requires Git, which the web runtime intentionally omits')
    def test_isolated_producer_uses_exact_checkout_fixed_amd64_dockerfiles_and_public_artifact_keys(self):
        source = self.home / 'checkout'
        trusted = self.home / 'trusted'
        trusted.mkdir()
        for relative in ['apps/api', 'apps/browser', 'apps/web/dist']:
            (source / relative).mkdir(parents=True, exist_ok=True)
        for relative in ['apps/api/Dockerfile', 'apps/browser/Dockerfile']:
            (source / relative).write_text('FROM synthetic-test-only')
        (source / 'apps/web/dist/index.html').write_text('<html>synthetic</html>')
        (source / 'nce-deploy.json').write_text(json.dumps({'schema': 1, 'archetype': self.binding['archetype'],
            'identity': self.binding['identity'], 'frontend': {'directory': 'apps/web', 'output': 'dist'},
            'backend': {'image': 'previous-arm-image-is-not-used'}}))
        (source / '.gitattributes').write_text('ignored.txt export-ignore\n')
        (source / 'ignored.txt').write_text('Tracked bytes must remain in provenance despite archive export-ignore')
        (source / 'apps/web/example-link').symlink_to('dist/index.html')
        subprocess.run(['git', 'init', '--quiet'], cwd=source, check=True)
        subprocess.run(['git', 'add', '.'], cwd=source, check=True)
        subprocess.run(['git', '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '--quiet', '-m', 'fixture'], cwd=source, check=True)
        actual_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        request = {'version': 1, 'app_identifier': APP, 'repository': self.binding['repository'], 'commit': actual_commit,
                   'binding_digest': self.snapshot['hosting_binding_digest'], 'public_config': artifact.public_values(self.snapshot, self.binding),
                   'identity_digest': artifact.digest(self.binding['identity']), 'build_contract': self.contract, 'run_token': TOKEN}
        request['request_digest'] = artifact.digest(request)
        environment = {'NCE_BUILD_REQUEST': base64.b64encode(json.dumps(request).encode()).decode(),
            'CODEBUILD_SRC_DIR_APPLICATION': str(source), 'CODEBUILD_SRC_DIR': str(trusted),
            'CODEBUILD_SOURCE_VERSION': self.contract['trusted_source_version'],
            'CODEBUILD_SOURCE_REPO_URL_APPLICATION': 'https://github.com/' + self.binding['repository'] + '.git',
            'CODEBUILD_SOURCE_VERSION_APPLICATION': actual_commit, 'CODEBUILD_BUILD_ID': 'fixed:synthetic', 'CODEBUILD_BUILD_ARN': 'arn:synthetic'}
        commands = []
        original_run = subprocess.run

        def run(command, **kwargs):
            if command[0] == 'git':
                return original_run(command, **kwargs)
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        original_output = subprocess.check_output
        def output(command, **kwargs):
            if command[0] == 'git':
                return original_output(command, **kwargs)
            if command[:3] == ['aws', 'ecr', 'get-login-password']:
                return b'synthetic-ephemeral-token'
            if command[:3] == ['aws', 'ecr', 'describe-images']:
                return json.dumps({'imageDetails': [{'imageDigest': 'sha256:' + 'c' * 64}]}).encode()
            self.fail('Unexpected producer command')

        with patch.dict(artifact.os.environ, environment, clear=True), patch.object(artifact.subprocess, 'run', side_effect=run), \
                patch.object(artifact.subprocess, 'check_output', side_effect=output):
            artifact.codebuild_main()
        result = json.loads((trusted / 'manifest.json').read_text())
        self.assertEqual(result['source_commit'], actual_commit)
        self.assertEqual(result['source_sha256'], artifact.sha(trusted / 'source.tar.gz'))
        self.assertTrue(artifact.verify_source_archive(trusted / 'source.tar.gz', actual_commit))
        with tarfile.open(trusted / 'source.tar.gz') as packed:
            self.assertIn('ignored.txt', packed.getnames())
            self.assertTrue(packed.getmember('apps/web/example-link').issym())
        self.assertEqual(result['images']['proposal-api'], result['images']['proposal-worker'])
        builds = [command for command in commands if command[:2] == ['docker', 'build']]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(command[command.index('--platform') + 1] == 'linux/amd64' and command[-1] == '.' for command in builds))
        self.assertEqual({Path(command[command.index('--file') + 1]).relative_to(source).as_posix() for command in builds},
                         {'apps/api/Dockerfile', 'apps/browser/Dockerfile'})
        uploads = [command for command in commands if command[:3] == ['aws', 's3', 'cp']]
        self.assertEqual({command[4] for command in uploads}, {'s3://' + BUCKET + '/lightsail-build/runs/' + TOKEN + '/' + name
                         for name in ['source.tar.gz', 'frontend.zip', 'manifest.json']})

    def test_status_publisher_reads_only_fixed_eight_service_health_with_utc_freshness(self):
        spec = importlib.util.spec_from_file_location('status_publisher_test', FILE.with_name('lightsail_runtime_status.py'))
        publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publisher)
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, ('a' * 64 + '\n') if command[0:2] == ['docker', 'compose'] else '"running" "healthy"\n')
        result = publisher.collect(run)
        self.assertEqual(set(result['services']), set(publisher.SERVICES))
        self.assertEqual(len(result['services']), 8)
        self.assertTrue(all(value == {'state': 'running', 'health': 'healthy'} for value in result['services'].values()))
        self.assertTrue(result['checked_at'].endswith('+00:00'))
        self.assertTrue(all('exec' not in command and 'logs' not in command for command in commands))
        self.assertTrue(all(command[-1] in publisher.SERVICES for command in commands if command[:2] == ['docker', 'compose']))

    def test_app_cannot_substitute_source_even_with_a_matching_claimed_sha256(self):
        altered = io.BytesIO()
        with tarfile.open(fileobj=altered, mode='w:gz', format=tarfile.PAX_FORMAT,
                          pax_headers={'NCE.git_commit': base64.b64encode(RAW_COMMIT).decode()}) as archive:
            data = b'changed by an application build after checkout'
            entry = tarfile.TarInfo('file.txt')
            entry.mode, entry.size = 0o644, len(data)
            archive.addfile(entry, io.BytesIO(data))
        self.source = altered.getvalue()
        # The mock build manifest now honestly hashes the forged artifact. The
        # independent commit/tree check still refuses it before image pulls.
        with self.assertRaisesRegex(artifact.Refused, 'approved Git tree'):
            self.stager.stage(self.snapshot, TOKEN)
        self.assertFalse(any(command[0] == 'docker' for command in self.calls))

    def test_archive_commit_mode_and_untracked_files_cannot_forge_approved_source(self):
        for raw, mode, extra in [(RAW_COMMIT + b'changed', 0o644, False), (RAW_COMMIT, 0o755, False), (RAW_COMMIT, 0o644, True)]:
            with self.subTest(mode=mode, extra=extra):
                output = self.home / 'forged.tar.gz'
                with tarfile.open(output, 'w:gz', format=tarfile.PAX_FORMAT,
                                  pax_headers={'NCE.git_commit': base64.b64encode(raw).decode()}) as archive:
                    for name in ['file.txt', *(['untracked.txt'] if extra else [])]:
                        entry = tarfile.TarInfo(name)
                        entry.mode, entry.size = mode, len(TRACKED)
                        archive.addfile(entry, io.BytesIO(TRACKED))
                with self.assertRaises(artifact.Refused): artifact.verify_source_archive(output, COMMIT)
