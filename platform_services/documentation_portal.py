"""Organization reader and human-reviewed documentation administration."""
from functools import wraps
import json

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache

from identity.access import request_principal, require_access, visible_resources
from identity.models import Membership, Organization
from identity.views import organization_admin
from .documentation import (DocumentationContext, chunks, documents, publish_source, render_document,
                            safe_html, save_page, require_documentation_capabilities)
from .documentation_catalog import GUIDES
from .models import Deployment, DocumentationPage, DocumentationSource, MarketplacePackage


def organization_reader(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, slug, *args, **kwargs):
        organization = get_object_or_404(Organization, slug=slug, active=True)
        if not request.user.is_superuser and not Membership.objects.filter(organization=organization, user=request.user, active=True).exists():
            raise PermissionDenied
        request.organization = organization
        principal = request_principal(request)
        require_documentation_capabilities(principal, organization, ['docs.read'])
        require_access(principal, organization, 'documentation.portal')
        return view(request, organization, *args, **kwargs)
    return wrapped


def context_for(request, organization):
    principal = request_principal(request)
    environment = get_object_or_404(organization.environments, pk=request.GET['environment'], active=True) if request.GET.get('environment') else None
    deployment = get_object_or_404(Deployment,
        pk=request.GET['deployment'], installation__organization=organization) if request.GET.get('deployment') else None
    if deployment:
        if environment and environment.pk != deployment.environment_id:
            raise PermissionDenied
        environment = deployment.environment
    for resource in [environment, deployment]:
        if resource:
            require_access(principal, resource, 'documentation.context')
    return DocumentationContext(environment, deployment)


@organization_reader
def index(request, organization):
    try:
        rows, cursor = documents(organization, request_principal(request), context_for(request, organization),
            query=request.GET.get('q', ''), limit=20, after=request.GET.get('after'))
    except (ValidationError, ValueError):
        return HttpResponseBadRequest('Use a valid query, cursor, and organization environment or deployment.')
    parameters = request.GET.copy()
    if cursor:
        parameters['after'] = cursor
    return render(request, 'platform_services/documentation.html', {'organization': organization,
        'documents': rows, 'query': request.GET.get('q', ''), 'next_query': parameters.urlencode() if cursor else None,
        'environments': visible_resources(request_principal(request), organization.environments.filter(active=True)),
        'selected_environment': request.GET.get('environment', ''),
        'context_query': '&'.join(f'{key}={value}' for key, value in request.GET.items() if key in {'environment', 'deployment'}),
        'can_manage': request.user.is_superuser or organization.memberships.filter(user=request.user, active=True, kind='administrator').exists()})


def local_links(text, organization, principal):
    """Resolve bundled guide links only after authorizing their target pages."""
    import re
    from pathlib import PurePosixPath
    filenames = {}
    for package, slug, *_, filename in GUIDES:
        filenames.setdefault(PurePosixPath(filename).name, (package, slug))
    def replace(match):
        target = match.group(2)
        filename, _, fragment = target.partition('#')
        reference = filenames.get(PurePosixPath(filename).name)
        if not reference or ':' in filename or filename.startswith('/'):
            return match.group(0)
        page = DocumentationPage.objects.filter(organization=organization, installation__package__slug=reference[0],
            slug=reference[1], environment__isnull=True, deployment__isnull=True, active=True).first()
        try:
            if page:
                render_document(page, principal)
                url = reverse('documentation_page', args=[organization.slug, page.pk])
                return f'[{match.group(1)}]({url}' + ('#' + fragment if fragment else '') + ')'
        except PermissionDenied:
            pass
        return match.group(1)
    return re.sub(r'\[([^\]]+)\]\(([^\s)]+)\)', replace, text)


@organization_reader
def detail(request, organization, pk):
    page = get_object_or_404(DocumentationPage, pk=pk, organization=organization)
    principal = request_principal(request)
    try:
        value = render_document(page, principal, context_for(request, organization))
    except (ValidationError, ValueError):
        return HttpResponseBadRequest('Use a valid organization environment or deployment.')
    selected = request.GET.get('format')
    if selected in {'markdown', 'json', 'jsonl'}:
        contents = value['markdown'] if selected == 'markdown' else json.dumps(value, ensure_ascii=False, indent=2) if selected == 'json' else '\n'.join(json.dumps(row, ensure_ascii=False) for row in chunks(value)) + '\n'
        result = HttpResponse(contents, content_type={'markdown': 'text/markdown', 'json': 'application/json', 'jsonl': 'application/x-ndjson'}[selected] + '; charset=utf-8')
        result['Content-Disposition'] = 'attachment; filename="documentation.' + {'markdown': 'md', 'json': 'json', 'jsonl': 'jsonl'}[selected] + '"'
        result['Cache-Control'] = 'no-store, private'
        return result
    if selected:
        return HttpResponseBadRequest('Choose markdown, json, or jsonl.')
    parameters = request.GET.copy()
    links = {}
    for selected in ['markdown', 'json', 'jsonl']:
        parameters['format'] = selected
        links[selected] = parameters.urlencode()
    return render(request, 'platform_services/documentation_page.html', {'organization': organization, 'document': value,
        'html': mark_safe(safe_html(local_links(value['markdown'], organization, principal))), 'exports': links})


