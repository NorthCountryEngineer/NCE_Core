"""Tenant-bound, human-operated deployment console. Never renders provider secrets."""
import re
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from identity.access import request_principal, visible_resources
from identity.views import audit, organization_admin
from .cloud_models import CloudApplication, CloudPlatform, CloudRelease, hosted_assignment
from . import cloud_deployments as deployments
from .models import DeploymentEnvironment

ARCHETYPES = [
    ('amplify-static', 'Website · Amplify Hosting'),
    ('amplify-serverless', 'Website and serverless API · Amplify + Lambda'),
    ('amplify-fargate', 'Website, API and workers · Amplify + Fargate'),
    ('amplify-academy-fargate', 'Academy website and API · Amplify + Fargate'),
    ('amplify-game-studio', 'Game Studio · Amplify + serverless API'),
]


def _visible(principal, queryset):
    return visible_resources(principal, queryset)


def _allowed_environments(platform):
    return {str(value) for value in platform.config.get('environment_ids', [])}


class PlatformForm(forms.Form):
    name = forms.CharField(max_length=160, label='Platform name', help_text='For example: AWS · family production.')
    mode = forms.ChoiceField(choices=[('demo', 'Simulation · no AWS calls or charges'), ('aws', 'AWS · real deployments')], label='Deployment mode')
    account_id = forms.RegexField(r'^\d{12}$', required=False, label='AWS account ID')
    region = forms.CharField(max_length=30, required=False, initial='us-east-1', label='AWS region')
    role_arn = forms.CharField(max_length=255, required=False, label='Deployment role ARN')
    external_id = forms.CharField(max_length=256, required=False, widget=forms.PasswordInput(render_value=False), label='Role external ID', help_text='Stored encrypted. Leave blank when editing to keep the current value.')
    ci_github_token = forms.RegexField(r'^[A-Za-z0-9_]{20,512}$', required=False, max_length=512, widget=forms.PasswordInput(render_value=False), label='GitHub CI read credential', help_text='Stored encrypted. For private source repositories, grant only Actions read and Metadata read on the approved repositories. Leave blank to keep the current credential.')
    clear_ci_github_token = forms.BooleanField(required=False, label='Remove the stored GitHub CI read credential')
    runner_project = forms.CharField(max_length=255, required=False, label='CodeBuild deployment runner', help_text='The preconfigured runner builds and deploys the reviewed release.')
    connection_arn = forms.CharField(max_length=255, required=False, label='GitHub CodeConnection ARN')
    build_project = forms.CharField(max_length=255, required=False, label='Isolated source build project')
    artifact_bucket = forms.CharField(max_length=63, required=False, label='Deployment artifact bucket')
    cloudformation_role_arn = forms.CharField(max_length=255, required=False, label='CloudFormation execution role ARN')
    fargate_cloudformation_role_arn = forms.CharField(max_length=255, required=False, label='Container CloudFormation execution role ARN', help_text='The separately onboarded role for the fixed Fargate recipe.')
    permissions_boundary_arn = forms.CharField(max_length=255, required=False, label='Application role permissions boundary ARN')
    runner_repository = forms.CharField(max_length=160, required=False, label='Approved deployment runner repository', help_text='Owner/repository containing the reviewed Core deployment runner.')
    runner_commit = forms.RegexField(r'^[0-9a-fA-F]{40}$', required=False, label='Approved runner commit', help_text='Pin the exact 40-character revision of the deployment runner.')
    academy_fargate_apps = forms.JSONField(required=False, widget=forms.Textarea(attrs={'rows': 6}), label='Approved Academy container resources', help_text='Repository map: image_repository, runtime_secret_arn, certificate_arn, api_host; optional allow_live_models and allow_agent_teachers booleans (both default false). Agent teachers additionally require approved Core workload/account/resource policy.')
    fargate_apps = forms.JSONField(required=False, widget=forms.Textarea(attrs={'rows': 8}), label='Approved container resources', help_text='Operator-reviewed repository map with ECR repository URLs, runtime secret ARN, certificate ARN and API hostname. Enter resource references only.')
    game_studio_apps = forms.JSONField(required=False, widget=forms.Textarea(attrs={'rows': 8}), label='Approved Game Studio integrations', help_text='Operator-owned repository map: version 1, branding {appName, badgeText, accentColor, optional logoUrl}, and hosts [{id, name, origin, returnPath}]. HTTPS hosts only; each configuration at most 2048 UTF-8 bytes. Optional allow_paid_models defaults false; bedrock_model_arns is an exact model/profile ARN list also admitted in the platform IAM onboarding. Game creation and sign-in open top-level; only public games can be embedded.')
    prefix = forms.RegexField(r'^[a-z][a-z0-9-]{2,15}$', initial='nce', label='AWS resource prefix', help_text='Used to restrict which resources this platform can manage.')
    allowed_repositories = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}), label='Approved GitHub repositories', help_text='One Owner/repository per line. Only these repositories appear when creating an app.')
    environments = forms.ModelMultipleChoiceField(queryset=DeploymentEnvironment.objects.none(), widget=forms.CheckboxSelectMultiple, label='Available environments')
    archetypes = forms.MultipleChoiceField(choices=ARCHETYPES, widget=forms.CheckboxSelectMultiple, label='Available application templates')

    def __init__(self, *args, organization, principal, instance=None, **kwargs):
        self.instance = instance
        initial = kwargs.pop('initial', {})
        if instance:
            for key in ['name', 'mode', 'account_id', 'region', 'role_arn', 'runner_project', 'connection_arn', 'prefix']:
                initial[key] = getattr(instance, key)
            for key in ['build_project', 'artifact_bucket', 'cloudformation_role_arn', 'permissions_boundary_arn', 'runner_repository', 'runner_commit', 'fargate_cloudformation_role_arn']:
                initial[key] = instance.config.get(key, '')
            initial['allowed_repositories'] = '\n'.join(instance.allowed_repositories)
            initial['environments'] = instance.config.get('environment_ids', [])
            initial['archetypes'] = instance.config.get('archetypes', [value for value, _ in ARCHETYPES])
            initial['fargate_apps'] = instance.config.get('fargate_apps', {})
            initial['academy_fargate_apps'] = instance.config.get('academy_fargate_apps', {})
            initial['game_studio_apps'] = instance.config.get('game_studio_apps', {})
        else:
            initial.setdefault('archetypes', [value for value, _ in ARCHETYPES])
        super().__init__(*args, initial=initial, **kwargs)
        if instance and instance.encrypted_ci_github_token:
            self.fields['ci_github_token'].help_text += ' A credential is currently configured.'
        environments = _visible(principal, organization.environments.filter(active=True))
        self.fields['environments'].queryset = organization.environments.filter(pk__in=[value.pk for value in environments])

    def clean_allowed_repositories(self):
        values = [line.strip() for line in self.cleaned_data['allowed_repositories'].splitlines() if line.strip()]
        if not values or len(values) > 100 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', value) for value in values):
            raise ValidationError('Use exact Owner/repository names, one per line, up to 100 repositories.')
        if len(values) != len(set(value.lower() for value in values)):
            raise ValidationError('List each repository once.')
        return values

    def clean_runner_commit(self):
        return self.cleaned_data.get('runner_commit', '').lower()

    def clean(self):
        values = super().clean()
        if values.get('ci_github_token') and values.get('clear_ci_github_token'):
            self.add_error('clear_ci_github_token', 'Choose either replacement or removal of the stored credential.')
        if values.get('mode') == 'aws':
            for field in ['account_id', 'region', 'role_arn', 'runner_project', 'connection_arn', 'build_project', 'artifact_bucket', 'cloudformation_role_arn', 'permissions_boundary_arn', 'runner_repository', 'runner_commit']:
                if not values.get(field):
                    self.add_error(field, 'Required for an AWS deployment platform.')
            if not values.get('external_id') and not (self.instance and self.instance.encrypted_external_id):
                self.add_error('external_id', 'Provide the external ID configured in the deployment role trust policy.')
        return values

    def save(self, organization):
        platform = self.instance or CloudPlatform(organization=organization)
        for key in ['name', 'mode', 'account_id', 'region', 'role_arn', 'runner_project', 'connection_arn', 'prefix', 'allowed_repositories']:
            setattr(platform, key, self.cleaned_data[key])
        if self.cleaned_data.get('external_id'):
            platform.external_id = self.cleaned_data['external_id']
        if self.cleaned_data.get('clear_ci_github_token'):
            platform.ci_github_token = ''
        elif self.cleaned_data.get('ci_github_token'):
            platform.ci_github_token = self.cleaned_data['ci_github_token']
        platform.config = dict(platform.config or {})
        for key in ['build_project', 'artifact_bucket', 'cloudformation_role_arn', 'permissions_boundary_arn', 'runner_repository', 'runner_commit']:
            platform.config[key] = self.cleaned_data.get(key, '')
        role = self.cleaned_data.get('fargate_cloudformation_role_arn')
        if role:
            platform.config['fargate_cloudformation_role_arn'] = role
        else:
            platform.config.pop('fargate_cloudformation_role_arn', None)
        platform.config['environment_ids'] = [str(value.pk) for value in self.cleaned_data['environments']]
        platform.config['archetypes'] = list(self.cleaned_data['archetypes'])
        academy = self.cleaned_data.get('academy_fargate_apps') or {}
        if academy:
            platform.config['academy_fargate_apps'] = academy
        else:
            platform.config.pop('academy_fargate_apps', None)
        studio = self.cleaned_data.get('game_studio_apps') or {}
        if studio:
            platform.config['game_studio_apps'] = studio
        else:
            platform.config.pop('game_studio_apps', None)
        approved = self.cleaned_data.get('fargate_apps') or {}
        if approved:
            platform.config['fargate_apps'] = approved
        else:
            platform.config.pop('fargate_apps', None)
        platform.active = False
        platform.verified_at = None
        platform.full_clean()
        platform.save()
        return platform


