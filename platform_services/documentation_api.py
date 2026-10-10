"""Read-only documentation APIs for application backends and scoped agent accounts."""
import json

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from .api import response
from .documentation import chunks, documents, principal_context, render_document, require_documentation_capabilities
from .integration_api import integration_api
from .models import DocumentationPage


def principals(request):
    source, reader = request.integration_source, request.access_principal
    for principal in [source, reader] if source is not reader else [reader]:
        require_documentation_capabilities(principal, request.organization, ['docs.read'])
    return reader, source, principal_context(source)


def private(result):
    result['Cache-Control'] = 'no-store, private'
    result['X-Content-Type-Options'] = 'nosniff'
    return result


@integration_api(namespace='documentation')
def index(request):
    if set(request.GET) - {'q', 'limit', 'after'}:
        raise ValidationError('Documentation context comes from the authenticated deployment.')
    reader, source, context = principals(request)
    try:
        limit = int(request.GET.get('limit', 20))
    except ValueError:
        raise ValidationError('Use a bounded documentation limit.')
    rows, cursor = documents(request.organization, reader, context, query=request.GET.get('q', ''),
        limit=limit, after=request.GET.get('after'), source_principal=source)
    return private(response({'organization_id': str(request.organization.pk), 'documents': rows, 'next_cursor': cursor}))


@integration_api(namespace='documentation')
def detail(request, pk):
    if set(request.GET) - {'format'}:
        raise ValidationError('Unsupported documentation parameters.')
    reader, source, context = principals(request)
    page = get_object_or_404(DocumentationPage, pk=pk, organization=request.organization)
    request.integration_audit_resource = page
    value = render_document(page, reader, context, source_principal=source)
    selected = request.GET.get('format', 'json')
    if selected == 'json':
        return private(response(value))
    if selected == 'chunks':
        return private(response({'organization_id': str(request.organization.pk), 'document_id': value['id'],
            'chunks': chunks(value)}))
    if selected == 'jsonl':
        result = HttpResponse('\n'.join(json.dumps(row, ensure_ascii=False) for row in chunks(value)) + '\n',
            content_type='application/x-ndjson; charset=utf-8')
    elif selected == 'markdown':
        result = HttpResponse(value['markdown'], content_type='text/markdown; charset=utf-8')
    else:
        raise ValidationError('Choose json, chunks, jsonl, or markdown.')
    result['Content-Disposition'] = 'attachment; filename="documentation.' + ('jsonl' if selected == 'jsonl' else 'md') + '"'
    result['X-Core-Documentation-Citation'] = value['citation']
    result['X-Core-Documentation-Digest'] = value['digest']
    return private(result)
