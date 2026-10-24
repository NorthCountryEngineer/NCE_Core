"""Human approval, immutable plans, and restartable cloud deployment orchestration."""
import copy
import json
import hashlib
import logging
import traceback
import uuid
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.access import Principal, require_access, resource_organization
from identity.models import AuditEvent, Membership, User
from .cloud_models import CloudApplication, CloudPlatform, CloudRelease, digest, hosted_assignment
from platform_services.github_ci import CIGateError

logger = logging.getLogger(__name__)


def authorized(principal, resource, action='cloud.manage'):
    organization = resource_organization(resource)
    user = principal.user
    if (not user or not user.is_authenticated or not user.is_active or principal.credential or principal.runtime or
            principal.agent_binding or principal.agent_credential or not (user.is_superuser or
            Membership.objects.filter(organization=organization, user=user, active=True, kind='administrator').exists())):
        raise PermissionDenied('An active human organization administrator is required.')
    require_access(principal, resource, action)
    if isinstance(resource, CloudRelease):
        authorized(principal, resource.application, action)
    if isinstance(resource, CloudApplication):
        assignment = hosted_assignment(resource)
        if resource.organization_id != resource.platform.organization_id and not assignment:
            raise PermissionDenied('The operator hosting assignment is unavailable.')
        if not assignment or action != 'cloud.console_access':
            if assignment and not (user.is_superuser or Membership.objects.filter(
                    organization=resource.platform.organization, user=user, active=True, kind='administrator').exists()):
                raise PermissionDenied('The hosting provider must authorize deployment changes.')
            require_access(principal, resource.platform, action)
            if assignment:
                require_access(principal, assignment.provider_environment, action)
        require_access(principal, resource.environment, action)
    return resource


def can_manage_hosting(principal, app):
    try:
        authorized(principal, app, 'cloud.application_configure')
        return True
    except PermissionDenied:
        return False


def _audit(resource, user, action):
    AuditEvent.objects.create(organization=resource_organization(resource), actor=user, action=action, target=str(resource.pk))


def _ready(platform):
    if not platform.active or not platform.verified_at or platform.verified_digest != platform.fingerprint():
        raise ValidationError('The deployment platform must be verified again.')
    platform.full_clean()


def resolve_source(platform, repository, branch, commit=''):
    if repository not in platform.allowed_repositories:
        raise ValidationError('Choose an approved repository.')
    if commit:
        return commit.lower()
    if platform.mode == 'demo':
        return 'd' * 40
    # Exact allowlisted repository and a fixed HTTPS host; redirects and
    # credentials are prohibited. CodeConnections remains the build transport.
    import requests
    try:
        response = requests.get(f'https://api.github.com/repos/{repository}/commits/{quote(branch, safe="")}',
            headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
            timeout=(5, 10), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError
        value = response.json()['sha']
        import re
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}', value):
            raise ValueError
        return value
    except Exception:
        raise ValidationError('The public branch could not be resolved. Check the branch or supply its exact 40-character commit SHA; private repositories require a SHA.') from None


def verify_platform(platform, principal):
    platform = CloudPlatform.objects.get(pk=platform.pk)
    authorized(principal, platform, 'cloud.platform_verify')
    # A failed re-verification must not retain the previous readiness stamp.
    CloudPlatform.objects.filter(pk=platform.pk).update(active=False, verified_at=None, verified_digest='')
    platform.active, platform.verified_at, platform.verified_digest = False, None, ''
    platform.full_clean()
    reviewed = platform.fingerprint()
    if platform.mode == 'aws':
        from .cloud_aws import AWSAdapterError, verify
        try:
            verify(platform)
        except AWSAdapterError as error:
            raise ValidationError(str(error)) from None
    with transaction.atomic():
        current = CloudPlatform.objects.select_for_update().get(pk=platform.pk)
        authorized(principal, current, 'cloud.platform_verify')
        if current.fingerprint() != reviewed:
            raise ValidationError('Platform settings changed during verification. Verify the new settings again.')
        current.verified_digest = reviewed
        current.verified_at, current.active = timezone.now(), True
        current.save(update_fields=['verified_digest', 'verified_at', 'active', 'updated'])
        platform = current
    _audit(platform, principal.user, 'cloud.platform_verified')
    return platform


@transaction.atomic
def create_application(organization, principal, **fields):
    authorized(principal, organization, 'cloud.application_create')
    app = CloudApplication(organization=organization, **fields)
    authorized(principal, app, 'cloud.application_create')
    _ready(app.platform)
    app.commit = resolve_source(app.platform, app.repository, app.branch, app.commit)
    app.full_clean()
    app.save()
    _audit(app, principal.user, 'cloud.application_created')
    return app


