"""CSRF-protected Core administration for enrolled robotics devices."""
from functools import wraps
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from identity.access import request_principal, require_access
from identity.models import Membership, Organization
from . import edge
from .edge_models import EdgeDevice, EdgeJob, EdgeRelease
from .models import Installation

OPERATION_LABELS = {
    'app.command': 'Robot command',
    'status.refresh': 'Refresh device status', 'app.deploy': 'Deploy software',
    'app.rollback': 'Restore previous software', 'os.update.check': 'Check for OS updates',
    'os.update.apply': 'Apply reviewed OS updates', 'os.reboot': 'Restart device',
}


def review_details(job, principal, installation):
    """Show the immutable target, subject to the same resource permissions."""
    job.operation_label = OPERATION_LABELS.get(job.kind, job.kind)
    job.review_release = None
    job.review_plan = None
    if job.kind == 'app.deploy':
        release = EdgeRelease.objects.filter(installation=installation, pk=job.arguments.get('release_id')).first()
        if release:
            try:
                edge.authorize(principal, installation, resource=release)
                job.review_release = release
            except PermissionDenied:
                pass
    if job.kind == 'os.update.apply':
        for check in job.device.jobs.filter(kind='os.update.check', status='succeeded').order_by('-completed_at')[:100]:
            plan = check.result.get('plan', check.result)
            if plan.get('plan_id') == job.arguments.get('plan_id') and plan.get('sha256') == job.arguments.get('sha256'):
                try:
                    edge.authorize(principal, installation, resource=check)
                    job.review_plan = plan
                except PermissionDenied:
                    pass
                break


def portal(permission='device-read'):
    def decorate(function):
        @login_required
        @never_cache
        @wraps(function)
        def wrapped(request, slug, *args, **kwargs):
            organization = get_object_or_404(Organization, slug=slug, active=True)
            request.organization = organization
            installation = edge.installation_for_organization(organization)
            principal = request_principal(request)
            edge.authorize(principal, installation, permission)
            try:
                return function(request, organization, installation, principal, *args, **kwargs)
            except (ValidationError, edge.Conflict) as error:
                messages.error(request, error.messages[0] if isinstance(error, ValidationError) else str(error))
                return redirect('edge_dashboard', slug=slug)
        return wrapped
    return decorate


@login_required
@never_cache
def index(request, slug):
    organization = get_object_or_404(Organization, slug=slug, active=True)
    request.organization = organization
    principal = request_principal(request)
    installation = Installation.objects.select_related('service', 'package', 'organization').filter(
        organization=organization, package__slug=edge.PACKAGE).first()
    if not installation:
        require_access(principal, organization, 'edge.registration_view')
        if not (request.user.is_superuser or organization.memberships.filter(user=request.user, active=True, kind='administrator').exists()):
            raise PermissionDenied
        return render(request, 'platform_services/edge_dashboard.html', {'organization': organization})
    permissions = edge.authorize(principal, installation)
    devices = []
    for device in EdgeDevice.objects.filter(installation=installation).order_by('created_at')[:200]:
        try:
            edge.authorize(principal, installation, resource=device)
        except PermissionDenied:
            continue
        device.can_manage = 'admin' in permissions or 'device-manage' in permissions
        device.operations = [(kind, OPERATION_LABELS[kind]) for kind, permission in edge.OPERATIONS.items()
                             if kind != 'app.command' and ('admin' in permissions or permission in permissions)]
        device.recent_jobs = []
        for job in device.jobs.order_by('-created_at')[:20]:
            try:
                edge.authorize(principal, installation, resource=job)
            except PermissionDenied:
                continue
            job.can_approve = (job.status == 'awaiting_approval' and ('admin' in permissions or
                'job-approve' in permissions and edge.OPERATIONS[job.kind] in permissions))
            job.can_cancel = job.status not in edge.TERMINAL and ('admin' in permissions or edge.OPERATIONS[job.kind] in permissions)
            review_details(job, principal, installation)
            device.recent_jobs.append(job)
        devices.append(device)
    releases = []
    for release in EdgeRelease.objects.filter(installation=installation).order_by('-created_at')[:100]:
        try:
            edge.authorize(principal, installation, resource=release)
        except PermissionDenied:
            continue
        releases.append(release)
    native = installation.service.applications.filter(name='NCE Robotics iOS', client_type='public', active=True).first()
    development_native = installation.service.applications.filter(native_development=True, client_type='public', active=True).first()
    can_register_development = request.user.is_superuser or organization.memberships.filter(
        user=request.user, active=True, kind='administrator').exists()
    return render(request, 'platform_services/edge_dashboard.html', {'organization': organization,
        'installation': installation, 'devices': devices, 'releases': releases, 'native': native,
        'development_native': development_native, 'can_register_development': can_register_development,
        'can_enroll': 'admin' in permissions or 'device-manage' in permissions,
        'can_release': 'admin' in permissions or 'release-manage' in permissions})


