#!/usr/bin/env python3
"""Root-owned fixed release protocol; SSH callers provide JSON on stdin only.

Install at /usr/local/sbin/nce-lightsail-release-protocol, root:root 0755.
The dedicated SSH identity must have this exact forced sudo command and no
interactive shell, forwarding or arbitrary sudo access. This runner provisions
no infrastructure. Its only AWS operations publish a staged ZIP to an existing
fixed Amplify app/production branch and read that job's status. App metadata,
custom domains, redirect/rewrite rules and branch configuration are untouched.
The fixed artifact helper stages approved source builds before maintenance;
explicit operator-staged entries remain available for recovery.
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
from zipfile import ZipFile

HOME = Path('/srv/nce')
MAINTENANCE_LOCK = Path('/run/lock/nce-maintenance.lock')
PUBLIC_FIELDS = {'platform_id', 'organization_id', 'environment_id', 'repository', 'archetype', 'origin', 'api_url', 'identity', 'runtime_digest'}
SNAPSHOT_FIELDS = {'app_identifier', 'platform_id', 'organization_id', 'environment_id', 'repository', 'commit',
                   'archetype', 'action', 'plan_hash', 'public_config', 'hosting_binding_digest'}
SERVICES = {'proposal': ('proposal-api', 'proposal-worker', 'sam-browser'), 'academy': ('academy-api',)}
DATABASES = {'proposal': 'studio', 'academy': 'academy'}
ACADEMY_MIGRATION_FIELDS = {'DATABASE_URL', 'ACADEMY_RUNTIME_DB_USERNAME', 'ACADEMY_RUNTIME_DB_PASSWORD', 'ACADEMY_REUSE_RUNTIME_DB_ROLE'}
HEX = re.compile(r'[a-f0-9]{64}\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
IMAGE = re.compile(r'(?:[a-zA-Z0-9][a-zA-Z0-9._/:-]*@)?sha256:[a-f0-9]{64}\Z')


class Refused(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def compose_json(value):
    """Serialize raw environment values for Compose's dollar interpolation pass.

    JSON encoding does not disable Compose interpolation. Escape only the
    serialized copy; callers retain raw values for subsequent override files.
    """
    document = json.loads(json.dumps(value))
    for service in document.get('services', {}).values():
        environment = service.get('environment', {})
        if not isinstance(environment, dict) or any(not isinstance(item, str) for item in environment.values()):
            raise Refused('Runner environment values must be explicit strings')
        service['environment'] = {key: item.replace('$', '$$') for key, item in environment.items()}
    return json.dumps(document, sort_keys=True)


def tree_hash(root):
    values = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise Refused('Staged frontend contains a symlink')
        if path.is_file():
            values[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
            if len(values) > 20000:
                raise Refused('Staged frontend exceeds its file limit')
    if not values or not (root / 'index.html').is_file():
        raise Refused('Staged frontend is incomplete')
    return digest(values)


class Runner:
    def __init__(self, home=HOME, *, enforce_owner=True, run=subprocess.run, maintenance_lock=MAINTENANCE_LOCK):
        self.home = Path(home)
        self.run = run
        self.maintenance_lock = Path(maintenance_lock)
        config_file = self.home / 'config/release-bindings.json'
        info = config_file.stat()
        if (enforce_owner and (info.st_uid != 0 or info.st_mode & 0o022)) or info.st_size > 262144:
            raise Refused('Operator configuration ownership is invalid')
        self.config = json.loads(config_file.read_text())
        if (set(self.config) not in ({'version', 'bindings', 'runtimes', 'releases'}, {'version', 'bindings', 'runtimes', 'releases', 'build'}) or self.config['version'] != 1
                or any(not isinstance(self.config[key], dict) for key in ('bindings', 'runtimes', 'releases'))):
            raise Refused('Invalid operator configuration')

    def validate(self, request):
        if (not isinstance(request, dict) or set(request) not in (
                {'version', 'operation', 'token', 'snapshot'}, {'version', 'operation', 'token', 'snapshot', 'credentials'})
                or request.get('version') != 1 or request.get('operation') not in {'start', 'recover', 'poll', 'stop', 'credentials'}
                or not isinstance(request.get('token'), str) or not HEX.fullmatch(request['token'])):
            raise Refused('Invalid protocol request')
        snapshot = request['snapshot']
        if (not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS
                or not isinstance(snapshot.get('commit'), str) or not COMMIT.fullmatch(snapshot['commit'])
                or snapshot.get('action') not in {'provision', 'release'} or not HEX.fullmatch(snapshot.get('plan_hash', ''))
                or not isinstance(snapshot.get('public_config'), dict) or len(snapshot['public_config']) > 50):
            raise Refused('Invalid reviewed snapshot')
        app = snapshot['app_identifier']
        try:
            if not isinstance(app, str) or str(UUID(app)) != app:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise Refused('Invalid application identity') from None
        binding = self.config['bindings'].get(app)
        if not isinstance(binding, dict) or set(binding) != PUBLIC_FIELDS or digest(binding) != snapshot['hosting_binding_digest']:
            raise Refused('Hosting binding changed')
        for key in ('platform_id', 'organization_id', 'environment_id', 'repository', 'archetype'):
            if snapshot[key] != binding[key]:
                raise Refused('Reviewed application placement does not match')
        for key, value in snapshot['public_config'].items():
            if (not isinstance(key, str) or not re.fullmatch(r'VITE_[A-Z0-9_]{1,100}', key)
                    or re.search(r'SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY', key)
                    or not isinstance(value, str) or len(value) > 4096 or '\x00' in value):
                raise Refused('Invalid public build configuration')
        runtime = self.config['runtimes'].get(app)
        runtime_fields = {'kind', 'frontend', 'amplify_app_id', 'amplify_branch'} if isinstance(runtime, dict) and runtime.get('frontend') == 'amplify' else {'kind', 'frontend'}
        if isinstance(runtime, dict) and 'build_contract_digest' in runtime:
            runtime_fields.add('build_contract_digest')
            if (not isinstance(self.config.get('build'), dict) or runtime['build_contract_digest'] != digest(self.config['build'])):
                raise Refused('The reviewed automatic source build contract changed')
        if (not isinstance(runtime, dict) or set(runtime) != runtime_fields or runtime['kind'] not in SERVICES
                or runtime['frontend'] not in {True, False, 'amplify'}
                or binding['archetype'] != ('amplify-fargate' if runtime['kind'] == 'proposal' else 'amplify-academy-fargate')):
            raise Refused('Invalid fixed runtime mapping')
        if runtime['frontend'] == 'amplify' and (not re.fullmatch(r'd[a-z0-9]{1,19}', runtime['amplify_app_id']) or runtime['amplify_branch'] != 'production'):
            raise Refused('Amplify publication requires the fixed existing app and production branch')
        if digest(runtime) != binding['runtime_digest']:
            raise Refused('The reviewed runtime/frontend destination changed')
        return snapshot, binding, runtime

    def release(self, snapshot, runtime):
        app, commit = snapshot['app_identifier'], snapshot['commit']
        release = self.config['releases'].get(app, {}).get(commit)
        root = self.home / 'releases' / app / commit
        if release is None and runtime.get('build_contract_digest'):
            marker = root / 'automatic-build.json'
            if not marker.is_file() or marker.is_symlink():
                raise Refused('The approved source build is not staged yet')
            managed = json.loads(marker.read_text())
            if (managed.get('build_contract_digest') != runtime['build_contract_digest']
                    or managed.get('binding_digest') != snapshot['hosting_binding_digest']
                    or managed.get('public_config_digest') != digest(snapshot['public_config'])):
                raise Refused('Automatically staged source uses another reviewed contract')
            release = managed['release']
        if (not isinstance(release, dict) or set(release) != {'source_sha256', 'images', 'frontend_sha256'}
                or not HEX.fullmatch(release.get('source_sha256', ''))
                or not isinstance(release['images'], dict) or set(release['images']) != set(SERVICES[runtime['kind']])
                or any(not isinstance(value, str) or not IMAGE.fullmatch(value) for value in release['images'].values())):
            raise Refused('Stage this exact reviewed source and immutable images first')
        if root.is_symlink() or any(path.is_symlink() for path in root.parents if path != self.home.parent):
            raise Refused('Staged artifact root contains a symlink')
        source = root / 'source.tar.gz'
        if not source.is_file() or source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != release['source_sha256']:
            raise Refused('Staged source checksum differs')
        if runtime['frontend'] == 'amplify':
            archive = root / 'frontend.zip'
            if (not HEX.fullmatch(release['frontend_sha256']) or not archive.is_file() or archive.is_symlink()
                    or hashlib.sha256(archive.read_bytes()).hexdigest() != release['frontend_sha256']):
                raise Refused('Staged frontend ZIP checksum differs')
            with ZipFile(archive) as zip_file:
                entries = zip_file.infolist()
                if ('index.html' not in zip_file.namelist() or len(entries) > 20000 or sum(entry.file_size for entry in entries) > 104857600
                        or any(entry.filename.startswith(('/', '\\')) or '\\' in entry.filename or '..' in Path(entry.filename).parts
                               or Path(entry.filename).name.startswith('.env') or (entry.external_attr >> 16) & 0o170000 == 0o120000 for entry in entries)):
                    raise Refused('Staged frontend ZIP has an invalid file layout')
        elif runtime['frontend']:
            if not HEX.fullmatch(release['frontend_sha256']) or tree_hash(root / 'frontend') != release['frontend_sha256']:
                raise Refused('Staged frontend checksum differs')
        elif release['frontend_sha256'] != '':
            raise Refused('Unexpected frontend artifact')
        return release, root

    def _job(self, token):
        return self.home / 'release-state' / token

    def credential_value(self, root):
        file = root / 'core-resource.env'
        if file.is_symlink():
            raise Refused('Staged credentials cannot be a symlink')
        raw = file.read_text().strip()
        prefix = 'CORE_RESOURCE_CREDENTIALS_JSON='
        if not raw.startswith(prefix) or '\n' in raw:
            raise Refused('Invalid staged credential environment')
        encoded = raw[len(prefix):]
        value = json.loads(encoded)
        if (len(encoded.encode()) > 4096 or not isinstance(value, dict) or set(value) not in (
                {'client_id', 'client_secret'}, {'client_id', 'client_secret', 'registrations'})):
            raise Refused('Invalid staged credential bundle')
        return encoded

    def _response(self, request, status, **fields):
        snapshot = request['snapshot']
        return {'version': 1, 'token': request['token'], 'status': status, 'commit': snapshot['commit'],
                'action': snapshot['action'], 'plan_hash': snapshot['plan_hash'], **fields}

    def _outputs(self, snapshot, binding):
        return {'origin': binding['origin'], 'api_url': binding['api_url'], 'commit': snapshot['commit'],
                'identity': json.dumps(binding['identity'], sort_keys=True, separators=(',', ':'))}

    def _load(self, request):
        path = self._job(request['token']) / 'request.json'
        if not path.exists():
            return None
        previous = json.loads(path.read_text())
        if digest(previous) != digest({key: value for key, value in request.items() if key not in {'operation', 'credentials'}}):
            raise Refused('Runner token belongs to a different reviewed plan')
        return json.loads((path.parent / 'result.json').read_text())

    def handle(self, request, *, launch=True):
        snapshot, binding, runtime = self.validate(request)
        operation, token = request['operation'], request['token']
        if operation == 'credentials':
            if runtime.get('build_contract_digest') and snapshot['commit'] not in self.config['releases'].get(snapshot['app_identifier'], {}):
                root = self.home / 'releases' / snapshot['app_identifier'] / snapshot['commit']
                if root.is_symlink() or any(path.is_symlink() for path in root.parents if path != self.home.parent):
                    raise Refused('Credential staging root contains a symlink')
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                _, root = self.release(snapshot, runtime)
            credentials = request.get('credentials')
            # The Core side validates the complete multi-organization structure;
            # this second boundary bounds keys and preserves a raw JSON value.
            if (not isinstance(credentials, dict) or set(credentials) not in (
                    {'client_id', 'client_secret'}, {'client_id', 'client_secret', 'registrations'})
                    or any(not isinstance(credentials.get(key), str) or not 1 <= len(credentials[key]) <= 4096
                           or any(ord(c) < 32 for c in credentials[key]) for key in ('client_id', 'client_secret'))):
                raise Refused('Invalid credential bundle')
            encoded = json.dumps(credentials, separators=(',', ':'))
            if len(encoded.encode()) > 4096 or '\n' in encoded:
                raise Refused('Credential bundle exceeds its limit')
            write_private(root / 'core-resource.env', 'CORE_RESOURCE_CREDENTIALS_JSON=' + encoded + '\n')
            return self._response(request, 'succeeded')
        job = self._job(token)
        job.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (job / 'protocol.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = self._load(request)
            if operation in {'poll', 'recover'}:
                return previous or self._response(request, 'not_found')
            if operation == 'stop':
                if not previous:
                    return self._response(request, 'not_found')
                write_private(job / 'cancel', '1\n')
                return previous
            if previous:
                return previous
            automatic = bool(runtime.get('build_contract_digest')) and snapshot['commit'] not in self.config['releases'].get(snapshot['app_identifier'], {})
            if not automatic:
                self.release(snapshot, runtime)
            intent = {key: value for key, value in request.items() if key != 'operation'}
            write_private(job / 'request.json', json.dumps(intent, sort_keys=True))
            if snapshot['action'] == 'provision':
                result = self._response(request, 'succeeded', outputs=self._outputs(snapshot, binding), completed_steps=['hosting-verified'])
                write_private(job / 'result.json', json.dumps(result, sort_keys=True))
                return result
            result = self._response(request, 'running')
            write_private(job / 'result.json', json.dumps(result, sort_keys=True))
            if launch:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--execute', token],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True, start_new_session=True)
            return result

    def compose(self, *arguments, override=None, stdout=subprocess.DEVNULL, timeout=300):
        command = ['docker', 'compose', '-p', 'nce', '-f', str(self.home / 'compose.yml')]
        if override and override.exists():
            command += ['-f', str(override)]
        self.run([*command, *arguments], cwd=self.home, check=True, stdin=subprocess.DEVNULL,
                 stdout=stdout, stderr=subprocess.DEVNULL, timeout=timeout)

    def database_sql(self, statement):
        """Root-only catalog/rollback SQL, never through an application role."""
        command = ['docker', 'compose', '-p', 'nce', '-f', str(self.home / 'compose.yml'),
                   'exec', '-T', 'postgres', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                   '-U', 'postgres', '-d', 'postgres']
        result = self.run(command, cwd=self.home, check=True, input=statement, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        return result.stdout.strip()

    @staticmethod
    def database_identifier(value):
        if not isinstance(value, str) or not value or '\x00' in value:
            raise Refused('Invalid captured database identifier')
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def database_literal(value):
        if not isinstance(value, str) or '\x00' in value:
            raise Refused('Invalid captured database value')
        return "'" + value.replace("'", "''") + "'"

    def database_metadata(self, database):
        if database not in DATABASES.values():
            raise Refused('Only a fixed application database may be captured')
        value = self.database_literal(database)
        query = """SELECT jsonb_build_object(
 'name',d.datname,'owner',pg_get_userbyid(d.datdba),'encoding',pg_encoding_to_char(d.encoding),
 'collate',d.datcollate,'ctype',d.datctype,'locale_provider',d.datlocprovider,
 'locale',COALESCE(to_jsonb(d)->>'datlocale',to_jsonb(d)->>'daticulocale'),
 'icu_rules',to_jsonb(d)->>'daticurules','collation_version',to_jsonb(d)->>'datcollversion',
 'tablespace',t.spcname,'allow_connections',d.datallowconn,'connection_limit',d.datconnlimit,
 'is_template',d.datistemplate,'comment',obj_description(d.oid,'pg_database'),
 'acl',COALESCE((SELECT jsonb_agg(jsonb_build_object('grantor',pg_get_userbyid(a.grantor),
   'grantee',CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
   'privilege',a.privilege_type,'grantable',a.is_grantable)
   ORDER BY pg_get_userbyid(a.grantor),a.grantee,a.privilege_type,a.is_grantable)
   FROM aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a),'[]'::jsonb),
 'settings',COALESCE((SELECT jsonb_agg(jsonb_build_object('role',CASE WHEN s.setrole=0 THEN NULL ELSE pg_get_userbyid(s.setrole) END,
   'values',(SELECT jsonb_agg(v ORDER BY v) FROM unnest(s.setconfig) v)) ORDER BY s.setrole)
   FROM pg_db_role_setting s WHERE s.setdatabase=d.oid),'[]'::jsonb),
 'security_labels',COALESCE((SELECT jsonb_agg(jsonb_build_object('provider',s.provider,'label',s.label) ORDER BY s.provider)
   FROM pg_shseclabel s WHERE s.classoid='pg_database'::regclass AND s.objoid=d.oid),'[]'::jsonb))
 FROM pg_database d JOIN pg_tablespace t ON t.oid=d.dattablespace WHERE d.datname=""" + value + ';'
        raw = self.database_sql(query)
        if not raw or len(raw.encode()) > 262144:
            raise Refused('Application database metadata is missing or oversized')
        result = json.loads(raw)
        if result.get('name') != database or result.get('locale_provider') not in ('c', 'i', 'b'):
            raise Refused('Unsupported captured database identity or locale provider')
        return result

    def recreate_database(self, metadata):
        """Recreate only the captured app DB; keep original cluster roles intact."""
        database = metadata['name']
        if database not in DATABASES.values():
            raise Refused('Rollback database is outside fixed application scope')
        ident, literal = self.database_identifier, self.database_literal
        self.compose('exec', '-T', 'postgres', 'dropdb', '-U', 'postgres', '--if-exists', database)
        provider = {'c': 'libc', 'i': 'icu', 'b': 'builtin'}[metadata['locale_provider']]
        command = ('SET standard_conforming_strings=on; CREATE DATABASE ' + ident(database)
                   + ' WITH TEMPLATE template0 OWNER ' + ident(metadata['owner'])
                   + ' ENCODING ' + literal(metadata['encoding']) + ' LC_COLLATE ' + literal(metadata['collate'])
                   + ' LC_CTYPE ' + literal(metadata['ctype']) + ' LOCALE_PROVIDER ' + provider
                   + ' TABLESPACE ' + ident(metadata['tablespace'])
                   + ' CONNECTION LIMIT ' + str(int(metadata['connection_limit']))
                   + ' IS_TEMPLATE ' + ('true' if metadata['is_template'] else 'false'))
        if provider in ('icu', 'builtin'):
            command += (' ICU_LOCALE ' if provider == 'icu' else ' BUILTIN_LOCALE ') + literal(metadata['locale'])
        if provider == 'icu' and metadata['icu_rules'] is not None:
            command += ' ICU_RULES ' + literal(metadata['icu_rules'])
        if metadata['collation_version'] is not None:
            command += ' COLLATION_VERSION ' + literal(metadata['collation_version'])
        # Never expose fresh PostgreSQL's default PUBLIC CONNECT permission.
        # Restore admission metadata while connections are disabled, then permit
        # only the original authorized roles while postgres restores contents.
        self.database_sql(command + ' ALLOW_CONNECTIONS false;')
        restoring = dict(metadata)
        restoring['allow_connections'] = True
        self.reapply_database_metadata(restoring)

    def reapply_database_metadata(self, metadata):
        database = metadata['name']
        current = self.database_metadata(database)
        ident, literal = self.database_identifier, self.database_literal
        target = ident(database)
        statements = ['SET standard_conforming_strings=on', 'ALTER DATABASE ' + target + ' OWNER TO ' + ident(metadata['owner'])]
        grantees = {row['grantee'] for row in current['acl']} | {'PUBLIC', metadata['owner']}
        for grantee in sorted(grantees):
            statements.append('REVOKE ALL PRIVILEGES ON DATABASE ' + target + ' FROM '
                              + ('PUBLIC' if grantee == 'PUBLIC' else ident(grantee)) + ' CASCADE')
        grants = []
        for row in sorted(metadata['acl'], key=lambda row: (row['grantor'] != metadata['owner'], row['grantor'], row['grantee'], row['privilege'])):
            if row['privilege'] not in {'CONNECT', 'CREATE', 'TEMPORARY'}:
                raise Refused('Unsupported captured database privilege')
            grants.append('SET ROLE ' + ident(row['grantor']) + '; GRANT ' + row['privilege'] + ' ON DATABASE ' + target + ' TO '
                          + ('PUBLIC' if row['grantee'] == 'PUBLIC' else ident(row['grantee']))
                          + (' WITH GRANT OPTION' if row['grantable'] else '')
                          + ' GRANTED BY ' + ident(row['grantor']) + '; RESET ROLE')
        # Owner grants run first. Further passes resolve delegated/inherited grant
        # chains; the exact final effective ACL comparison is mandatory.
        if len(grants) > 1000:
            raise Refused('Database ACL exceeds the reviewed rollback bound')
        statements.extend(grants * max(1, len(grants)))
        roles = {row['role'] for row in current['settings']} | {row['role'] for row in metadata['settings']} | {None}
        for role in sorted(roles, key=lambda value: value or ''):
            prefix = 'ALTER DATABASE ' + target if role is None else 'ALTER ROLE ' + ident(role) + ' IN DATABASE ' + target
            statements.append(prefix + ' RESET ALL')
        for setting in metadata['settings']:
            prefix = ('ALTER DATABASE ' + target if setting['role'] is None else
                      'ALTER ROLE ' + ident(setting['role']) + ' IN DATABASE ' + target)
            for entry in setting['values']:
                key, separator, value = entry.partition('=')
                if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', key):
                    raise Refused('Invalid captured database setting')
                statements.append(prefix + ' SET ' + ident(key) + ' TO ' + literal(value))
        statements.append('COMMENT ON DATABASE ' + target + ' IS ' + ('NULL' if metadata['comment'] is None else literal(metadata['comment'])))
        for label in metadata['security_labels']:
            statements.append('SECURITY LABEL FOR ' + ident(label['provider']) + ' ON DATABASE ' + target + ' IS ' + literal(label['label']))
        statements.append('ALTER DATABASE ' + target + ' WITH ALLOW_CONNECTIONS ' + ('true' if metadata['allow_connections'] else 'false'))
        self.database_sql(';\n'.join(statements) + ';')
        if self.database_metadata(database) != metadata:
            raise Refused('Rollback database metadata/ACL/role settings differ; keep writers stopped')

    @contextmanager
    def deployment_locks(self):
        """Share the backup/restore lock before taking the local release lock.

        Acquire before any maintenance/config capture or release cleanup. A
        contending run must not remove another operation's maintenance flags.
        """
        self.maintenance_lock.parent.mkdir(parents=True, exist_ok=True)
        with self.maintenance_lock.open('a') as shared:
            fcntl.flock(shared, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (self.home / 'release-state' / 'deployment.lock').open('a') as local:
                fcntl.flock(local, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield

    def amplify(self, operation, runtime, *, job_id=None):
        credentials = Path('/etc/nce-release/aws-credentials')
        info = credentials.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise Refused('Amplify credentials must remain root-only')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'AWS_SHARED_CREDENTIALS_FILE': str(credentials),
               'AWS_CONFIG_FILE': '/dev/null', 'AWS_PROFILE': 'nce-release', 'AWS_REGION': 'us-east-1',
               'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': ''}
        command = ['aws', 'amplify', operation, '--app-id', runtime['amplify_app_id'], '--branch-name', 'production',
                   '--profile', 'nce-release', '--region', 'us-east-1', '--output', 'json', '--no-cli-pager']
        if job_id is not None:
            if not re.fullmatch(r'[0-9]{1,255}', str(job_id)):
                raise Refused('Invalid fixed Amplify job identifier')
            command += ['--job-id', str(job_id)]
        response = self.run(command, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=90)
        if len(response.stdout.encode()) > 131072:
            raise Refused('Amplify result exceeds its private transport limit')
        return json.loads(response.stdout)

    def upload(self, url, archive):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.amazonaws.com')
                or parsed.username or parsed.password or parsed.fragment):
            raise Refused('Unexpected Amplify upload endpoint')

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, newurl):
                return None

        request = Request(url, data=archive.read_bytes(), method='PUT', headers={'Content-Type': 'application/zip'})
        with build_opener(NoRedirect()).open(request, timeout=120) as response:
            if response.status not in {200, 201, 204}:
                raise Refused('Amplify artifact upload failed')

    def publish_amplify(self, job, root, runtime):
        receipt = job / 'amplify.json'
        if receipt.exists():
            # CreateDeployment has no caller idempotency token. An uncertain
            # create/upload/start must be reconciled by the operator, not retried.
            raise Refused('Review the existing Amplify publication attempt before retrying')
        write_private(receipt, json.dumps({'state': 'creating'}))
        created = self.amplify('create-deployment', runtime)
        job_id, upload_url = created.get('jobId'), created.get('zipUploadUrl')
        if not isinstance(job_id, str) or not re.fullmatch(r'[0-9]{1,255}', job_id) or not isinstance(upload_url, str):
            raise Refused('Amplify returned an invalid manual deployment')
        write_private(receipt, json.dumps({'state': 'created', 'job_id': job_id}))
        self.upload(upload_url, root / 'frontend.zip')
        if (job / 'cancel').exists():
            raise Refused('Release cancelled before frontend publication')
        write_private(receipt, json.dumps({'state': 'starting', 'job_id': job_id}))
        self.amplify('start-deployment', runtime, job_id=job_id)
        for _ in range(120):
            result = self.amplify('get-job', runtime, job_id=job_id)
            summary = result.get('job', {}).get('summary', {})
            if summary.get('jobId') != job_id:
                raise Refused('Amplify publication receipt differs')
            state = summary.get('status')
            if state == 'SUCCEED':
                write_private(receipt, json.dumps({'state': 'succeeded', 'job_id': job_id}))
                if (job / 'cancel').exists():
                    raise Refused('Cancellation recorded after publication; operator review required')
                return
            if state in {'FAILED', 'CANCELLED'}:
                raise Refused('Amplify publication failed')
            if state not in {'CREATED', 'PENDING', 'PROVISIONING', 'RUNNING', 'CANCELLING'}:
                raise Refused('Amplify returned an unknown publication state')
            time.sleep(5)
        raise Refused('Amplify publication did not reach a verified terminal state')

    def execute(self, token):
        if not HEX.fullmatch(token):
            raise Refused('Invalid execution token')
        job = self._job(token)
        request = {**json.loads((job / 'request.json').read_text()), 'operation': 'start'}
        snapshot, binding, runtime = self.validate(request)
        if snapshot['action'] == 'provision':
            write_private(job / 'result.json', json.dumps(self._response(request, 'succeeded', outputs=self._outputs(snapshot, binding),
                          completed_steps=['hosting-verified']), sort_keys=True))
            return
        if runtime.get('build_contract_digest') and snapshot['commit'] not in self.config['releases'].get(snapshot['app_identifier'], {}):
            helper = Path(__file__).resolve().with_name('nce-lightsail-stage-artifacts')
            self.run([sys.executable, str(helper)], input=json.dumps({'snapshot': snapshot, 'token': token}), text=True,
                     check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3300)
        release, root = self.release(snapshot, runtime)
        if snapshot['action'] != 'release' or not (root / 'core-resource.env').is_file():
            raise Refused('Release requires staged existing credentials')
        credential_json = self.credential_value(root)
        services = SERVICES[runtime['kind']]
        current = self.home / 'config' / ('current-' + runtime['kind'] + '.json')
        target = self.home / 'static' / runtime['kind']
        stopped = False
        migration_started = False
        runtime_started = False
        maintenance = self.home / 'maintenance' / runtime['kind']
        with self.deployment_locks():
            if not current.is_file() or current.is_symlink():
                raise Refused('An operator-pinned current image override is required for rollback')
            previous = current.read_text()
            old_target = os.readlink(target) if target.is_symlink() else None
            try:
                if (job / 'cancel').exists():
                    raise Refused('Release cancelled before migration')
                # Verify each exact image is already admitted locally. Only the
                # fixed artifact helper may pull reviewed application images.
                for image in release['images'].values():
                    self.run(['docker', 'image', 'inspect', image], check=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                maintenance.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                write_private(maintenance, 'release in progress\n')
                maintenance.chmod(0o644)
                stopped = True
                self.compose('stop', *services, override=current)
                metadata = self.database_metadata(DATABASES[runtime['kind']])
                write_private(job / 'database-metadata.json', json.dumps(metadata, sort_keys=True))
                backup = job / 'database.dump'
                with backup.open('xb') as handle:
                    backup.chmod(0o600)
                    self.compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'postgres', '-d', DATABASES[runtime['kind']],
                                 '--format=custom', '--no-owner', stdout=handle)
                if backup.stat().st_size < 32:
                    raise Refused('Database backup is empty')
                with backup.open('rb') as handle:
                    command = ['docker', 'compose', '-p', 'nce', '-f', str(self.home / 'compose.yml'),
                               'exec', '-T', 'postgres', 'pg_restore', '--list']
                    self.run(command, cwd=self.home, check=True, stdin=handle, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=300)
                if (job / 'cancel').exists():
                    raise Refused('Release cancelled before migration')
                override = {'services': {service: {'image': release['images'][service],
                            **({'env_file': [{'path': str(root / 'core-resource.env'), 'format': 'raw'}],
                                'environment': {'CORE_RESOURCE_CREDENTIALS_JSON': credential_json}}
                               if service != 'sam-browser' else {})} for service in services}}
                staged = job / 'compose-override.json'
                write_private(staged, compose_json(override))
                migration = ['python', '-m', 'studio.runtime', 'migrate'] if runtime['kind'] == 'proposal' else ['python', '-m', 'academy.runtime', 'migrate']
                migration_override = staged
                if runtime['kind'] == 'academy':
                    owner_env = self.home / 'secrets/academy-migration.env'
                    if not owner_env.is_file() or owner_env.is_symlink():
                        raise Refused('Academy requires its separate owner-only migration configuration')
                    migration_override = job / 'migration-override.json'
                    migration_document = json.loads(json.dumps(override))
                    migration_document['services']['academy-api']['env_file'].append({'path': str(owner_env), 'format': 'raw'})
                    owner_values = {}
                    for line in owner_env.read_text().splitlines():
                        if not line or line.startswith('#'):
                            continue
                        key, separator, value = line.partition('=')
                        if not separator or key not in ACADEMY_MIGRATION_FIELDS or key in owner_values or not value:
                            raise Refused('Invalid fixed Academy migration environment')
                        owner_values[key] = value
                    if set(owner_values) != ACADEMY_MIGRATION_FIELDS or owner_values['ACADEMY_REUSE_RUNTIME_DB_ROLE'] != '1':
                        raise Refused('Academy migration environment is incomplete')
                    migration_document['services']['academy-api']['environment'].update(owner_values)
                    write_private(migration_override, compose_json(migration_document))
                migration_started = True
                self.compose('run', '--rm', '--no-deps', services[0], *migration, override=migration_override, timeout=600)
                if (job / 'cancel').exists():
                    raise Refused('Release cancelled after migration')
                if runtime['frontend'] is True:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and not target.is_symlink():
                        raise Refused('Frontend must first use an operator-created versioned symlink')
                    temporary = target.with_name(target.name + '.next-' + token[:12])
                    temporary.symlink_to(root / 'frontend')
                    os.replace(temporary, target)
                runtime_started = True
                self.compose('up', '-d', '--no-deps', '--wait', '--wait-timeout', '120', *services, override=staged)
                for service in services:
                    path = '/health' if service == 'sam-browser' else '/api/ready'
                    port = '8010' if service == 'sam-browser' else '8000'
                    if service == 'proposal-worker':
                        self.compose('exec', '-T', service, 'python', '-m', 'studio.runtime', 'worker-health', override=staged, timeout=30)
                        continue
                    script = 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:' + port + path + '",timeout=10).read()'
                    self.compose('exec', '-T', service, 'python', '-c', script, override=staged, timeout=30)
                if (job / 'cancel').exists():
                    raise Refused('Cancellation recorded after application start; preserve new writes for operator recovery')
                if runtime['frontend'] == 'amplify':
                    self.publish_amplify(job, root, runtime)
                write_private(current, compose_json(override))
                result = self._response(request, 'succeeded', outputs=self._outputs(snapshot, binding),
                                        completed_steps=['source-verified', 'database-migrated', 'services-healthy', 'frontend-published'])
                write_private(job / 'result.json', json.dumps(result, sort_keys=True))
                maintenance.unlink()
            except Exception:
                # Credentials, DB URLs and command output never reach receipts.
                # Schema rollback is explicit: restore the pre-migration snapshot
                # while old app writers remain stopped, then restart old images.
                rollback_ok = not stopped
                if stopped:
                    try:
                        self.compose('stop', *services, override=job / 'compose-override.json')
                        if runtime_started:
                            raise Refused('Preserve possible new application or external writes for operator recovery')
                        write_private(current, previous)
                        if migration_started:
                            metadata = json.loads((job / 'database-metadata.json').read_text())
                            self.recreate_database(metadata)
                            with (job / 'database.dump').open('rb') as handle:
                                command = ['docker', 'compose', '-p', 'nce', '-f', str(self.home / 'compose.yml'), 'exec', '-T',
                                           'postgres', 'pg_restore', '-U', 'postgres', '-d', DATABASES[runtime['kind']], '--role', metadata['owner'], '--no-owner', '--exit-on-error']
                                self.run(command, cwd=self.home, check=True, stdin=handle, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=600)
                            self.reapply_database_metadata(metadata)
                        if old_target is not None:
                            if target.is_symlink():
                                target.unlink()
                            target.symlink_to(old_target)
                        self.compose('up', '-d', '--no-deps', '--wait', '--wait-timeout', '120', *services, override=current)
                        rollback_ok = True
                    except Exception:
                        rollback_ok = False
                if rollback_ok and maintenance.exists():
                    maintenance.unlink()
                result = self._response(request, 'failed', error='Release failed; rollback verified.' if rollback_ok else
                                        'Release failed; operator recovery is required. Application writers remain stopped.')
                write_private(job / 'result.json', json.dumps(result, sort_keys=True))


def main():
    if os.geteuid() != 0:
        raise Refused('Root-owned protocol execution required')
    runner = Runner()
    if len(sys.argv) == 3 and sys.argv[1] == '--execute':
        token = sys.argv[2]
        try:
            runner.execute(token)
        except Exception:
            if HEX.fullmatch(token):
                job = runner._job(token)
                request = json.loads((job / 'request.json').read_text())
                write_private(job / 'result.json', json.dumps(runner._response(request, 'failed', error='Operator review required before restart.')))
        return
    if len(sys.argv) != 1:
        raise Refused('Protocol accepts JSON stdin only')
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768:
        raise Refused('Protocol request exceeds limit')
    request = json.loads(raw)
    try:
        response = runner.handle(request)
    except Refused:
        snapshot = request.get('snapshot', {})
        response = {'version': 1, 'token': request.get('token', ''), 'status': 'refused', 'commit': snapshot.get('commit'),
                    'action': snapshot.get('action'), 'plan_hash': snapshot.get('plan_hash')}
    print(json.dumps(response, sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(1)
