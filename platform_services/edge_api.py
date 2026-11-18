"""Native OAuth and outbound device APIs for NCE Robotics."""
from functools import wraps
import hashlib
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from oauth2_provider.models import get_access_token_model

from identity.access import Principal, evidence_for
from identity.views import token_claims
from . import edge
from .api import bearer, body, response
from .edge_models import EdgeDevice, EdgeJob, EdgeRelease
from .models import Installation


def secure_transport(request):
    hostname = urlsplit('//' + request.get_host()).hostname
    return request.is_secure() or bool(settings.DEBUG and getattr(settings, 'CORE_EDGE_ALLOW_INSECURE_LOCAL', False)
        and hostname in {'localhost', '127.0.0.1', '::1', 'testserver'})


def api_errors(function):
    @wraps(function)
    def wrapped(request, *args, **kwargs):
        try:
            if not secure_transport(request):
                return response({'error': 'https_required'}, 400)
            return function(request, *args, **kwargs)
        except Http404:
            return response({'error': 'not_found'}, 404)
        except PermissionDenied:
            return response({'error': 'insufficient_permission'}, 403)
        except edge.Conflict as error:
            return response({'error': 'conflict', 'detail': str(error)}, 409)
        except ValidationError as error:
            return response({'error': 'invalid_request', 'detail': error.messages[0]}, 400)
        except (ValueError, TypeError, OverflowError):
            return response({'error': 'invalid_request'}, 400)
    return wrapped


def human_api(methods=('GET',)):
    def decorate(function):
        @csrf_exempt
        @require_http_methods(methods)
        @api_errors
        @wraps(function)
        def wrapped(request, *args, **kwargs):
            raw = bearer(request)
            token = get_access_token_model().objects.select_related('application__service__organization', 'user').filter(
                token_checksum=hashlib.sha256(raw.encode()).hexdigest()).first() if raw else None
            purpose = request.headers.get('X-Core-Purpose', '')
            if (not token or not token.application or 'authorization' not in token.scopes or
                    not (token_claims(raw, token.application, purpose) or {}).get('active')):
                return response({'error': 'invalid_credential'}, 401)
            installation = Installation.objects.select_related('service', 'organization', 'package').filter(
                service=token.application.service, package__slug=edge.PACKAGE).first()
            if not installation:
                return response({'error': 'insufficient_permission'}, 403)
            request.edge_installation = installation
            request.access_principal = Principal(user=token.user, evidence=evidence_for(raw), application=token.application,
                purpose=purpose, request_id=getattr(request, 'security_request_id', None))
            request.edge_permissions = edge.authorize(request.access_principal, installation)
            return function(request, *args, **kwargs)
        return wrapped
    return decorate


def agent_api(methods=('POST',)):
    def decorate(function):
        @csrf_exempt
        @require_http_methods(methods)
        @api_errors
        @wraps(function)
        def wrapped(request, *args, **kwargs):
            try:
                request.edge_credential = edge.authenticate_device(bearer(request))
            except PermissionDenied:
                return response({'error': 'invalid_credential'}, 401)
            return function(request, *args, **kwargs)
        return wrapped
    return decorate


def fields(values, allowed, required=()):
    if set(values) - set(allowed) or set(required) - set(values):
        raise ValidationError('Unexpected or missing request fields.')
    return values


def device_for(request, pk):
    return get_object_or_404(EdgeDevice.objects.select_related('installation__service', 'installation__organization',
        'installation__package'), pk=pk, installation=request.edge_installation)


def job_for(request, pk):
    return get_object_or_404(EdgeJob.objects.select_related('device__installation'), pk=pk,
        device__installation=request.edge_installation)


@human_api()
def context(request):
    installation = request.edge_installation
    permissions = edge.authorize(request.access_principal, installation)
    return response({'api_version': 1, 'organization_id': str(installation.organization_id),
        'service_id': str(installation.service_id), 'installation_id': str(installation.pk),
        'permissions': sorted(permissions)})


@human_api()
def devices(request):
    rows = []
    for device in EdgeDevice.objects.filter(installation=request.edge_installation).order_by('created_at')[:200]:
        try:
            edge.authorize(request.access_principal, request.edge_installation, resource=device)
        except PermissionDenied:
            continue
        rows.append(edge.device_values(device, request.edge_permissions))
    return response({'devices': rows})


@human_api()
def device(request, pk):
    value = device_for(request, pk)
    edge.authorize(request.access_principal, request.edge_installation, resource=value)
    return response({'device': edge.device_values(value, request.edge_permissions)})


@human_api(('POST',))
def control_ticket(request, pk):
    from .edge_control import issue_ticket
    values = fields(body(request), {'session_id'}, {'session_id'})
    return response(issue_ticket(device_for(request, pk), request.access_principal, values['session_id']))


@agent_api()
def control_trust(request):
    from .edge_control import trust_configuration
    fields(body(request), set())
    return response(trust_configuration(request.edge_credential.device))


