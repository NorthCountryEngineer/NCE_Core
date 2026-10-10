"""Provider-neutral app development. Prompts/specifications never execute as code."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from identity.access import Principal, effective_tags, record_decision, require_access
from identity.models import Membership, ProtectedResource, Role, ServicePermission
from .models import AgentBinding, AgentDefinition, AgentListing, AgentTask, AppProject, Installation, MarketplacePackage
from .training import require_capability

ROLES = ['plan', 'backend', 'frontend', 'security', 'test', 'package']
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / 'app_factory_templates'
DEFAULT_SPEC = {'resources': [{'slug': 'items', 'name': 'Items', 'fields': {'title': 'string', 'status': 'string'}}]}


def validate_specification(value):
    if not isinstance(value, dict) or set(value) != {'resources'} or not isinstance(value['resources'], list) or not 1 <= len(value['resources']) <= 8:
        raise ValidationError('Use a resources list with 1–8 collections.')
    slugs = set()
    for resource in value['resources']:
        if not isinstance(resource, dict) or set(resource) != {'slug', 'name', 'fields'}:
            raise ValidationError('Each collection requires slug, name, and fields.')
        slug, name, fields = resource['slug'], resource['name'], resource['fields']
        if not isinstance(slug, str) or not re.fullmatch('[a-z][a-z0-9-]{0,29}', slug) or slug in slugs:
            raise ValidationError('Collection slugs must be unique lowercase names of at most 30 characters.')
        slugs.add(slug)
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValidationError('Collection names must have 1–100 characters.')
        if not isinstance(fields, dict) or not 1 <= len(fields) <= 20 or any(
                not isinstance(key, str) or not re.fullmatch('[a-z][a-z0-9_]{0,29}', key) or
                not isinstance(kind, str) or kind not in {'string', 'integer', 'boolean'} for key, kind in fields.items()):
            raise ValidationError('Use 1–20 named scalar fields: string, integer, or boolean.')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def specification_digest(project):
    return digest({'project_id': str(project.pk), 'name': project.name, 'description': project.description, 'specification': project.specification,
                   'organization': str(project.organization_id), 'environment': str(project.environment_id)})


def authorize(project, principal, capability='appfactory.read', human=False):
    project = AppProject.objects.select_related('organization', 'environment').get(pk=project.pk)
    require_capability(principal, project.organization, [capability])
    require_access(principal, project, capability)
    if not project.active or not project.environment.active:
        raise PermissionDenied('The development request is disabled.')
    if human and (not principal.user or not principal.user.is_active or not (principal.user.is_superuser or
            Membership.objects.filter(organization=project.organization, user=principal.user, active=True, kind='administrator').exists())):
        raise PermissionDenied('A current human administrator must approve application builds and releases.')
    return project


@transaction.atomic
def create_project(organization, environment, name, description, specification, principal):
    require_capability(principal, organization, ['appfactory.create'])
    require_access(principal, environment, 'appfactory.create')
    if not isinstance(description, str) or not 1 <= len(description) <= 12000 or not isinstance(name, str) or not 1 <= len(name) <= 160:
        raise ValidationError('Use a name and an application description of at most 12,000 characters.')
    project = AppProject(organization=organization, environment=environment, name=name, description=description,
                         specification=deepcopy(specification))
    project.full_clean()
    project.save()
    record_decision(principal, project, 'appfactory.created', True, 'specification_requires_review', effective_tags(project))
    return project


@transaction.atomic
def approve_build(project, principal):
    project = AppProject.objects.select_for_update().get(pk=project.pk)
    authorize(project, principal, 'appfactory.build', human=True)
    if project.build_id:
        return project.build
    project.full_clean()
    consumer = Installation.objects.get(organization=project.organization, package__slug='core-app-factory')
    task = AgentTask(organization=project.organization, installation=consumer, environment=project.environment.name,
        name=f'Build {project.name}'[:160], description='Build only this reviewed specification. No shell, network, customer data, billing, or deployment tools.',
        resources={'app_projects': [str(project.pk)]}, expires_at=timezone.now() + timedelta(hours=24), approved_by=principal.user)
    task.full_clean()
    task.save()
    tags = effective_tags(project)
    task.permission_tags.set(tags)
    agents = []
    for kind in ROLES:
        listing = AgentListing.objects.get(slug=f'app-developer-{kind}', active=True)
        agent = AgentDefinition(organization=project.organization, slug=f'build-{project.pk.hex[:20]}-{kind}',
            name=f'{project.name}: {kind}'[:160], kind=f'app_{kind}',
            configuration={'project_id': str(project.pk), 'spec_digest': specification_digest(project)})
        agent.full_clean()
        agent.save()
        agent.account.listing = listing
        agent.account.listing_version = listing.version
        agent.account.save(update_fields=['listing', 'listing_version'])
        agent.permission_tags.set(tags)
        role, _ = Role.objects.get_or_create(organization=project.organization, slug=f'build-{project.pk.hex[:20]}-{kind}', defaults={'name': f'App developer: {kind}'})
        # Exact grants only. Separate per-step roles; none includes release or administration.
        role.permissions.set(ServicePermission.objects.filter(service__organization=project.organization,
            service__slug__in=['core-agents', 'core-app-factory'], codename__in=['agents-run', 'appfactory-build']))
        binding = AgentBinding(account=agent.account, installation=consumer, environment=task.environment, task=task)
        binding.full_clean()
        binding.save()
        binding.roles.add(role)
        binding.access_tags.set(tags)
        agents.append(agent.slug)
    from .workflows import enqueue, snapshot_agents
    run, _ = enqueue(project.organization, f'app-build:{project.pk}', project.name, 'workflow',
        snapshot_agents(project.organization, agents), {'project_id': str(project.pk)}, actor=principal.user,
        principal=principal, environment=task.environment, task=task)
    project.build, project.approved_by = run, principal.user
    project.save(update_fields=['build', 'approved_by'])
    record_decision(principal, project, 'appfactory.build_approved', True, 'six_scoped_accounts', tags)
    return run


def execute_step(organization, step, data, principal, event_id):
    project = AppProject.objects.get(organization=organization, pk=step['configuration']['project_id'])
    authorize(project, principal, 'appfactory.build')
    if specification_digest(project) != step['configuration']['spec_digest'] or data.get('project_id') != str(project.pk):
        raise PermissionDenied('The approved specification does not match this build.')
    role = step['kind'].removeprefix('app_')
    if role not in ROLES or not principal.agent_binding or principal.agent_binding.account.definition.kind != step['kind']:
        raise PermissionDenied('A development tool requires its approved role account.')
    index = ROLES.index(role)
    if not project.build_id or event_id != f'run:{project.build_id}:step:{index}' or project.build.snapshot[index]['slug'] != step['slug']:
        raise PermissionDenied('Development tools execute only inside the approved project build.')
    expected_input = project.build.input if index == 0 else project.build.step_results.get(index=index - 1).output
    if data != expected_input or (index == 0 and set(data) != {'project_id'}):
        raise PermissionDenied('Development stages cannot accept caller-provided source files or predecessor output.')
    result = deepcopy(data)
    completed = result.get('completed_roles', [])
    if completed != ROLES[:ROLES.index(role)]:
        raise ValidationError('Development stages must run in their approved order.')
    files = result.setdefault('files', {})
    if role == 'plan':
        validate_specification(project.specification)
        spec = {'name': project.name, 'resources': project.specification['resources'],
                'organization_id': str(project.organization_id), 'environment': project.environment.name}
        files['app-spec.json'] = json.dumps(spec, indent=2, ensure_ascii=True) + '\n'
        files['REQUEST.md'] = project.description + '\n'
        result['spec_digest'] = specification_digest(project)
    elif role == 'backend':
        files['server.py'] = (TEMPLATES / 'server.py').read_text()
        files['identity_client.py'] = (ROOT / 'identity_client.py').read_text()
        files['integration_client.py'] = (ROOT / 'integration_client.py').read_text()
        files['core_client.py'] = (ROOT / 'core_client.py').read_text()
        files['header_client.py'] = (ROOT / 'header_client.py').read_text()
        files['documentation_client.py'] = (ROOT / 'documentation_client.py').read_text()
        files['ai_rules_client.py'] = (ROOT / 'ai_rules_client.py').read_text()
    elif role == 'frontend':
        files['index.html'] = (TEMPLATES / 'index.html').read_text()
        files['ui.js'] = (TEMPLATES / 'ui.js').read_text()
        files['ui.css'] = '\n'.join(path.read_text() for path in [ROOT / 'templates/identity/design-system.css.html', TEMPLATES / 'ui.css',
            ROOT / 'templates/identity/mobile-foundation.css.html', ROOT / 'templates/identity/accessibility.css.html'])
        files['header.js'] = (ROOT / 'platform_services/assets/header.js').read_text()
    elif role == 'security':
        files['security-contract.json'] = json.dumps({'version': 1, 'identity': 'Core OIDC/OAuth2 with live introspection',
            'permissions': ['app-read', 'app-write'], 'resource_type': 'app_collection',
            'permission_tags': sorted(tag.slug for tag in effective_tags(project)),
            'fail_closed': True, 'audit': 'Core policy decisions plus local metadata-only operation accounting',
            'ai_rules': 'Inherited Core constraints and shared native-user/agent reservations before reads/creates',
            'rules_capabilities': ['rules.read', 'usage.reserve', 'usage.report'],
            'model_code_execution': False, 'production_requires_review': True}, indent=2) + '\n'
    elif role == 'test':
        files['test_server.py'] = (TEMPLATES / 'test_server.py').read_text()
        result['verification'] = 'Tests generated; execute them in your CI or local sandbox before release.'
    elif role == 'package':
        for name in ['Dockerfile', 'README.md']:
            files[name] = (TEMPLATES / name).read_text()
        guide = ('# {{application_name}} for {{organization_name}}\n\n'
            'Sign in through **{{platform_name}}** at your administrator-provided application endpoint. '
            'Support: {{support_email}}. This guide describes the reviewed generated specification.\n\n'
            '## Collections and APIs\n\n'
            'Use the collection selector and typed create form in the application. Read operations require '
            '`app-read`; create operations require `app-write`. Every collection is checked against its '
            'live Core `app_collection` policy and agent task scope.\n\n')
        for resource in project.specification['resources']:
            guide += f"### Collection `{resource['slug']}`\n\n"
            guide += f"`GET /api/records/{resource['slug']}` reads records; `POST /api/records/{resource['slug']}` creates a record.\n\n"
            guide += '| Field | Type |\n| --- | --- |\n'
            guide += ''.join(f'| `{field}` | {kind} |\n' for field, kind in sorted(resource['fields'].items())) + '\n'
        guide += '## Deployment and operation\n\n' + files['README.md']
        guide += ('\n## Organization context\n\nEnvironment: {{environment_name}}. Namespace: {{namespace}}. '
            'Endpoint: {{application_origin}}. Installed version: {{application_version}}.\n\n'
            'The platform hosts this versioned guide and organization extensions. Access requires '
            'Core `docs-read` and application `docs-read`; business operations remain separately authorized. '
            'Use the included `documentation_client.py` for approved platform retrieval.\n')
        files['APPLICATION.md'] = guide
        for name in ['pyproject.toml', 'uv.lock']:
            files[name] = (ROOT / name).read_text()
        files['.dockerignore'] = '.git\n.venv\n__pycache__\n*.sqlite3\nsecrets\n.env\nartifacts\n'
        files['ci/check.sh'] = '#!/usr/bin/env bash\nset -euo pipefail\nuv sync --frozen\n.venv/bin/python ci/source_digest.py\n.venv/bin/python -m unittest test_server -v\n'
        files['.github/workflows/verify.yml'] = 'name: Verify generated app\non:\n  workflow_dispatch:\njobs:\n  verify:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n      - run: pip install uv==0.12.19\n      - run: bash ci/check.sh\n'
        names = sorted([*files, 'ci/source_digest.py'])
        files['ci/source_digest.py'] = ("import hashlib, json\nfrom pathlib import Path\n"
            "root = Path(__file__).resolve().parent.parent\n"
            f"files = {{name: (root / name).read_text() for name in {names!r}}}\n"
            "print(hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest())\n")
        result['source_digest'] = digest(files)
    result['completed_roles'] = [*completed, role]
    return result


def artifacts(project, principal):
    project = authorize(project, principal)
    run = project.build
    if not run or run.organization_id != project.organization_id or run.status != 'succeeded' or not run.output or run.output.get('project_id') != str(project.pk) or run.output.get('completed_roles') != ROLES:
        raise ValidationError('A successful six-stage build is required.')
    # Recheck every pinned account/tool even after completion; revoked approvals cannot export source.
    from .workflows import run_allowed
    if not run_allowed(run) or run.output.get('spec_digest') != specification_digest(project):
        raise PermissionDenied('Build authorization has been revoked.')
    files = run.output['files']
    if digest(files) != run.output['source_digest']:
        raise ValidationError('Build artifacts failed integrity verification.')
    return files, run.output['source_digest']


def source_archive(project, principal):
    files, source_hash = artifacts(project, principal)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(files.items()):
            archive.writestr(name, contents)
    record_decision(principal, project, 'appfactory.source_downloaded', True, 'authorized_source_export', effective_tags(project))
    return stream.getvalue(), source_hash


@transaction.atomic
def release(project, principal, image, source_hash, verification_reference):
    project = AppProject.objects.select_for_update().get(pk=project.pk)
    authorize(project, principal, 'appfactory.release', human=True)
    files, expected = artifacts(project, principal)
    if source_hash != expected or not isinstance(verification_reference, str) or not 1 <= len(verification_reference) <= 500:
        raise ValidationError('Supply the exact source digest and a reviewed successful CI/test evidence reference.')
    if project.package_id:
        if project.package.manifest['image'] != image or project.source_digest != source_hash:
            raise ValidationError('A released request cannot be assigned another image.')
        return project.package
    package = MarketplacePackage(organization=project.organization, slug=f'app-{project.pk.hex}', name=project.name,
        version='0.1.0', description=project.description,
        kind='application', manifest={'image': image, 'port': 8080, 'capabilities': ['docs.read', 'rules.read', 'usage.reserve', 'usage.report', 'chat.read', 'chat.send'], 'dependencies': ['core-platform'],
                                    'callback_path': '/oauth/callback', 'health_path': '/health'})
    package.full_clean()
    package.save()
    from .documentation import publish_source
    guide = files.get('APPLICATION.md') or files.get('README.md')
    if not guide:
        raise ValidationError('The reviewed source archive must include application documentation.')
    publish_source(package, {'version': package.version, 'slug': 'application-guide', 'title': project.name[:154] + ' guide',
        'summary': 'Reviewed generated collections, APIs, security, deployment, and organization support.',
        'category': 'user-guide', 'audience': 'users', 'required_permissions': [], 'markdown': guide}, principal)
    from .services import install_package
    installation = install_package(project.organization, package, actor=principal.user)
    installation.permission_tags.set(effective_tags(project))
    installation.service.permission_tags.set(project.permission_tags.all())
    for codename in ['app-read', 'app-write']:
        ServicePermission.objects.get_or_create(service=installation.service, codename=codename, defaults={'name': codename})
    for resource in project.specification['resources']:
        protected = ProtectedResource.objects.create(service=installation.service, resource_type='app_collection',
            external_id=resource['slug'], name=resource['name'])
        protected.permission_tags.set(effective_tags(project))
    project.package, project.source_digest, project.verification_reference = package, source_hash, verification_reference
    project.save(update_fields=['package', 'source_digest', 'verification_reference'])
    record_decision(principal, project, 'appfactory.released', True, 'human_reviewed_image_and_test_evidence', effective_tags(project))
    return package
