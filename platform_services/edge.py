"""Edge management policy and durable jobs; no remote shell or infrastructure calls."""
import hashlib
import hmac
import io
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tarfile
import tempfile
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from identity.access import Principal, effective_tags, require_access
from identity.models import AuditEvent, Membership, Organization, User
from identity.policy import authorization_for, membership_for
from .edge_models import EdgeCredential, EdgeDevice, EdgeEnrollment, EdgeJob, EdgeRelease, secret_digest
from .models import Installation, MarketplacePackage
from .services import install_package

PACKAGE = 'nce-robotics'
PERMISSIONS = {
    'device-read': 'View robot devices and jobs', 'device-manage': 'Enroll and revoke robot devices',
    'release-manage': 'Upload robot software releases', 'software-deploy': 'Request robot software deployments',
    'os-manage': 'Request robot OS maintenance', 'job-approve': 'Approve robot changes',
    'device-control': 'Open authenticated direct robot connections',
}
OPERATIONS = {'status.refresh': 'device-read', 'app.command': 'device-read', 'app.deploy': 'software-deploy', 'app.rollback': 'software-deploy',
              'os.update.check': 'os-manage', 'os.update.apply': 'os-manage', 'os.reboot': 'os-manage'}
MUTATIONS = {'app.deploy', 'app.rollback', 'os.update.apply', 'os.reboot'}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'uncertain'}
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
LEASE_SECONDS = 120
SECRET_FIELD = re.compile(r'password|passwd|secret|token|authorization|private.?key|cookie', re.I)
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class Conflict(Exception):
    pass


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def safe_document(value, limit=262144):
    if not isinstance(value, dict):
        raise ValidationError('Use a JSON object.')
    def visit(item, depth=0):
        if depth > 8:
            raise ValidationError('The report is too deeply nested.')
        if isinstance(item, dict):
            if any(not isinstance(k, str) or len(k) > 100 or SECRET_FIELD.search(k) for k in item):
                raise ValidationError('Reports must not contain credential fields.')
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 2000:
                raise ValidationError('The report contains too many entries.')
            for child in item:
                visit(child, depth + 1)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValidationError('Use JSON values.')
    visit(value)
    try:
        encoded = json.dumps(value, allow_nan=False).encode()
    except (ValueError, TypeError, OverflowError):
        raise ValidationError('Use finite JSON values.') from None
    if len(encoded) > limit:
        raise ValidationError('The report exceeds the size limit.')
    return value


def active_installation(installation):
    return bool(installation.package.slug == PACKAGE and installation.package.active and installation.enabled and
                installation.organization.active and installation.service.active and
                installation.manifest.get('external_runtime') == 'edge-device')


def authorize(principal, installation, permission='device-read', resource=None):
    if not active_installation(installation) or not principal.user or not principal.user.is_active:
        raise PermissionDenied
    if principal.application and (principal.application.service_id != installation.service_id or
                                  not principal.application.is_usable(None)):
        raise PermissionDenied
    member = membership_for(principal.user, installation.service)
    if not member and not principal.user.is_superuser:
        raise PermissionDenied
    require_access(principal, installation, 'edge.installation_access')
    if resource:
        if resource.organization_id != installation.organization_id:
            raise PermissionDenied
        require_access(principal, resource, 'edge.resource_access')
    permissions = set(authorization_for(member, installation.service, principal)['permissions']) if member else {'admin'}
    if permission not in permissions and 'admin' not in permissions:
        audit(installation, 'edge.permission_denied', principal=principal, target=resource.pk if resource else installation.pk,
            outcome='denied', reason='permission_required')
        raise PermissionDenied
    return permissions


def installation_for_organization(organization):
    try:
        return Installation.objects.select_related('organization', 'service', 'package').get(
            organization=organization, package__slug=PACKAGE)
    except Installation.DoesNotExist:
        raise PermissionDenied from None


def audit(installation, action, *, principal=None, device=None, target=None, outcome='allowed', reason=''):
    AuditEvent.objects.create(organization=installation.organization, actor=principal.user if principal else None,
        principal_kind='user' if principal else 'edge-device', principal_id=principal.user.pk if principal else device.pk if device else None,
        request_id=principal.request_id if principal else None, action=action, target=str(target or (device.pk if device else installation.pk)),
        outcome=outcome, reason_code=reason)