@human_api(('POST',))
def enrollments(request):
    values = fields(body(request), {'name'}, {'name'})
    device, enrollment, code = edge.create_enrollment(request.edge_installation, request.access_principal, values['name'])
    return response({'device': edge.device_values(device), 'code': code, 'expires_at': enrollment.expires_at.isoformat()}, 201)


@human_api(('POST',))
def revoke_device(request, pk):
    fields(body(request), set())
    edge.revoke_device(device_for(request, pk), request.access_principal)
    return response({'revoked': True})


@human_api(('GET', 'POST'))
def releases(request):
    if request.method == 'GET':
        rows = []
        for release in EdgeRelease.objects.filter(installation=request.edge_installation).order_by('-created_at')[:100]:
            try:
                edge.authorize(request.access_principal, request.edge_installation, resource=release)
            except PermissionDenied:
                continue
            rows.append(edge.release_values(release))
        return response({'releases': rows})
    edge.authorize(request.access_principal, request.edge_installation, 'release-manage')
    if set(request.POST) or set(request.FILES) != {'artifact'}:
        raise ValidationError('Upload a single multipart file named artifact.')
    upload = request.FILES['artifact']
    if upload.size > edge.MAX_ARTIFACT:
        raise ValidationError('The release exceeds 16 MiB.')
    value, created = edge.upload_release(request.edge_installation, request.access_principal, upload.read(edge.MAX_ARTIFACT + 1))
    return response({'release': edge.release_values(value)}, 201 if created else 200)


@human_api(('GET', 'POST'))
def jobs(request, pk):
    device = device_for(request, pk)
    edge.authorize(request.access_principal, request.edge_installation, resource=device)
    if request.method == 'GET':
        rows = []
        for row in device.jobs.order_by('-created_at')[:100]:
            try:
                edge.authorize(request.access_principal, request.edge_installation, resource=row)
            except PermissionDenied:
                continue
            rows.append(edge.job_values(row, request.edge_permissions))
        return response({'jobs': rows})
    values = fields(body(request), {'kind', 'arguments', 'request_id'}, {'kind', 'arguments', 'request_id'})
    job, created = edge.request_job(device, request.access_principal, values['kind'], values['arguments'], values['request_id'])
    return response({'job': edge.job_values(job)}, 201 if created else 200)


@human_api(('POST',))
def approve_job(request, pk):
    fields(body(request), set())
    return response({'job': edge.job_values(edge.approve_job(job_for(request, pk), request.access_principal))})


@human_api(('POST',))
def cancel_job(request, pk):
    fields(body(request), set())
    return response({'job': edge.job_values(edge.cancel_job(job_for(request, pk), request.access_principal))})


@csrf_exempt
@require_http_methods(('POST',))
@api_errors
def enroll_agent(request):
    values = fields(body(request), {'code', 'name', 'agent_version', 'report'}, {'code'})
    try:
        device, credential, raw = edge.enroll(values['code'], values.get('name', ''), values.get('agent_version', ''), values.get('report', {}))
    except PermissionDenied:
        return response({'error': 'invalid_enrollment'}, 401)
    return response({'device_id': str(device.pk), 'token': raw, 'expires_at': credential.expires_at.isoformat(),
        'poll_interval_seconds': 10}, 201)


@agent_api()
def heartbeat(request):
    values = fields(body(request), {'agent_version', 'report', 'job_id', 'lease_token'}, {'agent_version', 'report'})
    cancelled = edge.heartbeat(request.edge_credential, values['agent_version'], values['report'], values.get('job_id'), values.get('lease_token'))
    return response({'cancel_job_ids': cancelled, 'poll_interval_seconds': 10})


@agent_api()
def claim(request):
    fields(body(request), set())
    return response({'job': edge.claim_job(request.edge_credential)})


def agent_job(request, pk):
    return get_object_or_404(EdgeJob.objects.select_related('device__installation'), pk=pk,
        device=request.edge_credential.device)


@agent_api()
def start(request, pk):
    values = fields(body(request), {'lease_token'}, {'lease_token'})
    job = edge.start_job(agent_job(request, pk), request.edge_credential, values['lease_token'])
    return response({'job': edge.job_values(job)})


@agent_api()
def result(request, pk):
    values = fields(body(request), {'lease_token', 'status', 'result'}, {'lease_token', 'status', 'result'})
    job = edge.complete_job(agent_job(request, pk), request.edge_credential,
        values['lease_token'], values['status'], values['result'])
    return response({'job': edge.job_values(job)})


@agent_api(('GET',))
def artifact(request, pk):
    release = get_object_or_404(EdgeRelease, pk=pk, installation=request.edge_credential.device.installation)
    path = edge.authorized_artifact(request.edge_credential, release)
    edge.audit(release.installation, 'edge.artifact_read', device=request.edge_credential.device, target=release.pk)
    result = FileResponse(path.open('rb'), content_type='application/gzip')
    result['Cache-Control'] = 'no-store'
    result['Content-Length'] = str(release.size_bytes)
    result['X-Content-SHA256'] = release.sha256
    result['Content-Disposition'] = 'attachment; filename="nce-robotics-release.tar.gz"'
    return result
