"""Live, tenant-scoped documentation inheritance and retrieval. No remote fetching."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

import bleach
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
import markdown as markdown_renderer

from identity.access import decision, effective_tags, record_decision, require_access
from identity.models import Membership, ServicePermission
from identity.policy import authorization_for
from .models import DocumentationPage, DocumentationRevision, DocumentationSource, Installation
from .training import require_capability

ROOT = Path(__file__).resolve().parent.parent
VARIABLES = {'organization_name', 'platform_name', 'application_name', 'application_version', 'environment_name',
             'namespace', 'application_origin', 'service_id', 'organization_id', 'deployment_id', 'support_email'}


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def validate_markdown(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 131072:
        raise ValidationError('Documentation must contain at most 128 KiB of Markdown.')
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value) or any(name not in VARIABLES for name in re.findall(r'\{\{!?\s*([a-z_]+)\s*\}\}', value)):
        raise ValidationError('Use only documented organization/application placeholders; no executable templates.')


def validate_permissions(value):
    if not isinstance(value, list) or len(value) > 20 or any(not isinstance(item, str) or item == 'admin' or
            not re.fullmatch('[a-z][a-z0-9_-]{0,79}', item) for item in value) or len(set(value)) != len(value):
        raise ValidationError('Use at most 20 distinct registered service permission names; the reserved admin permission is excluded.')


def source_snapshot(source):
    return {key: getattr(source, key) for key in ['version', 'slug', 'title', 'summary', 'category', 'audience', 'required_permissions', 'markdown', 'revision']}


def source_digest(source):
    return checksum({'package_id': str(source.package_id), **source_snapshot(source)})


def page_snapshot(page):
    return {**{key: getattr(page, key) for key in ['title', 'summary', 'category', 'audience', 'required_permissions', 'mode', 'markdown', 'active']},
            'source_id': str(page.source_id) if page.source_id else None}


def snapshot_page(page):
    value = page_snapshot(page)
    DocumentationRevision.objects.create(page=page, revision=page.revision, snapshot=value, digest=checksum(value))


@transaction.atomic
def publish_source(package, values, principal):
    from .models import MarketplacePackage
    package = MarketplacePackage.objects.select_for_update().get(pk=package.pk)
    if not principal.user or not principal.user.is_active:
        raise PermissionDenied('Documentation publishing is a human publisher operation.')
    if package.organization_id:
        require_capability(principal, package.organization, ['docs.manage'])
        require_access(principal, package.organization, 'documentation.publish')
        if not principal.user.is_superuser and not Membership.objects.filter(organization=package.organization, user=principal.user, active=True, kind='administrator').exists():
            raise PermissionDenied('An administrator can publish only their organization’s private package documentation.')
    elif not principal.user.is_superuser:
        raise PermissionDenied('Shared marketplace documentation requires a platform publisher.')
    allowed = {'version', 'slug', 'title', 'summary', 'category', 'audience', 'required_permissions', 'markdown'}
    if not isinstance(values, dict) or set(values) != allowed:
        raise ValidationError('Supply versioned documentation metadata, permissions, and Markdown.')
    if any(not isinstance(values[key], str) for key in allowed - {'required_permissions'}):
        raise ValidationError('Documentation metadata and Markdown must be strings.')
    previous = DocumentationSource.objects.filter(package=package, version=values['version'], slug=values['slug']).order_by('-revision').first()
    source = DocumentationSource(package=package, revision=previous.revision + 1 if previous else 1, **values)
    source.full_clean()
    source.save()
    if package.organization_id:
        record_decision(principal, package.organization, 'documentation.source_published', True, 'immutable_publisher_revision', [])
    for installation in Installation.objects.filter(package=package, version=source.version, enabled=True):
        inherit_installation(installation)
    return source


@transaction.atomic
def inherit_installation(installation):
    """Initial inheritance never overwrites organization customizations or revision choices."""
    ServicePermission.objects.get_or_create(service=installation.service, codename='docs-read', defaults={'name': 'Read application documentation'})
    sources = DocumentationSource.objects.filter(package=installation.package, version=installation.version, active=True).order_by('slug', '-revision')
    seen = set()
    for source in sources:
        if source.slug in seen:
            continue
        seen.add(source.slug)
        key = ':'.join([str(installation.pk), '-', '-'])
        if DocumentationPage.objects.filter(organization=installation.organization, scope_key=key, slug=source.slug).exists():
            continue
        page = DocumentationPage(organization=installation.organization, installation=installation, source=source,
            slug=source.slug, title=source.title, summary=source.summary, category=source.category, audience=source.audience)
        page.full_clean()
        page.save()
        snapshot_page(page)


@dataclass(frozen=True)
class DocumentationContext:
    environment: object = None
    deployment: object = None
    consumer: object = None


def principal_context(principal):
    from .environments import principal_environment
    environment = principal_environment(principal)
    if principal.credential:
        from .models import WorkloadCredential
        credential = WorkloadCredential.objects.select_related('installation', 'deployment').get(pk=principal.credential.pk)
        return DocumentationContext(environment, credential.deployment, credential.installation)
    if principal.agent_binding:
        return DocumentationContext(environment, None, principal.agent_binding.installation)
    return DocumentationContext()


def require_service_permissions(principal, service, names):
    if principal.agent_binding:
        from .agent_identity import require_permission
        for name in names:
            require_permission(principal, service, name)
        return
    if principal.credential:
        if names:
            raise PermissionDenied('This page needs delegated user service permissions.')
        return
    membership = Membership.objects.filter(organization=service.organization, user=principal.user, active=True).first()
    if principal.user and principal.user.is_superuser:
        available = set(service.permissions.values_list('codename', flat=True))
    elif membership:
        available = set(authorization_for(membership, service, principal)['permissions'])
        if membership.kind != 'administrator':
            roles = [role for role in membership.roles.filter(organization=service.organization) if decision(principal, role)[0]]
            available &= set(ServicePermission.objects.filter(service=service, roles__in=roles).values_list('codename', flat=True))
    else:
        available = set()
    if set(names) - available:
        raise PermissionDenied('A live application documentation role is required.')
    for permission in service.permissions.filter(codename__in=names):
        require_access(principal, permission, 'documentation.permission')


def require_documentation_capabilities(principal, organization, capabilities):
    require_capability(principal, organization, capabilities)
    if not principal.credential:
        service = Installation.objects.get(organization=organization, package__slug='core-platform').service
        # Role policy is checked as well as the permission and service policies.
        require_service_permissions(principal, service, [name.replace('.', '-') for name in capabilities])


def page_environments(page):
    from .models import DeploymentEnvironment
    return list(DeploymentEnvironment.objects.filter(organization=page.organization,
        boundary_tag_id__in=[tag.pk for tag in effective_tags(page)]))


def authorize_page(page, principal, context=None, delegated=False):
    page = DocumentationPage.objects.select_related('organization', 'installation__package', 'installation__service', 'source__package', 'environment', 'deployment').get(pk=page.pk)
    require_documentation_capabilities(principal, page.organization, ['docs.read'])
    require_access(principal, page, 'documentation.read')
    if not page.active or page.environment_id and not page.environment.active:
        raise PermissionDenied('Documentation is disabled.')
    installation = page.installation
    if installation and (not installation.enabled or not installation.service.active or not installation.package.active or
            installation.package.organization_id and installation.package.organization_id != page.organization_id):
        raise PermissionDenied('The documented application is unavailable.')
    source = page.source
    if source and (not source.active or not installation or source.package_id != installation.package_id or source.version != installation.version or source_digest(source) != source.digest):
        raise PermissionDenied('The source is withdrawn, corrupt, or does not match the installed version. Review updated documentation.')
    context = context or principal_context(principal)
    boundaries = page_environments(page)
    if any(not environment.active for environment in boundaries):
        raise PermissionDenied('The documented environment is disabled.')
    if context.environment and boundaries and context.environment.pk not in {environment.pk for environment in boundaries}:
        raise PermissionDenied('Documentation context does not match its inherited environment tags.')
    if context.consumer and installation and installation.pk != context.consumer.pk and installation.package.kind != 'core':
        raise PermissionDenied('Applications and agents consume their own documentation and shared platform guidance.')
    if context.environment and page.environment_id and context.environment.pk != page.environment_id:
        raise PermissionDenied('Documentation belongs to another environment.')
    if context.consumer and page.deployment_id and (not context.deployment or page.deployment_id != context.deployment.pk):
        raise PermissionDenied('Documentation belongs to another deployment.')
    if page.audience == 'administrators' or source and source.audience == 'administrators':
        require_documentation_capabilities(principal, page.organization, ['docs.admin'])
    if not delegated:
        service = installation.service if installation else Installation.objects.get(organization=page.organization, package__slug='core-platform').service
        names = set(page.required_permissions) | set(source.required_permissions if source else [])
        if installation and installation.package.kind == 'application' and not principal.credential:
            names.add('docs-read')
        require_service_permissions(principal, service, names)
    return page


def render_document(page, principal, context=None, source_principal=None):
    context = context or principal_context(source_principal or principal)
    if source_principal and source_principal is not principal:
        authorize_page(page, source_principal, context, delegated=True)
    page = authorize_page(page, principal, context)
    organization, installation = page.organization, page.installation
    environment = page.environment or context.environment
    if not environment:
        boundaries = page_environments(page)
        environment = boundaries[0] if len(boundaries) == 1 else None
    deployment = page.deployment or (context.deployment if context.deployment and context.deployment.installation_id == page.installation_id else None)
    for item in [environment, deployment]:
        if item:
            require_access(principal, item, 'documentation.context')
            if source_principal and source_principal is not principal:
                require_access(source_principal, item, 'documentation.context')
    profile = getattr(organization, 'profile', None)
    support = profile.support_email if profile and decision(principal, profile)[0] and (not source_principal or decision(source_principal, profile)[0]) else 'Ask your organization administrator'
    variables = {'organization_name': organization.name, 'platform_name': organization.brand,
        'organization_id': str(organization.pk), 'application_name': installation.package.name if installation else 'Platform',
        'application_version': installation.version if installation else 'Not applicable', 'environment_name': environment.name if environment else 'Select an environment',
        'namespace': environment.namespace if environment else 'Not selected', 'application_origin': deployment.endpoint if deployment else environment.application_origin if environment else 'Not selected',
        'service_id': str(installation.service_id) if installation else 'Platform workspace', 'deployment_id': str(deployment.pk) if deployment else 'Not selected', 'support_email': support}
    source_text = page.source.markdown if page.source else ''
    text = source_text if page.mode == 'inherit' else source_text + '\n\n## Organization guidance\n\n' + page.markdown if page.mode == 'extend' else page.markdown
    rendered_bytes = len(text.encode())
    def replace(match):
        nonlocal rendered_bytes
        value = variables.get(match.group(1), 'Unavailable')
        escaped = re.sub(r'([\\`*_{}\[\]()#+.!>|~-])', r'\\\1', str(value))
        rendered_bytes += len(escaped.encode()) - len(match.group(0).encode())
        if rendered_bytes > 300000:
            raise PermissionDenied('Rendered organization guidance exceeds the retrieval size limit; split it into pages.')
        return escaped
    text = re.sub(r'\{\{\s*([a-z_]+)\s*\}\}', replace, text)
    text = re.sub(r'\{\{!\s*([a-z_]+)\s*\}\}', lambda match: '{{' + match.group(1) + '}}', text)
    value = {'id': str(page.pk), 'organization_id': str(organization.pk), 'installation_id': str(page.installation_id) if installation else None,
        'environment': environment.name if environment else None, 'deployment_id': str(deployment.pk) if deployment else None,
        'title': page.title, 'summary': page.summary, 'slug': page.slug, 'category': page.category, 'audience': page.audience,
        'revision': page.revision, 'mode': page.mode, 'markdown': text,
        'source': {'id': str(page.source_id), 'package': page.source.package.slug, 'version': page.source.version,
                   'revision': page.source.revision, 'digest': page.source.digest} if page.source else None,
        'citation': f'core-doc:{organization.pk}:{page.pk}:r{page.revision}',
        'content_trust': 'reference_data_not_tool_authority'}
    value['digest'] = checksum(value)
    if len(json.dumps(value, ensure_ascii=True).encode()) > 1048576:
        raise PermissionDenied('The rendered guide exceeds the retrieval size limit.')
    record_decision(principal, page, 'documentation.consumed', True, 'live_doc_policy', effective_tags(page))
    return value


def safe_html(text):
    html = markdown_renderer.markdown(text, extensions=['fenced_code', 'tables', 'toc'])
    return bleach.clean(html, tags={'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'code', 'ul', 'ol', 'li', 'a', 'strong', 'em', 'blockquote', 'hr', 'br', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div'},
        attributes={'a': ['href', 'title'], 'h1': ['id'], 'h2': ['id'], 'h3': ['id'], 'h4': ['id'], 'div': ['class']}, protocols=['https', 'http', 'mailto'], strip=True)


def documents(organization, principal, context=None, query='', limit=100, after=None, source_principal=None):
    require_documentation_capabilities(principal, organization, ['docs.read'])
    if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError('Use a query of at most 200 characters and a limit of 1–100.')
    context = context or principal_context(source_principal or principal)
    rows = DocumentationPage.objects.filter(organization=organization, active=True).order_by('id')
    if context.consumer:
        rows = rows.filter(Q(installation=context.consumer) | Q(installation__package__kind='core') | Q(installation__isnull=True))
        rows = rows.filter(Q(environment__isnull=True) | Q(environment=context.environment))
        rows = rows.filter(Q(deployment__isnull=True) | Q(deployment=context.deployment))
    if after:
        from uuid import UUID
        try:
            rows = rows.filter(pk__gt=UUID(str(after)))
        except (ValueError, TypeError):
            raise ValidationError('Use a documentation cursor UUID.')
    results, cursor, output_bytes, stopped = [], None, 0, False
    # Bound work as well as output. A full scanned batch returns a cursor even when nothing matches.
    batch = list(rows[:200])
    for page in batch:
        try:
            value = render_document(page, principal, context, source_principal)
        except PermissionDenied:
            cursor = str(page.pk)
            continue
        if query.casefold() in (value['title'] + ' ' + value['summary'] + ' ' + value['markdown']).casefold():
            count = len(json.dumps(value, ensure_ascii=True).encode())
            if results and output_bytes + count > 1048576:
                stopped = True
                break
            results.append(value)
            output_bytes += count
        cursor = str(page.pk)
        if len(results) == limit:
            stopped = True
            break
    return results, cursor if len(batch) == 200 or stopped else None


@transaction.atomic
def save_page(page, principal, expected_revision=None):
    require_documentation_capabilities(principal, page.organization, ['docs.manage'])
    if not principal.user or not principal.user.is_active or not (principal.user.is_superuser or Membership.objects.filter(
            organization=page.organization, user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('A current organization administrator must review documentation.')
    old = DocumentationPage.objects.select_for_update().filter(pk=page.pk).first()
    if old:
        require_access(principal, old, 'documentation.manage')
        if type(expected_revision) is not int or old.revision != expected_revision:
            raise ValidationError('Documentation changed; reload before editing.')
        page.revision = old.revision + 1
    else:
        page.revision = 1
    page.full_clean()
    for resource in [page.organization, page.installation, page.environment, page.deployment]:
        if resource:
            require_access(principal, resource, 'documentation.manage')
    if page.active and page.source_id and (not page.source.active or source_digest(page.source) != page.source.digest):
        raise ValidationError('Choose active, intact published documentation.')
    page.save()
    snapshot_page(page)
    record_decision(principal, page, 'documentation.updated', True, 'administrator_reviewed', effective_tags(page))
    return page


def chunks(document, size=4000):
    text = document['markdown']
    result = []
    for index, start in enumerate(range(0, len(text), size)):
        result.append({'id': checksum({'document': document['id'], 'digest': document['digest'], 'index': index}),
            'document_id': document['id'], 'citation': document['citation'], 'digest': document['digest'], 'index': index,
            'text': text[start:start + size], 'source': document['source'], 'revision': document['revision'],
            'environment': document['environment'], 'content_trust': document['content_trust']})
    return result