@transaction.atomic
def register_application(organization, actor=None):
    Organization.objects.select_for_update().get(pk=organization.pk)
    package, _ = MarketplacePackage.objects.get_or_create(slug=PACKAGE, defaults={
        'name': 'NCE Robotics', 'description': 'Local robot control and managed edge software.', 'kind': 'application',
        'version': '0.1.0', 'manifest': {'external_runtime': 'edge-device', 'capabilities': [], 'dependencies': ['core-platform'],
            'permissions': [{'codename': key, 'name': name} for key, name in PERMISSIONS.items()]}})
    if package.manifest.get('external_runtime') != 'edge-device':
        raise ValidationError('The reserved NCE Robotics package has incompatible configuration.')
    installation = install_package(organization, package, actor=actor)
    from identity.models import Application, ServicePermission
    for codename, name in PERMISSIONS.items():
        ServicePermission.objects.get_or_create(service=installation.service, codename=codename, defaults={'name': name})
    callback = settings.ISSUER + '/apps/nce-robotics/oauth/callback'
    app = Application.objects.filter(service=installation.service, name='NCE Robotics iOS').first()
    if not app:
        app = Application(service=installation.service, name='NCE Robotics iOS', client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE, algorithm=Application.RS256_ALGORITHM,
            redirect_uris=callback, allowed_origins='')
        app.full_clean()
        app.save()
    elif app.client_type != Application.CLIENT_PUBLIC or app.redirect_uris != callback or not app.active:
        raise ValidationError('The existing iOS registration must be reviewed before it can be reused.')
    return installation, app


@transaction.atomic
def register_development_client(organization, actor=None):
    """Create a separate opt-in Personal Team client; never edit the HTTPS client."""
    from identity.models import Application
    from identity.native_clients import ROBOTICS_DEVELOPMENT_CLIENT_NAME, ROBOTICS_DEVELOPMENT_REDIRECT
    installation, _ = register_application(organization, actor)
    app = Application.objects.filter(service=installation.service, native_development=True).first()
    if app:
        app.full_clean()
        if not app.active:
            raise ValidationError('This development registration is revoked. Review it before reusing it.')
        return installation, app
    app = Application(service=installation.service, name=ROBOTICS_DEVELOPMENT_CLIENT_NAME,
        client_type=Application.CLIENT_PUBLIC, authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        algorithm=Application.RS256_ALGORITHM, redirect_uris=ROBOTICS_DEVELOPMENT_REDIRECT,
        allowed_origins='', native_development=True)
    app.full_clean()
    app.save()
    audit(installation, 'edge.development_client_registered',
        principal=Principal(user=actor) if actor else None, target=app.pk)
    return installation, app


def command_capable(device):
    capabilities = device.report.get('capabilities')
    return isinstance(capabilities, list) and 'app.command' in capabilities


def device_values(device, permissions=None):
    value = {'id': str(device.pk), 'name': device.name, 'active': device.active, 'online': device.online,
        'last_seen_at': device.last_seen_at.isoformat() if device.last_seen_at else None,
        'agent_version': device.agent_version, 'report': device.report, 'installation_id': str(device.installation_id)}
    if permissions is not None:
        value['allowed_actions'] = [kind for kind, permission in OPERATIONS.items()
            if device.active and ('admin' in permissions or permission in permissions)
            and (kind != 'app.command' or command_capable(device))]
    return value


def release_values(release):
    return {'id': str(release.pk), 'application': PACKAGE, 'version': release.version, 'sha256': release.sha256,
            'size_bytes': release.size_bytes, 'created_at': release.created_at.isoformat()}