class ApplicationForm(forms.Form):
    name = forms.CharField(max_length=160, label='Application name')
    slug = forms.SlugField(max_length=63, label='Application identifier', help_text='A short name such as axolotl-news. Used in resource names.')
    platform = forms.ModelChoiceField(queryset=CloudPlatform.objects.none(), label='Deployment platform')
    repository = forms.ChoiceField(choices=[], label='GitHub repository')
    branch = forms.CharField(max_length=200, initial='main', label='Branch')
    commit = forms.RegexField(r'^[0-9a-fA-F]{40}$', required=False, label='Commit override (optional)', help_text='Leave blank to resolve this public branch. Private repositories need an exact 40-character commit ID. Simulation uses a sample commit.')
    archetype = forms.ChoiceField(choices=ARCHETYPES, label='Service approach')
    environment = forms.ModelChoiceField(queryset=DeploymentEnvironment.objects.none(), label='Environment')
    public_origin = forms.URLField(required=False, label='Application URL (optional)', help_text='Use your existing HTTPS address, or leave blank for a generated hosting address. DNS can be connected after deployment.')

    def __init__(self, *args, organization, principal, instance=None, **kwargs):
        self.instance = instance
        initial = kwargs.pop('initial', {})
        if instance:
            initial.update({key: getattr(instance, key) for key in ['name', 'slug', 'platform', 'environment', 'repository', 'branch', 'commit', 'archetype', 'public_origin']})
        super().__init__(*args, initial=initial, **kwargs)
        platforms = _visible(principal, CloudPlatform.objects.filter(organization=organization, active=True, verified_at__isnull=False))
        environments = _visible(principal, organization.environments.filter(active=True))
        assignment = hosted_assignment(instance) if instance else None
        if assignment:
            # Only provider-authorized operators reach this form. Offer this
            # app's fixed placement, never other provider destinations/repos.
            deployments.authorized(principal, instance, 'cloud.application_configure')
            platforms = [instance.platform]
            environments = [instance.environment]
        self.platform_values = {str(value.pk): value for value in platforms}
        allowed_ids = set().union(*(_allowed_environments(value) for value in platforms)) if platforms else set()
        if assignment:
            allowed_ids = {str(instance.environment_id)}
        self.environment_values = {str(value.pk): value for value in environments if str(value.pk) in allowed_ids}
        self.fields['platform'].queryset = CloudPlatform.objects.filter(pk__in=[value.pk for value in platforms])
        self.fields['environment'].queryset = organization.environments.filter(pk__in=[value.pk for value in self.environment_values.values()])
        repositories = sorted(set(repo for value in platforms for repo in value.allowed_repositories), key=str.casefold)
        if assignment:
            repositories = [instance.repository]
        self.fields['repository'].choices = [('', 'Choose a repository')] + [(value, value) for value in repositories]
        if instance:
            for key in ['slug', 'platform', 'environment', 'archetype', 'public_origin']:
                self.fields[key].disabled = True
                self.fields[key].help_text = 'Fixed for this application. Create a separate application to use a different destination.'
        self.choice_map = {str(value.pk): {'repositories': value.allowed_repositories, 'environments': sorted(_allowed_environments(value) & set(self.environment_values)), 'archetypes': value.config.get('archetypes', [item for item, _ in ARCHETYPES])} for value in platforms}
        if assignment:
            self.choice_map = {str(instance.platform_id): {'repositories': [instance.repository],
                'environments': [str(instance.environment_id)], 'archetypes': [instance.archetype]}}

    def clean_branch(self):
        branch = self.cleaned_data['branch']
        if (branch.startswith(('-', '/')) or branch.endswith(('/', '.', '.lock')) or any(value in branch for value in ['..', '@{', '//']) or re.search(r'[\s~^:?*\[\\\x00-\x1f\x7f]', branch)):
            raise ValidationError('Enter a valid Git branch name.')
        return branch

    def clean_public_origin(self):
        value = self.cleaned_data['public_origin'].rstrip('/')
        parsed = urlsplit(value)
        if value and (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValidationError('Use an HTTPS origin such as https://axolotl.news without a path or credentials.')
        return value

    def clean(self):
        values = super().clean()
        platform = values.get('platform')
        if platform:
            if values.get('repository') not in platform.allowed_repositories:
                self.add_error('repository', 'Choose a repository approved for this platform.')
            environment = values.get('environment')
            assignment = hosted_assignment(self.instance) if self.instance else None
            allowed_environments = {str(self.instance.environment_id)} if assignment else _allowed_environments(platform)
            if environment and str(environment.pk) not in allowed_environments:
                self.add_error('environment', 'Choose an environment configured for this platform.')
            if values.get('archetype') not in platform.config.get('archetypes', [key for key, _ in ARCHETYPES]):
                self.add_error('archetype', 'This service approach is unavailable on the selected platform.')
        if values.get('commit'):
            values['commit'] = values['commit'].lower()
        return values


class ApprovalForm(forms.Form):
    digest = forms.CharField(widget=forms.HiddenInput)
    confirm_deployment = forms.BooleanField(label='Deploy this exact reviewed release')
    confirm_costs = forms.BooleanField(required=False, label='I authorize AWS resources and deployment charges for this release')

    def __init__(self, *args, release, **kwargs):
        self.release = release
        kwargs.setdefault('initial', {})['digest'] = release.digest
        super().__init__(*args, **kwargs)
        self.fields['confirm_costs'].required = release.application.platform.mode == 'aws'
        if not self.fields['confirm_costs'].required:
            self.fields.pop('confirm_costs')

    def clean_digest(self):
        if self.cleaned_data['digest'] != self.release.digest:
            raise ValidationError('The review changed. Reload and review the current release before deploying.')
        return self.cleaned_data['digest']


def _resource(request, organization, model, pk):
    principal = request_principal(request)
    scope = {'application__organization': organization} if model is CloudRelease else {'organization': organization}
    resource = get_object_or_404(model, pk=pk, **scope)
    deployments.authorized(principal, resource, 'cloud.console_access')
    return resource


@organization_admin
def platforms(request, organization):
    principal = request_principal(request)
    deployments.authorized(principal, organization, 'cloud.platform_list')
    values = _visible(principal, CloudPlatform.objects.filter(organization=organization).order_by('name'))
    return render(request, 'platform_services/cloud_platforms.html', {'organization': organization, 'platforms': values, 'title': 'Deployment platforms'})


@organization_admin
def platform_add(request, organization, pk=None):
    principal = request_principal(request)
    platform = _resource(request, organization, CloudPlatform, pk) if pk else None
    deployments.authorized(principal, platform or organization, 'cloud.platform_configure')
    form = PlatformForm(request.POST or None, organization=organization, principal=principal, instance=platform)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                platform = form.save(organization)
                audit(request, 'cloud.platform_configured', platform.pk)
        except ValidationError:
            form.add_error(None, 'The platform settings are invalid. Check the AWS account, role, connection, and resource names.')
        else:
            messages.info(request, 'Settings saved. Verify this platform before it becomes available for applications.')
            return redirect('cloud_platforms', slug=organization.slug)
    return render(request, 'platform_services/cloud_platform_form.html', {'organization': organization, 'form': form, 'platform': platform, 'title': 'Configure deployment platform'})


@organization_admin
@require_POST
def platform_verify(request, organization, pk):
    platform = _resource(request, organization, CloudPlatform, pk)
    try:
        deployments.verify_platform(platform, request_principal(request))
    except ValidationError:
        messages.error(request, 'Verification failed. Check the approved AWS account, role trust, GitHub connection, runner, and environment settings. This platform remains unavailable.')
    else:
        messages.success(request, 'Platform verified and ready for applications.')
    return redirect('cloud_platforms', slug=organization.slug)


@organization_admin
def applications(request, organization):
    principal = request_principal(request)
    deployments.authorized(principal, organization, 'cloud.application_list')
    values = _visible(principal, CloudApplication.objects.filter(organization=organization).select_related('platform', 'environment').order_by('name'))
    return render(request, 'platform_services/cloud_applications.html', {'organization': organization, 'applications': values, 'title': 'Cloud applications'})


@organization_admin
def application_add(request, organization, pk=None):
    principal = request_principal(request)
    app = _resource(request, organization, CloudApplication, pk) if pk else None
    deployments.authorized(principal, app or organization, 'cloud.application_configure')
    form = ApplicationForm(request.POST or None, organization=organization, principal=principal, instance=app)
    if request.method == 'POST' and form.is_valid():
        try:
            application = deployments.update_application(app, principal, **form.cleaned_data) if app else deployments.create_application(organization, principal, **form.cleaned_data)
        except ValidationError:
            form.add_error(None, 'The application could not be configured. Check the approved repository, source revision, platform, and environment.')
        else:
            return redirect('cloud_application', slug=organization.slug, pk=application.pk)
    return render(request, 'platform_services/cloud_application_form.html', {'organization': organization, 'form': form, 'choice_map': form.choice_map, 'has_platforms': bool(form.platform_values), 'application': app, 'title': 'Application settings' if app else 'Create application'})


@organization_admin
def application(request, organization, pk):
    app = _resource(request, organization, CloudApplication, pk)
    releases = _visible(request_principal(request), CloudRelease.objects.filter(application=app).order_by('-created')[:50])
    return render(request, 'platform_services/cloud_application.html', {'organization': organization, 'application': app,
        'releases': releases, 'can_manage_hosting': deployments.can_manage_hosting(request_principal(request), app), 'title': app.name})


@organization_admin
@require_POST
def release_prepare(request, organization, pk):
    app = _resource(request, organization, CloudApplication, pk)
    try:
        release = deployments.prepare_release(app, request_principal(request))
    except ValidationError as error:
        messages.error(request, ' '.join(error.messages) if getattr(error, 'code', '') == 'ci_checks_required' else
            'A release could not be prepared. Check the application source and platform settings before trying again.')
        return redirect('cloud_application', slug=organization.slug, pk=app.pk)
    return redirect('cloud_release', slug=organization.slug, pk=release.pk)


def _release_context(organization, release, form=None, principal=None):
    # Raw provider state may contain signed URLs or credentials. It never reaches a template.
    public_outputs = []
    for key in ['origin', 'application_url', 'frontend_url', 'api_url', 'stack_name', 'amplify_app_id', 'build_id', 'recovery_token', 'commit']:
        value = release.outputs.get(key)
        if isinstance(value, str):
            url = ''
            if key.endswith('_url') or key == 'origin':
                try:
                    parsed = urlsplit(value)
                except ValueError:
                    continue
                if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    continue
                url = value
            elif len(value) > 255 or re.search(r'[\x00-\x1f]', value):
                continue
            public_outputs.append({'name': key.replace('_', ' ').capitalize(), 'value': value, 'url': url})
    return {'organization': organization, 'release': release, 'application': release.application, 'form': form or ApprovalForm(release=release), 'public_outputs': public_outputs,
            'live_enabled': settings.CORE_ENABLE_CLOUD_DEPLOYMENTS,
            'can_manage_hosting': bool(principal and deployments.can_manage_hosting(principal, release.application)), 'title': 'Review release'}


@organization_admin
def release(request, organization, pk):
    value = _resource(request, organization, CloudRelease, pk)
    return render(request, 'platform_services/cloud_release.html', _release_context(organization, value, principal=request_principal(request)))


@organization_admin
@require_POST
def release_approve(request, organization, pk):
    value = _resource(request, organization, CloudRelease, pk)
    form = ApprovalForm(request.POST, release=value)
    if form.is_valid():
        try:
            deployments.approve_release(value, request_principal(request), confirm_costs=bool(form.cleaned_data.get('confirm_costs')))
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages) if getattr(error, 'code', '') == 'ci_checks_required' else
                'The release cannot be approved. Reload the review; the platform, source, access, or release status may have changed.')
        else:
            messages.success(request, 'Release approved. The deployment worker will run the reviewed steps.')
            return redirect('cloud_release', slug=organization.slug, pk=value.pk)
    return render(request, 'platform_services/cloud_release.html', _release_context(organization, value, form, request_principal(request)), status=400)


@organization_admin
@require_POST
def release_cancel(request, organization, pk):
    value = _resource(request, organization, CloudRelease, pk)
    try:
        deployments.cancel_release(value, request_principal(request))
    except ValidationError:
        messages.error(request, 'This release cannot be cancelled in its current state.')
    else:
        messages.info(request, 'Simulated release cancelled.' if value.application.platform.mode == 'demo' else 'Cancellation recorded. Resources already created remain in AWS; review the release outputs before cleanup.')
    return redirect('cloud_release', slug=organization.slug, pk=value.pk)