@transaction.atomic
def update_application(app, principal, **fields):
    app = CloudApplication.objects.select_for_update().get(pk=app.pk)
    authorized(principal, app, 'cloud.application_update')
    if app.releases.filter(status__in=['queued', 'running']).exists():
        raise ValidationError('Wait for the current release to finish or cancel it first.')
    for key, value in fields.items():
        if key not in {'name', 'slug', 'platform', 'environment', 'repository', 'branch', 'commit', 'archetype', 'public_origin'}:
            raise ValidationError('Unsupported application setting.')
        setattr(app, key, value)
    _ready(app.platform)
    app.commit = resolve_source(app.platform, app.repository, app.branch, app.commit)
    app.full_clean()
    app.save()
    _audit(app, principal.user, 'cloud.application_updated')
    return app


def _snapshot(app):
    result = {'version': 1, 'app_identifier': str(app.pk), 'app_name': app.name, 'organization_id': str(app.organization_id),
        'environment': app.environment.name, 'environment_id': str(app.environment_id), 'platform_id': str(app.platform_id),
        'platform_digest': app.platform.fingerprint(), 'platform_config': copy.deepcopy(app.platform.config),
        'repository': app.repository, 'branch': app.branch, 'commit': app.commit, 'archetype': app.archetype,
        'public_origin': app.public_origin, 'secret_parameter': f'/{app.platform.prefix}/apps/{app.pk}/core-resource-client'}
    assignment = hosted_assignment(app)
    if assignment:
        result['hosting_assignment_digest'] = digest(assignment.descriptor())
    return result


def _require_ci(app, snapshot):
    if app.platform.mode != 'aws':
        return []
    from .cloud_aws import require_ci
    try:
        return require_ci(app.platform, snapshot)
    except CIGateError as error:
        raise ValidationError(str(error), code='ci_checks_required') from None


@transaction.atomic
def prepare_release(app, principal):
    app = CloudApplication.objects.select_for_update().select_related('platform', 'environment', 'organization').get(pk=app.pk)
    authorized(principal, app, 'cloud.release_prepare')
    _ready(app.platform)
    app.full_clean()
    if not app.active or not app.environment.active:
        raise ValidationError('The application and environment must be active.')
    snapshot = _snapshot(app)
    _require_ci(app, snapshot)
    release = CloudRelease.objects.create(application=app, requested_by=principal.user, evidence=principal.evidence,
        purpose=principal.purpose, snapshot=snapshot, digest=digest(snapshot), steps=[
            {'label': 'Create or update AWS hosting', 'status': 'pending', 'description':
                'Manual Amplify app and branch.' + (' Python Lambda, HTTP API, two on-demand DynamoDB tables, and a private media bucket.' if app.archetype == 'amplify-serverless' else
                ' Game Studio Python Lambda, HTTP API, five retained on-demand DynamoDB tables, private media and exact-origin public game embeds.' if app.archetype == 'amplify-game-studio' else
                ' Academy API and private PostgreSQL; use an approved immutable image and stop services for controlled migration.' if app.archetype == 'amplify-academy-fargate' else (' Fargate API, database-backed worker, isolated browser service, private PostgreSQL and document storage; use approved immutable images and stop services for controlled migration.' if app.archetype == 'amplify-fargate' else ''))},
            {'label': 'Register the application with Core', 'status': 'pending', 'description': 'Central user identity, app-branded sign-in, and explicit app roles. API credentials go to SSM SecureString.'},
            {'label': 'Build the reviewed commit', 'status': 'pending', 'description': 'Run the frontend build in a separate project with no deployment permissions.'},
            {'label': 'Publish and confirm hosting', 'status': 'pending', 'description': 'Deploy the compiled ZIP and wait for Amplify publication. Retain application data.'}])
    _audit(release, principal.user, 'cloud.release_prepared')
    return release


def _check(release, principal):
    app = release.application
    authorized(principal, release, 'cloud.release_execute')
    _ready(app.platform)
    app.full_clean()
    if not app.active or not app.environment.active or digest(release.snapshot) != release.digest or _snapshot(app) != release.snapshot:
        raise ValidationError('The application or reviewed plan changed. Prepare a new release.')