def job_values(job, permissions=None):
    value = {'id': str(job.pk), 'device_id': str(job.device_id), 'kind': job.kind, 'arguments': job.arguments,
        'status': job.status, 'request_id': str(job.request_id), 'created_at': job.created_at.isoformat(),
        'approved_at': job.approved_at.isoformat() if job.approved_at else None,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        'result': job.result, 'error': job.error}
    if permissions is not None:
        value['can_approve'] = bool(job.status == 'awaiting_approval' and ('admin' in permissions or
            'job-approve' in permissions and OPERATIONS[job.kind] in permissions))
        value['can_cancel'] = bool(job.status not in TERMINAL and ('admin' in permissions or OPERATIONS[job.kind] in permissions))
    return value


@transaction.atomic
def create_enrollment(installation, principal, name):
    authorize(principal, installation, 'device-manage')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValidationError('Use a device name of 1–100 characters.')
    device = EdgeDevice.objects.create(installation=installation, name=name.strip())
    raw = 'enr_' + secrets.token_urlsafe(32)
    enrollment = EdgeEnrollment.objects.create(device=device, code_hash=secret_digest(raw),
        expires_at=timezone.now() + timedelta(minutes=10), created_by=principal.user,
        evidence=principal.evidence, application=principal.application)
    audit(installation, 'edge.enrollment_created', principal=principal, device=device)
    return device, enrollment, raw


@transaction.atomic
def enroll(code, name, agent_version, report):
    if not isinstance(code, str) or not 20 <= len(code) <= 256:
        raise PermissionDenied
    enrollment = EdgeEnrollment.objects.select_for_update().select_related('device__installation__organization',
        'device__installation__service', 'device__installation__package', 'created_by').filter(code_hash=secret_digest(code)).first()
    if not enrollment or enrollment.used_at or enrollment.expires_at <= timezone.now():
        raise PermissionDenied
    device = enrollment.device
    principal = Principal(user=enrollment.created_by, evidence=enrollment.evidence, application=enrollment.application)
    authorize(principal, device.installation, 'device-manage', device)
    if not device.active:
        raise PermissionDenied
    if name and name != device.name:
        raise ValidationError('The enrollment device name does not match.')
    safe_document(report)
    if not isinstance(agent_version, str) or len(agent_version) > 64:
        raise ValidationError('Invalid agent version.')
    raw = 'edg_' + secrets.token_urlsafe(48)
    credential = EdgeCredential.objects.create(device=device, token_hash=secret_digest(raw),
        expires_at=timezone.now() + timedelta(days=getattr(settings, 'CORE_EDGE_CREDENTIAL_DAYS', 90)))
    enrollment.used_at = timezone.now()
    enrollment.save(update_fields=['used_at'])
    device.agent_version, device.report, device.last_seen_at = agent_version, report, timezone.now()
    device.save(update_fields=['agent_version', 'report', 'last_seen_at'])
    audit(device.installation, 'edge.device_enrolled', device=device)
    return device, credential, raw


def authenticate_device(raw):
    if not raw or not raw.startswith('edg_'):
        raise PermissionDenied
    credential = EdgeCredential.objects.select_related('device__installation__organization', 'device__installation__service',
        'device__installation__package', 'device__environment').filter(token_hash=secret_digest(raw), active=True,
        expires_at__gt=timezone.now(), device__active=True).first()
    if not credential:
        raise PermissionDenied
    return current_credential(credential)


def current_credential(credential):
    credential = EdgeCredential.objects.select_related('device__installation__organization', 'device__installation__service',
        'device__installation__package', 'device__environment').filter(pk=credential.pk, active=True,
        expires_at__gt=timezone.now(), device__active=True).first()
    if not credential or not active_installation(credential.device.installation):
        raise PermissionDenied
    device = credential.device
    if device.environment_id and (not device.environment.active or device.environment.organization_id != device.organization_id):
        raise PermissionDenied
    # Device credentials grant this device only, never a human role or another app's resources.
    if any(not tag.active or not tag.allow_workloads or tag.requires_mfa or tag.max_auth_age or tag.allowed_purposes
           for tag in effective_tags(device)):
        raise PermissionDenied
    return credential


