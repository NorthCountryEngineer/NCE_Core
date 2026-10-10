import json
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from identity.access import request_principal, visible_resources
from identity.views import organization_admin
from .app_factory import DEFAULT_SPEC, approve_build, artifacts, authorize, create_project, release, source_archive
from .models import AppProject, DeploymentEnvironment
from .training import require_capability


@organization_admin
def index(request, organization):
    principal = request_principal(request)
    require_capability(principal, organization, ['appfactory.read'])
    return render(request, 'platform_services/app_factory.html', {'organization': organization,
        'projects': visible_resources(principal, organization.app_projects.order_by('-created')[:100])})


@organization_admin
def create(request, organization):
    principal = request_principal(request)
    require_capability(principal, organization, ['appfactory.create'])
    class ProjectForm(forms.Form):
        name = forms.CharField(max_length=160)
        description = forms.CharField(widget=forms.Textarea, max_length=12000)
        environment = forms.ModelChoiceField(queryset=DeploymentEnvironment.objects.none())
        specification = forms.JSONField(initial=DEFAULT_SPEC,
            help_text='Your AI can produce this specification. Review the collections and fields; description text alone does not implement custom logic.')
    form = ProjectForm(request.POST or None)
    form.fields['environment'].queryset = DeploymentEnvironment.objects.filter(pk__in=[env.pk for env in visible_resources(principal,
        organization.environments.filter(active=True).exclude(name='production'))])
    if request.method == 'POST' and form.is_valid():
        try:
            project = create_project(organization, principal=principal, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error.messages[0])
        else:
            return redirect('app_factory_project', slug=organization.slug, pk=project.pk)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Describe an application and review its specification'})


@organization_admin
def detail(request, organization, pk):
    project = authorize(get_object_or_404(AppProject, organization=organization, pk=pk), request_principal(request))
    source_hash = None
    artifact_status = ''
    if project.build_id and project.build.status == 'succeeded':
        try:
            _, source_hash = artifacts(project, request_principal(request))
        except (PermissionDenied, ValidationError):
            artifact_status = 'Source export requires valid current task, account, and role approvals and intact artifacts.'
    class ReleaseForm(forms.Form):
        source_hash = forms.CharField(max_length=64)
        image = forms.CharField(help_text='Your reviewed container image pinned with @sha256:', max_length=500)
        verification_reference = forms.CharField(max_length=500, help_text='Reference to the reviewed, successful tests and container/source provenance. Core does not independently attest this evidence.')
    return render(request, 'platform_services/app_factory_project.html', {'organization': organization, 'project': project,
        'specification': json.dumps(project.specification, indent=2), 'source_hash': source_hash, 'artifact_status': artifact_status,
        'steps': project.build.step_results.all() if project.build_id else [], 'release_form': ReleaseForm(initial={'source_hash': source_hash})})


@organization_admin
@require_POST
def approve(request, organization, pk):
    project = get_object_or_404(AppProject, organization=organization, pk=pk)
    try:
        approve_build(project, request_principal(request))
    except ValidationError:
        return HttpResponse('Review the specification, agent marketplace listings, task environment, and permission policy before building.', status=400)
    return redirect('app_factory_project', slug=organization.slug, pk=pk)


@organization_admin
def download(request, organization, pk):
    project = get_object_or_404(AppProject, organization=organization, pk=pk)
    archive, source_hash = source_archive(project, request_principal(request))
    response = HttpResponse(archive, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="app-{project.pk}.zip"'
    response['Cache-Control'] = 'no-store'
    response['X-Core-Source-Digest'] = source_hash
    return response


@organization_admin
@require_POST
def publish(request, organization, pk):
    project = get_object_or_404(AppProject, organization=organization, pk=pk)
    try:
        release(project, request_principal(request), request.POST.get('image'), request.POST.get('source_hash'),
            request.POST.get('verification_reference'))
    except ValidationError:
        return HttpResponse('Release requires the reviewed source digest, valid image digest, and successful test evidence.', status=400)
    return redirect('app_factory_project', slug=organization.slug, pk=pk)