@transaction.atomic
def approve_release(release, principal, confirm_costs=False):
    release = CloudRelease.objects.select_for_update().select_related('application__platform', 'application__environment').get(pk=release.pk)
    # Lock placement as well so two reviewers cannot queue simultaneous releases.
    CloudApplication.objects.select_for_update().get(pk=release.application_id)
    _check(release, principal)
    if release.status != 'draft' or release.application.releases.exclude(pk=release.pk).filter(status__in=['queued', 'running']).exists():
        raise ValidationError('This release cannot be queued in its current state.')
    if release.application.platform.mode == 'aws' and (not confirm_costs or not settings.CORE_ENABLE_CLOUD_DEPLOYMENTS):
        raise ValidationError('Live deployment requires operator enablement and explicit authorization of charges.')
    _require_ci(release.application, release.snapshot)
    release.status, release.approved_at, release.requested_by = 'queued', timezone.now(), principal.user
    release.evidence, release.purpose = principal.evidence, principal.purpose
    release.save()
    _audit(release, principal.user, 'cloud.release_approved')
    return release


@transaction.atomic
def cancel_release(release, principal):
    release = CloudRelease.objects.select_for_update().get(pk=release.pk)
    authorized(principal, release, 'cloud.release_cancel')
    if release.status not in ['draft', 'queued', 'running']:
        raise ValidationError('This release has already finished.')
    # Polling is harmless, but do not race cancellation against an active provider mutation.
    if release.lease_until and release.lease_until > timezone.now():
        raise ValidationError('A deployment step is in progress. Retry cancellation shortly.')
    if release.provider_state.get('build_id') and release.application.platform.mode == 'aws':
        from .cloud_aws import AWSAdapterError, stop_run
        try:
            working = copy.deepcopy(release.snapshot)
            working.update(action=release.provider_state.get('phase', 'provision'), public_config=release.provider_state.get('public_config', {}))
            stop_run(release.application.platform, working, release.provider_state)
        except AWSAdapterError:
            raise ValidationError('The runner could not be stopped. Retry shortly.') from None
    release.status, release.lease_until, release.lease_owner = 'cancelled', None, None
    release.save()
    _audit(release, principal.user, 'cloud.release_cancelled')
    return release


@transaction.atomic
def _register(release, outputs):
    from .models import MarketplacePackage
    from .services import install_package
    from .external_apps import external_public_configuration, external_resource_credentials, register_external_deployment
    app = CloudApplication.objects.select_for_update().get(pk=release.application_id)
    identity = {'callback_path': '/oauth/callback', 'permissions': [
        {'codename': 'app-read', 'name': 'Read application'}, {'codename': 'app-write', 'name': 'Write application'}]}
    if app.archetype == 'amplify-game-studio':
        from .cloud_contract import game_studio_identity
        identity = game_studio_identity()
    elif app.archetype in ('amplify-fargate', 'amplify-academy-fargate'):
        from .cloud_contract import ContractError, identity_contract
        try:
            identity = identity_contract(json.loads(outputs.get('identity', 'null')))
        except (ValueError, ContractError):
            raise ValidationError('The container recipe did not return a valid application identity contract.') from None
    if not app.installation_id:
        manifest = {'external_runtime': 'aws-amplify', 'capabilities': [], **identity}
        package, _ = MarketplacePackage.objects.get_or_create(slug='cloud-' + app.pk.hex,
            defaults={'organization': app.organization, 'name': app.name, 'description': 'Application managed by the Core cloud deployment console.',
                      'kind': 'application', 'version': '1.0.0', 'manifest': manifest})
        app.installation = install_package(app.organization, package, actor=release.requested_by)
        app.save(update_fields=['installation', 'updated'])
        service = app.installation.service
        service.auth_name = app.name
        service.save(update_fields=['auth_name'])
    elif app.archetype in ('amplify-fargate', 'amplify-academy-fargate', 'amplify-game-studio') and any(app.installation.manifest.get(key) != value for key, value in identity.items()):
        raise ValidationError('The reviewed identity contract differs from the installed application. Review an explicit package upgrade first.')
    origin = app.public_origin or outputs.get('origin')
    if not origin:
        raise ValidationError('Hosting did not return an application origin.')
    deployment = register_external_deployment(app.installation, app.environment, origin, release.requested_by)
    public = external_public_configuration(deployment)
    variables = {'VITE_CORE_ISSUER': public['issuer'], 'VITE_CORE_ORGANIZATION_ID': public['organization_id'],
        'VITE_CORE_SERVICE_ID': public['service_id'], 'VITE_CORE_BROWSER_CLIENT_ID': public['browser_client_id'],
        'VITE_APPLICATION_ORIGIN': origin, 'VITE_API_URL': outputs.get('api_url', '')}
    if app.archetype in ('amplify-fargate', 'amplify-academy-fargate'):
        variables.update(VITE_CORE_REDIRECT_URI=public['redirect_uri'], VITE_CORE_DEPLOYMENT_ID=public['deployment_id'])
    if app.archetype == 'amplify-game-studio':
        from .cloud_contract import game_studio_public_configuration
        approved = game_studio_public_configuration(app.platform.config['game_studio_apps'][app.repository])
        variables.update(VITE_APP_MODE='nce', VITE_CORE_REDIRECT_URI=public['redirect_uri'],
            VITE_GAME_STUDIO_CONFIG_JSON=json.dumps(approved, ensure_ascii=False, separators=(',', ':')))
    return variables, external_resource_credentials(deployment), deployment