@transaction.atomic
def revoke_device(device, principal):
    device = EdgeDevice.objects.select_for_update().get(pk=device.pk)
    authorize(principal, device.installation, 'device-manage', device)
    device.active = False
    device.save(update_fields=['active'])
    device.credentials.update(active=False)
    device.enrollments.filter(used_at__isnull=True).update(expires_at=timezone.now())
    device.jobs.filter(status__in=['awaiting_approval', 'queued', 'claimed']).update(
        status='cancelled', completed_at=timezone.now(), error='Device access was revoked.')
    device.jobs.filter(status__in=['running', 'rebooting']).update(cancel_requested=True)
    audit(device.installation, 'edge.device_revoked', principal=principal, device=device)


def validate_archive(content):
    if not content or len(content) > MAX_ARTIFACT:
        raise ValidationError('Use a nonempty tar.gz release no larger than 16 MiB.')
    paths, regular_paths, expanded, manifest = set(), set(), 0, None
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValidationError('Release manifest contains duplicate keys.')
            value[key] = item
        return value
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
            raw = compressed.read(MAX_EXPANDED + 1024 * 1024 + 1)
        if len(raw) > MAX_EXPANDED + 1024 * 1024:
            raise ValidationError('Release expands beyond the size limit.')
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for index, item in enumerate(archive):
                path = PurePosixPath(item.name)
                normalized = str(path)
                if (index >= 512 or len(item.name) > 240 or item.name.startswith('/') or '\\' in item.name or
                        any(ord(c) < 32 for c in item.name) or '..' in path.parts or normalized in {'.', ''} or
                        normalized != item.name.rstrip('/') or normalized in paths or
                        not (item.isfile() or item.isdir()) or item.mode & 0o7000 or item.sparse is not None or
                        item.pax_headers or archive.pax_headers):
                    raise ValidationError('Release contains an unsafe archive entry.')
                paths.add(normalized)
                if normalized not in {'nce-release.json', 'app'} and not normalized.startswith('app/'):
                    raise ValidationError('Release contains an unexpected root path.')
                if any(str(parent) in regular_paths for parent in path.parents):
                    raise ValidationError('Release has a file/directory collision.')
                if item.isfile() and any(name.startswith(normalized + '/') for name in paths):
                    raise ValidationError('Release has a file/directory collision.')
                if item.isfile():
                    regular_paths.add(normalized)
                expanded += item.size
                if item.size < 0 or expanded > MAX_EXPANDED:
                    raise ValidationError('Release expands beyond 64 MiB.')
                if normalized == 'nce-release.json':
                    if not item.isfile() or item.size > 8192:
                        raise ValidationError('Invalid release manifest.')
                    manifest = json.load(archive.extractfile(item), object_pairs_hook=unique_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-JSON number')))
                # Read every regular file so truncated gzip/tar content cannot pass validation.
                elif item.isfile():
                    source = archive.extractfile(item)
                    while source.read(1024 * 1024):
                        pass
    except (tarfile.TarError, OSError, ValueError, UnicodeError, EOFError):
        raise ValidationError('The release is not a valid tar.gz archive.') from None
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'application', 'version', 'entrypoint'} or
            type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['application'] != PACKAGE or
            not isinstance(manifest['version'], str) or not VERSION.fullmatch(manifest['version']) or
            '..' in manifest['version'] or
            manifest['entrypoint'] != 'app/main.py' or 'app/main.py' not in regular_paths):
        raise ValidationError('Use the NCE Robotics release manifest and app/main.py entrypoint.')
    return manifest


