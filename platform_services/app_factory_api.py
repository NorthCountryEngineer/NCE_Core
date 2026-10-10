from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from .api import body, response, workload_api
from .app_factory import authorize, create_project, source_archive
from .models import AppProject, DeploymentEnvironment


def document(project):
    return {'id': str(project.pk), 'name': project.name, 'description': project.description,
        'specification': project.specification, 'environment': project.environment.name,
        'status': project.build.status if project.build_id else 'needs_human_approval',
        'build_id': str(project.build_id) if project.build_id else None,
        'package_slug': project.package.slug if project.package_id else None,
        'source_digest': project.build.output.get('source_digest') if project.build_id and project.build.output else None}


@workload_api(['appfactory.create'], methods=('POST',))
def create(request):
    values = body(request)
    if set(values) != {'environment', 'name', 'description', 'specification'} or not isinstance(values.get('environment'), str) or values['environment'] not in {'sandbox', 'development', 'staging', 'uat'}:
        raise ValidationError('Use a name, description, specification, and non-production environment.')
    environment = get_object_or_404(DeploymentEnvironment, organization=request.organization, name=values.get('environment'), active=True)
    project = create_project(request.organization, environment, values.get('name'), values.get('description'),
                             values.get('specification'), request.access_principal)
    return response(document(project), 201)


@workload_api(['appfactory.read'])
def detail(request, pk):
    project = authorize(get_object_or_404(AppProject, organization=request.organization, pk=pk), request.access_principal)
    return response(document(project))


@workload_api(['appfactory.read'])
def download(request, pk):
    project = get_object_or_404(AppProject, organization=request.organization, pk=pk)
    archive, source_hash = source_archive(project, request.access_principal)
    result = HttpResponse(archive, content_type='application/zip')
    result['Cache-Control'] = 'no-store'
    result['X-Core-Source-Digest'] = source_hash
    return result