@portal()
def console(request, organization, installation, principal, pk):
    device = get_object_or_404(EdgeDevice, installation=installation, pk=pk)
    permissions = edge.authorize(principal, installation, resource=device)
    jobs = []
    for job in device.jobs.filter(kind='app.command').order_by('-created_at')[:30]:
        try:
            edge.authorize(principal, installation, resource=job)
        except PermissionDenied:
            continue
        job.console_output = job.result.get('output', '')
        jobs.append(job)
    can_command = 'app.command' in edge.device_values(device, permissions).get('allowed_actions', [])
    return render(request, 'platform_services/edge_console.html', {'organization': organization,
        'device': device, 'jobs': jobs, 'can_command': can_command, 'request_id': str(uuid.uuid4()),
        'has_pending': any(job.status not in edge.TERMINAL for job in jobs)})


@login_required
@never_cache
@require_POST
def register(request, slug):
    organization = get_object_or_404(Organization, slug=slug, active=True)
    request.organization = organization
    principal = request_principal(request)
    require_access(principal, organization, 'edge.application_register')
    if not (request.user.is_superuser or organization.memberships.filter(user=request.user, active=True, kind='administrator').exists()):
        raise PermissionDenied
    try:
        edge.register_application(organization, actor=request.user)
    except ValidationError as error:
        return HttpResponseBadRequest(error.messages[0])
    return redirect('edge_dashboard', slug=slug)


@login_required
@never_cache
@require_POST
def register_development(request, slug):
    organization = get_object_or_404(Organization, slug=slug, active=True)
    request.organization = organization
    principal = request_principal(request)
    require_access(principal, organization, 'edge.development_registration')
    if not (request.user.is_superuser or organization.memberships.filter(user=request.user, active=True, kind='administrator').exists()):
        raise PermissionDenied
    installation = edge.installation_for_organization(organization)
    edge.authorize(principal, installation, 'device-manage')
    try:
        edge.register_development_client(organization, request.user)
    except ValidationError as error:
        return HttpResponseBadRequest(error.messages[0])
    messages.success(request, 'The separate Personal Team development client is registered. Use its development client ID in the local-testing build.')
    return redirect('edge_dashboard', slug=slug)


@require_POST
@portal('device-manage')
def enroll(request, organization, installation, principal):
    device, enrollment, code = edge.create_enrollment(installation, principal, request.POST.get('name', ''))
    return render(request, 'platform_services/edge_enrollment.html', {'organization': organization,
        'device': device, 'code': code, 'expires_at': enrollment.expires_at})


@require_POST
@portal('device-manage')
def revoke(request, organization, installation, principal, pk):
    device = get_object_or_404(EdgeDevice, installation=installation, pk=pk)
    edge.revoke_device(device, principal)
    messages.success(request, 'Device access revoked. Existing local software remains installed.')
    return redirect('edge_dashboard', slug=organization.slug)


@require_POST
@portal('release-manage')
def upload(request, organization, installation, principal):
    artifact = request.FILES.get('artifact')
    if not artifact or artifact.size > edge.MAX_ARTIFACT:
        raise ValidationError('Choose a tar.gz application release no larger than 16 MiB.')
    release, created = edge.upload_release(installation, principal, artifact.read(edge.MAX_ARTIFACT+1))
    messages.success(request, 'Release uploaded. Devices change only after a deployment is approved.' if created else 'This release is already registered.')
    return redirect('edge_dashboard', slug=organization.slug)


@require_POST
@portal()
def request_job(request, organization, installation, principal, pk):
    device = get_object_or_404(EdgeDevice, installation=installation, pk=pk)
    kind = request.POST.get('kind', '')
    arguments = {}
    if kind == 'app.deploy':
        arguments = {'release_id': request.POST.get('release_id', '')}
    elif kind == 'app.command':
        arguments = {'command': request.POST.get('command', '').strip()}
    elif kind == 'os.update.apply':
        check = device.jobs.filter(kind='os.update.check', status='succeeded').order_by('-completed_at').first()
        plan = check.result.get('plan', check.result) if check else {}
        arguments = {key: plan.get(key, '') for key in ['plan_id', 'sha256']}
    job, _ = edge.request_job(device, principal, kind, arguments, request.POST.get('request_id') or str(uuid.uuid4()))
    if kind == 'app.command':
        return redirect('edge_console', slug=organization.slug, pk=device.pk)
    messages.success(request, 'Review and approve the requested change below.' if job.status == 'awaiting_approval' else 'Read-only job queued.')
    return redirect('edge_dashboard', slug=organization.slug)


@require_POST
@portal()
def approve(request, organization, installation, principal, pk):
    job = get_object_or_404(EdgeJob, device__installation=installation, pk=pk)
    edge.approve_job(job, principal)
    messages.success(request, 'Job approved. The device will recheck permission before starting.')
    return redirect('edge_dashboard', slug=organization.slug)


@require_POST
@portal()
def cancel(request, organization, installation, principal, pk):
    job = get_object_or_404(EdgeJob, device__installation=installation, pk=pk)
    edge.cancel_job(job, principal)
    messages.success(request, 'Cancellation requested. An operation already running may need to finish safely.')
    return redirect('edge_dashboard', slug=organization.slug)