def upload_release(installation, principal, content):
    authorize(principal, installation, 'release-manage')
    manifest = validate_archive(content)
    digest = hashlib.sha256(content).hexdigest()
    old = EdgeRelease.objects.filter(installation=installation, version=manifest['version']).first()
    if old:
        if old.sha256 != digest:
            raise Conflict('A different artifact already uses this version.')
        return old, False
    if not settings.CORE_EDGE_ARTIFACT_ROOT:
        raise ValidationError('An operator must configure persistent CORE_EDGE_ARTIFACT_ROOT storage before uploading releases.')
    root = Path(settings.CORE_EDGE_ARTIFACT_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (secrets.token_hex(16) + '.tar.gz')
    try:
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            stream.write(content)
        with transaction.atomic():
            Installation.objects.select_for_update().get(pk=installation.pk)
            authorize(principal, installation, 'release-manage')
            old = EdgeRelease.objects.filter(installation=installation, version=manifest['version']).first()
            if old:
                if old.sha256 != digest:
                    raise Conflict('A different artifact already uses this version.')
                path.unlink()
                return old, False
            release = EdgeRelease.objects.create(installation=installation, version=manifest['version'], sha256=digest,
                size_bytes=len(content), artifact_path=str(path), manifest=manifest, created_by=principal.user)
            audit(installation, 'edge.release_uploaded', principal=principal, target=release.pk)
            return release, True
    except Exception:
        path.unlink(missing_ok=True)
        raise


def validated_arguments(device, kind, arguments):
    if kind not in OPERATIONS or not isinstance(arguments, dict):
        raise ValidationError('Unsupported edge operation.')
    if kind == 'app.command':
        if not command_capable(device):
            raise ValidationError('This device needs the command-capable edge agent before using the console.')
        command = arguments.get('command')
        if (set(arguments) != {'command'} or not isinstance(command, str) or not 1 <= len(command) <= 240
                or command != command.strip() or not command.isprintable()):
            raise ValidationError('Use a command of 1–240 printable characters.')
        return {'command': command}
    if kind == 'app.deploy':
        if set(arguments) != {'release_id'}:
            raise ValidationError('app.deploy requires only release_id.')
        try:
            release = EdgeRelease.objects.get(pk=UUID(str(arguments['release_id'])), installation=device.installation)
        except (ValueError, EdgeRelease.DoesNotExist):
            raise ValidationError('Choose a release for this application.') from None
        return {'release_id': str(release.pk)}
    if kind == 'os.update.apply':
        if (set(arguments) != {'plan_id', 'sha256'} or not isinstance(arguments.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', arguments['sha256'])):
            raise ValidationError('os.update.apply requires the checked plan_id and SHA-256 digest.')
        try:
            plan_id = str(UUID(str(arguments['plan_id'])))
        except ValueError:
            raise ValidationError('Use a valid OS update plan ID.') from None
        check = device.jobs.filter(kind='os.update.check', status='succeeded', completed_at__gt=timezone.now()-timedelta(hours=1)).order_by('-completed_at').first()
        plan = check.result.get('plan', check.result) if check else {}
        if plan.get('plan_id') != plan_id or plan.get('sha256') != arguments['sha256']:
            raise ValidationError('Run a new OS update check and approve its exact plan.')
        return {'plan_id': plan_id, 'sha256': arguments['sha256']}
    if arguments:
        raise ValidationError('This operation accepts no arguments.')
    return {}


def authorize_operation(principal, device, kind, arguments, resource=None):
    authorize(principal, device.installation, OPERATIONS.get(kind, 'device-read'), resource or device)
    if kind == 'app.deploy':
        release = EdgeRelease.objects.filter(pk=arguments.get('release_id'), installation=device.installation).first()
        if not release:
            raise PermissionDenied
        require_access(principal, release, 'edge.release_use')
    elif kind == 'app.rollback' and device.previous_release_id:
        require_access(principal, device.previous_release, 'edge.release_use')


@transaction.atomic
def request_job(device, principal, kind, arguments, request_id):
    device = EdgeDevice.objects.select_for_update().get(pk=device.pk)
    authorize(principal, device.installation, OPERATIONS.get(kind, 'device-read'), device)
    if not device.active:
        raise ValidationError('This device is revoked.')
    arguments = validated_arguments(device, kind, arguments)
    authorize_operation(principal, device, kind, arguments)
    try:
        request_id = UUID(str(request_id))
    except ValueError:
        raise ValidationError('Use a UUID request_id.') from None
    digest = canonical_digest({'kind': kind, 'arguments': arguments})
    previous = device.jobs.filter(request_id=request_id).first()
    if previous:
        if previous.request_digest != digest:
            raise Conflict('The request_id was already used for different work.')
        return previous, False
    job = EdgeJob.objects.create(device=device, kind=kind, arguments=arguments, request_id=request_id, request_digest=digest,
        requested_by=principal.user, request_evidence=principal.evidence, request_application=principal.application,
        purpose=principal.purpose, status='awaiting_approval' if kind in MUTATIONS else 'queued',
        expires_at=timezone.now()+(timedelta(minutes=2) if kind == 'app.command' else timedelta(hours=1)))
    audit(device.installation, 'edge.job_requested', principal=principal, device=device, target=job.pk)
    return job, True


def live_job_authority(job):
    if job.request_digest != canonical_digest({'kind': job.kind, 'arguments': job.arguments}):
        return False
    try:
        user = User.objects.get(pk=job.requested_by_id)
        principal = Principal(user=user, evidence=job.request_evidence, application=job.request_application, purpose=job.purpose)
        authorize_operation(principal, job.device, job.kind, job.arguments, job)
        if job.kind in MUTATIONS:
            if not job.approved_by_id or not job.approved_at:
                raise PermissionDenied
            approver = User.objects.get(pk=job.approved_by_id)
            principal = Principal(user=approver, evidence=job.approval_evidence, application=job.approval_application, purpose=job.purpose)
            authorize(principal, job.device.installation, 'job-approve', job)
            authorize_operation(principal, job.device, job.kind, job.arguments, job)
        return True
    except (PermissionDenied, User.DoesNotExist):
        return False


@transaction.atomic
def approve_job(job, principal):
    job = EdgeJob.objects.select_for_update().select_related('device__installation').get(pk=job.pk)
    authorize(principal, job.device.installation, 'job-approve', job)
    authorize_operation(principal, job.device, job.kind, job.arguments, job)
    if job.kind not in MUTATIONS or job.status != 'awaiting_approval' or job.expires_at <= timezone.now():
        raise Conflict('This job cannot be approved in its current state.')
    if job.request_digest != canonical_digest({'kind': job.kind, 'arguments': job.arguments}):
        raise Conflict('The requested job changed. Create a new request.')
    if not live_request_authority(job):
        raise PermissionDenied
    # Revalidate pinned OS check immediately before approval, not only on submission.
    validated_arguments(job.device, job.kind, job.arguments)
    job.approved_by, job.approval_evidence, job.approval_application = principal.user, principal.evidence, principal.application
    job.approved_at, job.status = timezone.now(), 'queued'
    job.save()
    audit(job.device.installation, 'edge.job_approved', principal=principal, device=job.device, target=job.pk)
    return job


def live_request_authority(job):
    try:
        authorize_operation(Principal(user=User.objects.get(pk=job.requested_by_id), evidence=job.request_evidence,
            application=job.request_application, purpose=job.purpose), job.device, job.kind, job.arguments, job)
        return True
    except (User.DoesNotExist, PermissionDenied):
        return False


@transaction.atomic
def cancel_job(job, principal):
    job = EdgeJob.objects.select_for_update().get(pk=job.pk)
    authorize(principal, job.device.installation, OPERATIONS[job.kind], job)
    if job.status in TERMINAL:
        return job
    job.cancel_requested = True
    if job.status in {'awaiting_approval', 'queued', 'claimed'}:
        job.status, job.completed_at = 'cancelled', timezone.now()
    job.save()
    audit(job.device.installation, 'edge.job_cancelled', principal=principal, device=job.device, target=job.pk)
    return job


def _expire(device):
    now = timezone.now()
    for job in device.jobs.select_for_update().filter(status__in=['claimed', 'running'], lease_expires_at__lte=now):
        if job.kind in MUTATIONS:
            job.status, job.error = 'uncertain', 'Agent lease expired; inspect the device before requesting new work.'
        else:
            job.status, job.error = 'failed', 'The agent did not complete this read operation.'
            job.completed_at = now
        job.save()
    device.jobs.filter(status__in=['queued', 'awaiting_approval'], expires_at__lte=now).update(
        status='cancelled', completed_at=now, error='Job expired before execution.')
    device.jobs.filter(status='rebooting', started_at__lt=now-timedelta(hours=24)).update(
        status='uncertain', error='Reboot acknowledgement was not received; inspect this device before requesting new work.')


def expire_jobs():
    """Expire abandoned work even when a device never polls Core again."""
    from django.db.models import Q
    now = timezone.now()
    ids = list(EdgeJob.objects.filter(
        Q(status__in=['claimed', 'running'], lease_expires_at__lte=now) |
        Q(status__in=['queued', 'awaiting_approval'], expires_at__lte=now) |
        Q(status='rebooting', started_at__lt=now-timedelta(hours=24))
    ).values_list('device_id', flat=True).distinct()[:100])
    for device_id in ids:
        with transaction.atomic():
            device = EdgeDevice.objects.select_for_update().get(pk=device_id)
            _expire(device)
    return len(ids)


def _delivery(job, raw):
    arguments = dict(job.arguments)
    if job.kind == 'app.deploy':
        release = EdgeRelease.objects.get(pk=arguments['release_id'], installation=job.device.installation)
        arguments = {'artifact': {'release_id': str(release.pk), 'sha256': release.sha256,
            'size_bytes': release.size_bytes, 'version': release.version,
            'url': settings.ISSUER + '/api/edge/v1/agent/artifacts/' + str(release.pk) + '/'}}
    return {'id': str(job.pk), 'kind': job.kind, 'arguments': arguments, 'lease_token': raw,
            'expires_at': job.lease_expires_at.isoformat()}


@transaction.atomic
def claim_job(credential):
    device = EdgeDevice.objects.select_for_update().get(pk=credential.device_id)
    credential = current_credential(credential)
    _expire(device)
    if device.jobs.filter(status__in=['claimed', 'running', 'rebooting']).exists():
        return None
    for job in device.jobs.select_for_update().filter(status='queued').order_by('created_at'):
        if not live_job_authority(job):
            job.status, job.error, job.completed_at = 'cancelled', 'Current permissions no longer authorize this job.', timezone.now()
            job.save()
            continue
        try:
            validated_arguments(device, job.kind, job.arguments)
        except ValidationError:
            job.status, job.error = 'cancelled', 'The approved plan is no longer available.'
            job.save()
            continue
        raw = 'lease_' + secrets.token_urlsafe(32)
        job.lease_hash, job.credential, job.lease_expires_at = secret_digest(raw), credential, timezone.now()+timedelta(seconds=LEASE_SECONDS)
        job.status, job.attempts = 'claimed', job.attempts + 1
        job.save()
        audit(device.installation, 'edge.job_claimed', device=device, target=job.pk)
        return _delivery(job, raw)
    return None


def lease_matches(job, credential, token):
    return bool(isinstance(token, str) and 20 <= len(token) <= 256 and job.credential_id == credential.pk and
                job.device_id == credential.device_id and hmac.compare_digest(job.lease_hash, secret_digest(token)))


@transaction.atomic
def start_job(job, credential, token):
    EdgeDevice.objects.select_for_update().get(pk=credential.device_id)
    credential = current_credential(credential)
    job = EdgeJob.objects.select_for_update().select_related('device__installation').get(pk=job.pk)
    if not lease_matches(job, credential, token):
        raise PermissionDenied
    if (job.status not in {'claimed', 'running'} or job.cancel_requested or job.expires_at <= timezone.now() or
            not job.lease_expires_at or job.lease_expires_at <= timezone.now()):
        raise Conflict('The job lease is no longer executable.')
    if not live_job_authority(job):
        raise PermissionDenied
    validated_arguments(job.device, job.kind, job.arguments)
    if job.status == 'claimed':
        job.status, job.started_at = 'running', timezone.now()
        job.save()
        audit(job.device.installation, 'edge.job_started', device=job.device, target=job.pk)
    return job


@transaction.atomic
def heartbeat(credential, agent_version, report, job_id=None, token=None):
    safe_document(report, limit=131072)
    if not isinstance(agent_version, str) or len(agent_version) > 64:
        raise ValidationError('Invalid agent version.')
    if bool(job_id) != bool(token):
        raise ValidationError('Supply both job_id and lease_token for an active job.')
    device = EdgeDevice.objects.select_for_update().get(pk=credential.device_id)
    credential = current_credential(credential)
    device.agent_version, device.report, device.last_seen_at = agent_version, report, timezone.now()
    device.save(update_fields=['agent_version', 'report', 'last_seen_at'])
    if job_id:
        job = device.jobs.select_for_update().filter(pk=job_id).first()
        if not job or not lease_matches(job, credential, token):
            raise PermissionDenied
        if job.status in {'claimed', 'running'} and job.lease_expires_at > timezone.now() and job.expires_at > timezone.now():
            job.lease_expires_at = min(timezone.now()+timedelta(seconds=LEASE_SECONDS), job.expires_at)
            job.save(update_fields=['lease_expires_at'])
    return [str(value) for value in device.jobs.filter(cancel_requested=True, status__in=['claimed', 'running', 'rebooting']).values_list('pk', flat=True)]


@transaction.atomic
def complete_job(job, credential, token, status, result):
    safe_document(result)
    if status not in {'succeeded', 'failed', 'rebooting'}:
        raise ValidationError('Invalid job result status.')
    EdgeDevice.objects.select_for_update().get(pk=credential.device_id)
    credential = current_credential(credential)
    job = EdgeJob.objects.select_for_update().select_related('device__installation').get(pk=job.pk)
    if not lease_matches(job, credential, token):
        raise PermissionDenied
    digest = canonical_digest({'status': status, 'result': result})
    if job.status in {'succeeded', 'failed'}:
        if job.result_digest != digest:
            raise Conflict('A different result was already recorded.')
        return job
    if job.status == 'rebooting' and status == 'rebooting':
        if job.result_digest != digest:
            raise Conflict('A different reboot acknowledgement was already recorded.')
        return job
    # This authorizes acknowledgement only, never a second execution. A device
    # with a durable completion journal can recover a lost result response.
    recovery = job.kind in MUTATIONS and job.status in {'running', 'rebooting', 'uncertain'} and job.started_at and job.started_at > timezone.now()-timedelta(hours=24)
    if (not job.started_at or (job.status != 'running' and not recovery) or
            not recovery and (not job.lease_expires_at or job.lease_expires_at <= timezone.now())):
        raise Conflict('This result cannot complete the job in its current state.')
    if status == 'rebooting' and job.kind != 'os.reboot':
        raise ValidationError('Only a reboot job can report rebooting.')
    job.status, job.result, job.result_digest = status, result, digest
    if status != 'rebooting':
        job.completed_at = timezone.now()
    job.error = 'The agent reported a failed operation.' if status == 'failed' else ''
    job.save()
    if status == 'succeeded' and job.kind == 'app.deploy':
        device = EdgeDevice.objects.select_for_update().get(pk=job.device_id)
        if str(device.current_release_id) != job.arguments['release_id']:
            device.previous_release_id, device.current_release_id = device.current_release_id, job.arguments['release_id']
            device.save(update_fields=['previous_release', 'current_release'])
    elif status == 'succeeded' and job.kind == 'app.rollback':
        device = EdgeDevice.objects.select_for_update().get(pk=job.device_id)
        device.current_release_id, device.previous_release_id = device.previous_release_id, device.current_release_id
        device.save(update_fields=['current_release', 'previous_release'])
    audit(job.device.installation, 'edge.job_' + status, device=job.device, target=job.pk)
    return job


def authorized_artifact(credential, release):
    credential = current_credential(credential)
    if release.installation_id != credential.device.installation_id:
        raise PermissionDenied
    matching = credential.device.jobs.filter(kind='app.deploy', credential=credential, status__in=['claimed', 'running'],
        lease_expires_at__gt=timezone.now(), cancel_requested=False, arguments__release_id=str(release.pk))
    if not any(live_job_authority(job) for job in matching):
        raise PermissionDenied
    if not settings.CORE_EDGE_ARTIFACT_ROOT:
        raise ValidationError('The operator has not configured persistent artifact storage.')
    path = Path(release.artifact_path).resolve()
    root = Path(settings.CORE_EDGE_ARTIFACT_ROOT).resolve()
    if path.parent != root or not path.is_file() or path.is_symlink():
        raise ValidationError('The approved artifact is unavailable.')
    if path.stat().st_size != release.size_bytes or hashlib.sha256(path.read_bytes()).hexdigest() != release.sha256:
        raise ValidationError('The approved artifact failed integrity verification.')
    return path