def process_one():
    """Claim one short step. Provider identifiers survive restarts; credentials never do."""
    now, owner = timezone.now(), uuid.uuid4()
    with transaction.atomic():
        release = CloudRelease.objects.select_for_update().filter(status__in=['queued', 'running']).filter(
            Q(lease_until__isnull=True) | Q(lease_until__lte=now)).filter(Q(next_poll__isnull=True) | Q(next_poll__lte=now)).order_by('created').first()
        if not release:
            return False
        claimed = CloudRelease.objects.filter(pk=release.pk).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).update(
            lease_owner=owner, lease_until=now + timedelta(minutes=4), status='running')
        if not claimed:
            return False
    release = CloudRelease.objects.select_related('requested_by', 'evidence', 'application__platform', 'application__environment', 'application__organization').get(pk=release.pk)
    state = copy.deepcopy(release.provider_state)
    outputs = copy.deepcopy(release.outputs)
    status, error = 'running', ''
    try:
        user = User.objects.get(pk=release.requested_by_id)
        principal = Principal(user=user, evidence=release.evidence, purpose=release.purpose)
        _check(release, principal)
        platform = release.application.platform
        if platform.mode == 'aws' and not settings.CORE_ENABLE_CLOUD_DEPLOYMENTS:
            raise ValidationError('Live deployments are disabled by the operator.')
        phase = state.get('phase', 'provision')
        if phase == 'provision':
            release.steps[0]['status'] = 'running'
        else:
            release.steps[2]['status'] = 'running'
        working = copy.deepcopy(release.snapshot)
        working.update(action=phase, plan_hash=release.digest, public_config=state.get('public_config', {}))
        if platform.mode == 'demo':
            # Simulation never imports the provider SDK, executes repository code, or creates cloud resources.
            if phase == 'provision':
                outputs.update(origin=f'https://{release.application.slug}.example.test', api_url='', commit=working['commit'])
                if working['archetype'] in ('amplify-fargate', 'amplify-academy-fargate'):
                    # Simulation never downloads application source or images.
                    outputs['identity'] = json.dumps({'callback_path': '/api/auth/core/callback', 'permissions': [
                        {'codename': 'app-read', 'name': 'Read application'},
                        {'codename': 'app-write', 'name': 'Write application'},
                        {'codename': 'app-admin', 'name': 'Administer application'}]})
                    if working['archetype'] == 'amplify-academy-fargate':
                        outputs['identity'] = json.dumps({'callback_path': '/api/auth/core/callback', 'permissions': [
                            {'codename': 'academy-use', 'name': 'Use NCAcademy'},
                            {'codename': 'academy-school', 'name': 'Manage assigned schools and their resources'},
                            {'codename': 'academy-teach', 'name': 'Teach and manage assigned courses'},
                            {'codename': 'academy-audit', 'name': 'Review assigned student sessions'}]})
                variables, _, _ = _register(release, outputs)
                state = {'phase': 'release', 'public_config': variables}
                release.steps[0]['status'] = release.steps[1]['status'] = 'succeeded'
            else:
                status = 'succeeded'
                for step in release.steps:
                    step['status'] = 'succeeded'
        else:
            from .cloud_aws import start_run, poll_run, recover_run, store_credentials
            if not state.get('build_id'):
                key = str(release.pk) + '-' + phase
                if state.get('starting'):
                    state.update(recover_run(platform, working, key))
                else:
                    state.update(starting=True, phase=phase, run_token=hashlib.sha256(key.encode()).hexdigest())
                    recorded_intent = CloudRelease.objects.filter(pk=release.pk, lease_owner=owner, status='running',
                        lease_until__gt=timezone.now()).update(provider_state=state)
                    if not recorded_intent:
                        return True
                    state.update(start_run(platform, working, key))
                state.pop('starting', None)
                state['phase'] = phase
                state['public_config'] = working['public_config']
                outputs['build_id'] = state['build_id']
            else:
                result = poll_run(platform, working, state)
                state.update(result.get('provider_state', {}))
                outputs.update(result.get('outputs', {}))
                if result['status'] == 'failed':
                    raise ValidationError('AWS deployment failed. Review the named CodeBuild run in AWS for details.')
                if result['status'] == 'succeeded':
                    if phase == 'provision':
                        variables, credentials, _ = _register(release, outputs)
                        if working['archetype'] in {'amplify-serverless', 'amplify-fargate', 'amplify-academy-fargate', 'amplify-game-studio'}:
                            if not CloudRelease.objects.filter(pk=release.pk, lease_owner=owner, status='running', lease_until__gt=timezone.now()).exists():
                                return True
                            store_credentials(platform, working['secret_parameter'], credentials)
                        state = {'phase': 'release', 'public_config': variables}
                        release.steps[0]['status'] = release.steps[1]['status'] = 'succeeded'
                    else:
                        status = 'succeeded'
                        for step in release.steps:
                            step['status'] = 'succeeded'
        if outputs.get('origin'):
            outputs['application_url'] = release.application.public_origin or outputs['origin']
        if status == 'succeeded':
            from .models import Deployment
            Deployment.objects.filter(installation_id=release.application.installation_id, environment=release.application.environment, target='external').update(
                observed_state='running', healthy=True, last_seen=timezone.now())
    except Exception as failure:
        from .cloud_aws import AWSAdapterError, stop_run, recover_run
        if isinstance(failure, CIGateError):
            # Admission failed before AWS credential acquisition or StartBuild.
            # Do not treat the durable intent as an ambiguous billable start.
            state.pop('starting', None)
        if isinstance(failure, (ValidationError, PermissionDenied, AWSAdapterError, CIGateError)):
            error = (' '.join(failure.messages) if isinstance(failure, ValidationError) else str(failure))[:400]
        else:
            error = 'The deployment step could not be completed. Review Core worker logs and prepare a new release if needed.'
            frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(failure.__traceback__)]
            logger.error('Cloud release %s phase %s failed: %s frames=%s', release.pk,
                state.get('phase', 'provision'), type(failure).__name__, frames)
        status = 'failed'
        if state.get('starting') and not state.get('build_id') and release.application.platform.mode == 'aws':
            try:
                recovery_snapshot = copy.deepcopy(release.snapshot)
                recovery_snapshot.update(action=state.get('phase', 'provision'), plan_hash=release.digest,
                    public_config=state.get('public_config', {}))
                state.update(recover_run(release.application.platform, recovery_snapshot, str(release.pk) + '-' + state.get('phase', 'provision')))
                outputs['build_id'] = state['build_id']
            except Exception:
                outputs['recovery_token'] = state.get('run_token', '')
                error = (error[:220] + ' AWS build start could not be confirmed. Inspect the recovery token in the runner project before approving another release.')[:400]
        if (state.get('build_id') and release.application.platform.mode == 'aws' and
                CloudRelease.objects.filter(pk=release.pk, lease_owner=owner, status='running', lease_until__gt=timezone.now()).exists()):
            try:
                stop_snapshot = copy.deepcopy(release.snapshot)
                stop_snapshot.update(action=state.get('phase', 'provision'), public_config=state.get('public_config', {}))
                stop_run(release.application.platform, stop_snapshot, state)
            except Exception:
                error = (error[:280] + ' The runner could not be stopped automatically; inspect its recorded CodeBuild run in AWS.')[:400]
        for step in release.steps:
            if step['status'] in ['pending', 'running']:
                step['status'] = 'failed'
                break
    values = {'status': status, 'error': error, 'steps': release.steps, 'outputs': outputs, 'provider_state': state,
        'lease_owner': None, 'lease_until': None, 'next_poll': timezone.now() + timedelta(seconds=5 if release.application.platform.mode == 'demo' else 15),
        'updated': timezone.now()}
    recorded = CloudRelease.objects.filter(pk=release.pk, lease_owner=owner, status='running').update(**values)
    if recorded and status in ['succeeded', 'failed']:
        _audit(release, release.requested_by, 'cloud.release_' + status)
    return True
