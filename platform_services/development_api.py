"""Applications and agents propose recipes and consume explicitly reviewed revisions."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from .api import body, response
from .development import (authorize_object, create_object, export_bundle, revision_document, require_development,
    run_document, run_revision, save_revision)
from .integration_api import integration_api
from .models import DevelopmentObject, DevelopmentRevision, DevelopmentRun


def development_api(methods=('GET',)):
    return integration_api(methods, namespace='development')


def reader(request):
    for principal in [request.integration_source, request.access_principal]:
        require_development(principal, request.organization, 'development.read')
    return request.access_principal, request.integration_source


@development_api(('GET', 'POST'))
def objects(request):
    principal, source = reader(request)
    if request.method == 'POST':
        obj = create_object(request.organization, principal, body(request), source)
        revision = obj.versions.get(number=1)
        return response({'object_id': str(obj.pk), 'revision_id': str(revision.pk), 'number': 1,
            'digest': revision.digest, 'published': False}, 201)
    if set(request.GET) - {'after', 'limit', 'kind'}:
        raise ValidationError('Use kind and bounded cursor pagination.')
    try:
        limit = int(request.GET.get('limit', 20))
        if not 1 <= limit <= 50:
            raise ValueError
    except ValueError:
        raise ValidationError('Limit is 1–50.')
    query = DevelopmentObject.objects.filter(organization=request.organization, active=True).order_by('pk')
    if request.GET.get('kind'):
        if request.GET['kind'] not in dict(DevelopmentObject._meta.get_field('kind').choices):
            raise ValidationError('Unknown object kind.')
        query = query.filter(kind=request.GET['kind'])
    if request.GET.get('after'):
        from uuid import UUID
        try:
            query = query.filter(pk__gt=UUID(request.GET['after']))
        except ValueError:
            raise ValidationError('Use the returned cursor.')
    values, cursor, scanned = [], None, 0
    for obj in query[:200]:
        scanned += 1
        cursor = str(obj.pk)
        try:
            authorize_object(obj, principal, source)
            for revision in obj.versions.filter(published=True).order_by('-number')[:20]:
                try:
                    values.append(revision_document(revision, principal, source))
                    break
                except PermissionDenied:
                    continue
        except PermissionDenied:
            continue
        if len(values) == limit:
            break
    return response({'organization_id': str(request.organization.pk), 'objects': values,
        'next_cursor': cursor if len(values) == limit or scanned == 200 else None})


@development_api(('POST',))
def revisions(request, pk):
    principal, source = reader(request)
    obj = get_object_or_404(DevelopmentObject, pk=pk, organization=request.organization)
    request.integration_audit_resource = obj
    values = body(request)
    if set(values) != {'revision', 'spec'}:
        raise ValidationError('Supply the current revision and replacement recipe.')
    revision = save_revision(obj, principal, values['revision'], values['spec'], source)
    return response({'revision_id': str(revision.pk), 'number': revision.number, 'digest': revision.digest, 'published': False}, 201)


def selected(request, pk):
    item = get_object_or_404(DevelopmentRevision, pk=pk, development_object__organization=request.organization)
    request.integration_audit_resource = item
    return item


@development_api()
def detail(request, pk):
    if set(request.GET) - {'format'} or request.GET.get('format', 'json') not in {'json', 'bundle'}:
        raise ValidationError('Choose json or bundle.')
    principal, source = reader(request)
    revision = selected(request, pk)
    return response(export_bundle(revision, principal, source) if request.GET.get('format') == 'bundle' else revision_document(revision, principal, source))


@development_api(('POST',))
def execute(request, pk):
    principal, source = reader(request)
    row = run_revision(selected(request, pk), principal, body(request), source)
    return response(run_document(row, principal, source), 201)


@development_api()
def run(request, pk):
    if request.GET:
        raise ValidationError('Run scope comes from the authenticated context.')
    principal, source = reader(request)
    row = get_object_or_404(DevelopmentRun, pk=pk, organization=request.organization)
    request.integration_audit_resource = row
    return response(run_document(row, principal, source))