class PageForm(forms.ModelForm):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    class Meta:
        model = DocumentationPage
        fields = ['installation', 'environment', 'deployment', 'source', 'slug', 'title', 'summary', 'category',
            'audience', 'required_permissions', 'mode', 'markdown', 'active']


@organization_admin
def administration(request, organization):
    principal = request_principal(request)
    require_documentation_capabilities(principal, organization, ['docs.manage'])
    pages = visible_resources(principal, organization.documentation_pages.select_related('source', 'installation__package', 'environment', 'deployment'))
    for page in pages:
        page.source_choices = DocumentationSource.objects.filter(package_id=page.installation.package_id, version=page.installation.version,
            slug=page.source.slug, active=True).order_by('-revision') if page.source_id and page.installation_id else []
        page.available_revision = next(iter(page.source_choices), None)
        page.needs_review = bool(page.source_id and (not page.source.active or page.source.version != page.installation.version or
            page.available_revision and page.available_revision.pk != page.source_id))
    installations = visible_resources(principal, organization.installations.filter(enabled=True).select_related('package'))
    coverage = [{'installation': item, 'has_docs': any(page.installation_id == item.pk for page in pages)} for item in installations]
    return render(request, 'platform_services/documentation_admin.html', {'organization': organization, 'pages': pages, 'coverage': coverage})


@organization_admin
def edit(request, organization, pk=None):
    principal = request_principal(request)
    require_documentation_capabilities(principal, organization, ['docs.manage'])
    page = get_object_or_404(DocumentationPage, pk=pk, organization=organization) if pk else DocumentationPage(organization=organization)
    if pk:
        require_access(principal, page, 'documentation.manage')
    form = PageForm(request.POST or None, instance=page, initial={'expected_revision': page.revision, 'mode': 'replace' if not pk else page.mode})
    allowed_installations = visible_resources(principal, organization.installations.filter(enabled=True))
    querysets = {'installation': organization.installations.filter(pk__in=[item.pk for item in allowed_installations]),
        'environment': organization.environments.filter(active=True),
        'deployment': Deployment.objects.filter(installation__organization=organization)}
    for name, queryset in querysets.items():
        if pk and getattr(page, name + '_id'):
            # Administrators can archive existing placement after its app/environment is disabled.
            queryset = queryset | queryset.model.objects.filter(pk=getattr(page, name + '_id'))
        form.fields[name].queryset = queryset.filter(pk__in=[item.pk for item in visible_resources(principal, queryset)])
    sources = DocumentationSource.objects.none()
    for installation in allowed_installations:
        sources = sources | DocumentationSource.objects.filter(package_id=installation.package_id, version=installation.version, active=True)
    if pk and page.source_id:
        sources = sources | DocumentationSource.objects.filter(pk=page.source_id)
    form.fields['source'].queryset = sources.order_by('package__name', 'slug', '-revision')
    if pk:
        for name in ['installation', 'environment', 'deployment', 'slug']:
            form.fields[name].disabled = True
    if request.method == 'POST' and form.is_valid():
        try:
            save_page(form.save(commit=False), principal, form.cleaned_data['expected_revision'])
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'A page already exists at this placement.')
        else:
            return redirect('documentation_admin', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Organization documentation',
        'documentation_history': page.revisions.order_by('-revision')[:20] if pk else []})


class SourceForm(forms.Form):
    package = forms.ModelChoiceField(queryset=MarketplacePackage.objects.none())
    bundle = forms.JSONField(help_text='One versioned page: version, slug, title, summary, category, audience, required_permissions, markdown. No credentials or regulated records.')


@organization_admin
def publish(request, organization):
    principal = request_principal(request)
    require_documentation_capabilities(principal, organization, ['docs.manage'])
    form = SourceForm(request.POST or None)
    form.fields['package'].queryset = MarketplacePackage.objects.filter(active=True).filter(
        Q(organization=organization) | Q(organization__isnull=True) if request.user.is_superuser else Q(organization=organization))
    if request.method == 'POST' and form.is_valid():
        try:
            publish_source(form.cleaned_data['package'], form.cleaned_data['bundle'], principal)
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, error if isinstance(error, ValidationError) else 'A revision was published concurrently; reload.')
        else:
            return redirect('documentation_admin', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Publish application documentation'})
