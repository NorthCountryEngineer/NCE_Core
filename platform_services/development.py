"""Composable objects retain live authority, pinned recipes, and protected lineage."""
from copy import deepcopy
import re
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from identity.access import Principal, effective_tags, record_decision, require_access
from identity.models import Membership, Organization
from .ai_rules import RuleConflict, context, core_query, core_step, subject
from .data import transform
from .development_engine import operate, order, rows, shape
from .documentation import checksum, require_service_permissions
from .models import (AgentBinding, AgentDefinition, ApplicationIntegration, Dataset, DevelopmentObject,
    DevelopmentRevision, DevelopmentRun, Installation, Pipeline)
from .training import require_capability

RESOURCE_MODELS = {'dataset_id': Dataset, 'pipeline_id': Pipeline, 'integration_id': ApplicationIntegration,
    'agent_id': AgentDefinition, 'binding_id': AgentBinding}


def require_development(principal, organization, capability):
    require_capability(principal, organization, [capability])
    if not principal.credential:
        installation = Installation.objects.get(organization=organization, package__slug='core-workflows')
        require_service_permissions(principal, installation.service, [capability.replace('.', '-')])


def administrator(principal, organization):
    require_development(principal, organization, 'development.write')
    if (not principal.user or principal.application or principal.credential or principal.agent_binding or
            not principal.user.is_active or not principal.user.is_superuser and not Membership.objects.filter(
                organization=organization, user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('Publishing, sharing, and importing require a native organization administrator.')


def principals(reader, source):
    return [reader, source] if source is not None and source is not reader else [reader]


def authorize_draft(revision, reader, source=None):
    if subject(reader) != revision.author:
        administrator(reader, revision.development_object.organization)
    else:
        authorize_object(revision.development_object, reader, source, 'development.write', writing=True)


def require_placement(item, environment):
    from .models import DeploymentEnvironment
    boundaries = set(DeploymentEnvironment.objects.filter(organization=environment.organization,
        boundary_tag_id__in=[tag.pk for tag in effective_tags(item)]).values_list('pk', flat=True))
    if boundaries != {environment.pk}:
        raise PermissionDenied('Every source and inherited boundary must match the object environment.')


def authorize_object(obj, reader, source=None, capability='development.read', *, writing=False):
    obj = DevelopmentObject.objects.select_related('organization', 'installation__package', 'installation__service', 'environment').get(pk=obj.pk)
    if not obj.active or not obj.environment.active or not obj.installation.enabled or not obj.installation.service.active or not obj.installation.package.active:
        raise PermissionDenied('The object, owner, or environment is disabled.')
    require_placement(obj, obj.environment)
    for principal in principals(reader, source):
        require_development(principal, obj.organization, capability)
        require_access(principal, obj, 'development.object')
        consumer, environment = context(principal)
        if environment and environment.pk != obj.environment_id:
            raise PermissionDenied('Objects execute in their assigned environment.')
        if consumer and consumer.pk != obj.installation_id and (writing or not obj.consumers.filter(pk=consumer.pk, enabled=True, service__active=True, package__active=True).exists()):
            raise PermissionDenied('An administrator must share this object with the consuming application.')
    return obj


def resource(config, key, organization):
    try:
        scope = {'account__organization': organization} if key == 'binding_id' else {'organization': organization}
        return RESOURCE_MODELS[key].objects.get(pk=config[key], **scope)
    except (RESOURCE_MODELS[key].DoesNotExist, ValidationError, ValueError, TypeError):
        raise ValidationError('Resource references must resolve within this organization.')


def check_resources(obj, spec, reader, source):
    op, config = spec['operator'], spec['config']
    found = {key: resource(config, key, obj.organization) for key in RESOURCE_MODELS if key in config}
    for item in found.values():
        require_placement(item, obj.environment)
    for principal in principals(reader, source):
        for item in found.values():
            require_access(principal, item, 'development.resource')
            if hasattr(item, 'active') and not item.active:
                raise PermissionDenied('A development source is disabled.')
        if op == 'dataset':
            require_capability(principal, obj.organization, ['data.read'])
            if not principal.credential:
                service = Installation.objects.get(organization=obj.organization, package__slug='core-data').service
                require_service_permissions(principal, service, ['data-read'])
    if op == 'dataset' and checksum(found['dataset_id'].schema) != config['schema_digest']:
        raise PermissionDenied('The dataset schema changed. Review a new connection revision.')
    if op == 'pipeline' and checksum(found['pipeline_id'].transforms) != config['transforms_digest']:
        raise PermissionDenied('The pipeline transforms changed. Review a new revision.')
    if op == 'integration':
        from .integrations import authorize
        for principal in principals(reader, source):
            authorize(principal, found['integration_id'])
    if op == 'agent':
        from .agent_identity import binding_valid, require_capabilities
        binding, agent = found['binding_id'], found['agent_id']
        if (agent.kind not in {'inspect', 'select'} or agent.account_id != binding.account_id or
                binding.environment != obj.environment.name or not binding_valid(binding) or
                checksum({'kind': agent.kind, 'configuration': agent.configuration}) != config['definition_digest']):
            raise PermissionDenied('Use an active, approved inspect/select agent with a pinned definition.')
        consumer, _ = context(source or reader)
        if binding.installation_id != (consumer.pk if consumer else obj.installation_id):
            raise PermissionDenied('This agent approval belongs to another consuming application.')
        for principal in principals(reader, source):
            require_capability(principal, obj.organization, ['agents.run'])
            if not principal.credential:
                service = Installation.objects.get(organization=obj.organization, package__slug='core-agents').service
                require_service_permissions(principal, service, ['agents-run'])
        runner = Principal(agent_binding=binding, purpose=reader.purpose, request_id=reader.request_id)
        require_capabilities(runner, ['agents.run'])
        require_access(runner, agent, 'development.agent_definition')
    return found


def compile_spec(obj, spec, reader, source=None, *, published=True, path=(), budget=None):
    """Count expanded execution, not just distinct revisions, to bound reused subgraphs."""
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if budget[0] > 48 or len(path) > 8:
        raise ValidationError('A composition expands to at most 48 operators and eight nested graphs.')
    output = shape(spec, obj.kind)
    resources = check_resources(obj, spec, reader, source)
    dependencies, children = [], {}
    if spec['operator'] == 'graph':
        for node in spec['config']['nodes']:
            if not isinstance(node, dict) or not isinstance(node.get('revision_id'), str):
                raise ValidationError('Graph nodes pin revision UUIDs.')
            revision = load_revision(node['revision_id'], obj.organization)
            if revision.pk in path or revision.development_object.environment_id != obj.environment_id:
                raise ValidationError('Dependencies must be acyclic and in the same environment.')
            if published and not revision.published:
                raise PermissionDenied('Every dependency needs administrator publication.')
            if not revision.published:
                authorize_draft(revision, reader, source)
            authorize_object(revision.development_object, reader, source)
            require_placement(revision, obj.environment)
            for principal in principals(reader, source):
                require_access(principal, revision, 'development.revision')
            plan = compile_spec(revision.development_object, revision.spec, reader, source,
                published=published, path=(*path, revision.pk), budget=budget)
            if checksum(revision.spec) != revision.digest or set(revision.resources) != set(plan['resources']) or set(revision.dependencies.values_list('pk', flat=True)) != {d.pk for d in plan['dependencies']}:
                raise PermissionDenied('A pinned revision is corrupt.')
            revision._output_type = plan['output']
            children[str(revision.pk)] = revision
            dependencies.append(revision)
        ordered, output = order(spec, children)
    else:
        ordered = []
    return {'output': output, 'dependencies': dependencies, 'resources': resources, 'ordered': ordered}


def load_revision(pk, organization):
    try:
        return DevelopmentRevision.objects.select_related('development_object__organization', 'development_object__installation', 'development_object__environment').get(pk=pk, development_object__organization=organization)
    except (DevelopmentRevision.DoesNotExist, ValidationError, ValueError, TypeError):
        raise ValidationError('Choose a revision in this organization.')


@transaction.atomic
def create_object(organization, reader, values, source=None):
    required = {'slug', 'name', 'kind', 'installation_id', 'environment_id', 'spec'}
    if not isinstance(values, dict) or set(values) != required:
        raise ValidationError('Supply object identity, owner, environment, and recipe.')
    require_development(reader, organization, 'development.write')
    from .models import DeploymentEnvironment
    try:
        installation = Installation.objects.get(pk=values['installation_id'], organization=organization, enabled=True)
        environment = DeploymentEnvironment.objects.get(pk=values['environment_id'], organization=organization, active=True)
    except (Installation.DoesNotExist, DeploymentEnvironment.DoesNotExist, ValueError, TypeError, ValidationError):
        raise ValidationError('Choose an active owner and environment in this organization.')
    Organization.objects.select_for_update().get(pk=organization.pk)
    obj = DevelopmentObject(organization=organization, installation=installation, environment=environment,
        **{key: values[key] for key in ['slug', 'name', 'kind']})
    obj.full_clean()
    obj.save()
    save_revision(obj, reader, 0, values['spec'], source)
    return obj


@transaction.atomic
def save_revision(obj, reader, expected, spec, source=None):
    Organization.objects.select_for_update().get(pk=obj.organization_id)
    obj = authorize_object(obj, reader, source, 'development.write', writing=True)
    if type(expected) is not int or expected != obj.revision:
        raise RuleConflict
    # Drafts may refer to other drafts; publication and execution require reviewed dependencies.
    plan = compile_spec(obj, spec, reader, source, published=False)
    if len(str(spec).encode()) > 65536:
        raise ValidationError('Keep each recipe below 64 KiB.')
    obj.revision += 1
    obj.save(update_fields=['revision'])
    revision = DevelopmentRevision.objects.create(development_object=obj, number=obj.revision,
        spec=deepcopy(spec), digest=checksum(spec), resources={key: str(item.pk) for key, item in plan['resources'].items()}, author=subject(reader))
    revision.dependencies.set(plan['dependencies'])
    # Materialize lineage controls on this version; future recipe edits cannot remove them.
    tags = {tag.pk: tag for item in [*plan['resources'].values(), *plan['dependencies']] for tag in effective_tags(item)}
    revision.permission_tags.add(*tags)
    record_decision(reader, revision, 'development.draft_created', True, 'immutable_recipe_no_new_grants', effective_tags(revision))
    obj.revision = revision.number
    return revision


@transaction.atomic
def publish_revision(revision, reader, *, withdraw=False):
    obj = revision.development_object
    Organization.objects.select_for_update().get(pk=obj.organization_id)
    administrator(reader, obj.organization)
    authorize_object(obj, reader, capability='development.write')
    revision = load_revision(revision.pk, obj.organization)
    require_access(reader, revision, 'development.review')
    if checksum(revision.spec) != revision.digest:
        raise PermissionDenied('Corrupt revision.')
    if not withdraw:
        require_placement(revision, obj.environment)
        compile_spec(obj, revision.spec, reader)
    revision.published, revision.reviewer = not withdraw, subject(reader)
    revision.save(update_fields=['published', 'reviewer'])
    record_decision(reader, revision, 'development.withdrawn' if withdraw else 'development.published', True, 'native_administrator_review', effective_tags(revision))
    return revision


@transaction.atomic
def share_object(obj, reader, consumers, active):
    Organization.objects.select_for_update().get(pk=obj.organization_id)
    administrator(reader, obj.organization)
    obj = authorize_object(obj, reader, capability='development.write') if obj.active else obj
    require_access(reader, obj, 'development.sharing')
    if type(active) is not bool or not isinstance(consumers, list) or len(consumers) > 100:
        raise ValidationError('Use a boolean active state and at most 100 approved consuming installations.')
    resolved = []
    for pk in consumers:
        try:
            item = Installation.objects.get(pk=pk, organization=obj.organization, enabled=True, service__active=True, package__active=True)
        except (Installation.DoesNotExist, ValueError, TypeError, ValidationError):
            raise ValidationError('Consumers must be active installations in this organization.')
        require_access(reader, item, 'development.consumer_approval')
        resolved.append(item)
    obj.consumers.set(resolved)
    obj.active = active
    obj.save(update_fields=['active'])
    record_decision(reader, obj, 'development.sharing_changed', True, 'no_resource_or_role_grants_added', effective_tags(obj))
    return obj


def authorize_revision(revision, reader, source=None, capability='development.read'):
    revision = load_revision(revision.pk, revision.development_object.organization)
    obj = authorize_object(revision.development_object, reader, source, capability)
    require_placement(revision, obj.environment)
    for principal in principals(reader, source):
        require_access(principal, revision, 'development.revision')
    if not revision.published:
        authorize_draft(revision, reader, source)
    if checksum(revision.spec) != revision.digest:
        raise PermissionDenied('Corrupt recipe.')
    plan = compile_spec(obj, revision.spec, reader, source, published=revision.published)
    if revision.resources != {key: str(item.pk) for key, item in plan['resources'].items()} or set(revision.dependencies.values_list('pk', flat=True)) != {d.pk for d in plan['dependencies']}:
        raise PermissionDenied('Corrupt dependency or resource index.')
    return plan


def revision_document(revision, reader, source=None):
    revision = load_revision(revision.pk, revision.development_object.organization)
    plan = authorize_revision(revision, reader, source)
    obj = revision.development_object
    return {'organization_id': str(obj.organization_id), 'object_id': str(obj.pk), 'revision_id': str(revision.pk),
        'number': revision.number, 'name': obj.name, 'kind': obj.kind, 'environment': obj.environment.name,
        'installation_id': str(obj.installation_id), 'published': revision.published, 'digest': revision.digest,
        'spec': revision.spec, 'output_type': plan['output'], 'content_trust': 'recipe_data_not_tool_authority'}


def all_resources(revision):
    result, seen = [], set()
    def visit(item):
        if item.pk in seen:
            return
        seen.add(item.pk)
        result.extend([item, item.development_object])
        for key, pk in item.resources.items():
            if key in RESOURCE_MODELS:
                result.append(resource({key: pk if key not in {'pipeline_id', 'agent_id'} else int(pk)}, key, revision.development_object.organization))
        for dep in item.dependencies.all():
            visit(dep)
    visit(revision)
    return result


def execute_revision(revision, reader, source, inputs, run, lineage, path, protected):
    plan = authorize_revision(revision, reader, source, 'development.run')
    spec, config = revision.spec, revision.spec['config']
    if spec['operator'] == 'graph':
        def graph_callback():
            outputs = {}
            for node in plan['ordered']:
                child = load_revision(node['revision_id'], run.organization)
                arguments = {port: inputs[value[1:]] if value.startswith('$') else outputs[value] for port, value in node['inputs'].items()}
                outputs[node['name']] = execute_revision(child, reader, source, arguments, run, lineage, path + '/' + node['name'], protected)
            return outputs[config['output']]
        result = core_step(run.organization, reader, f'development:{run.pk}:{path}', graph_callback)
    else:
        found = plan['resources']
        def callback():
            if spec['operator'] == 'dataset':
                result, _ = core_query(run.organization, reader, config['limit'], lambda limit:
                    list(found['dataset_id'].records.order_by('id').values_list('data', flat=True)[:limit]))
                return rows(result)
            if spec['operator'] == 'integration':
                from .integrations import authorize, descriptor
                return descriptor(*authorize(reader, found['integration_id']))
            if spec['operator'] == 'pipeline':
                return rows([transform(row, found['pipeline_id'].transforms) for row in rows(inputs['rows'])])
            if spec['operator'] == 'agent':
                from .agent_identity import require_capabilities
                agent, binding = found['agent_id'], found['binding_id']
                runner = Principal(agent_binding=binding, purpose=reader.purpose, request_id=reader.request_id)
                require_capabilities(runner, ['agents.run'])
                # An agent receives nothing its live task, tags, and environment do not permit.
                for item in protected:
                    require_access(runner, item, 'development.agent_input')
                    if isinstance(item, Dataset):
                        require_capabilities(runner, ['data.read'])
                tool = lambda: rows(deepcopy(inputs['rows'])) if agent.kind == 'inspect' else rows([
                    {f: row[f] for f in agent.configuration['fields'] if f in row} for row in rows(inputs['rows'])])
                return core_step(run.organization, runner, f'development:{run.pk}:{path}:agent', tool)
            return operate(spec['operator'], config, inputs)
        result = core_step(run.organization, reader, f'development:{run.pk}:{path}', callback)
    lineage.append({'path': path, 'object_id': str(revision.development_object_id), 'revision_id': str(revision.pk),
        'digest': revision.digest, 'operator': spec['operator'], 'output_digest': checksum(result),
        **({'agent_account_id': str(plan['resources']['agent_id'].account_id)} if spec['operator'] == 'agent' else {})})
    return result


@transaction.atomic
def run_revision(revision, reader, values, source=None):
    obj = revision.development_object
    Organization.objects.select_for_update().get(pk=obj.organization_id)
    revision = load_revision(revision.pk, obj.organization)
    authorize_revision(revision, reader, source, 'development.run')
    if not revision.published:
        raise PermissionDenied('Publish a reviewed recipe before execution.')
    if not isinstance(values, dict) or set(values) != {'request_key', 'inputs'} or not isinstance(values['request_key'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', values['request_key']):
        raise ValidationError('Supply request_key and typed inputs.')
    inputs = values['inputs']
    if not isinstance(inputs, dict) or set(inputs) != set(revision.spec['inputs']):
        raise ValidationError('Supply exactly the recipe’s declared inputs.')
    # Model artifacts enter through a pinned training revision, preserving its provenance.
    if any(t != 'rows' for t in revision.spec['inputs'].values()):
        raise ValidationError('External inputs are records only. Compose model/connection ports inside a graph.')
    for value in inputs.values():
        rows(value)
    consumer, _ = context(source or reader)
    fingerprint = checksum({'revision_id': str(revision.pk), 'digest': revision.digest, 'inputs': inputs})
    old = DevelopmentRun.objects.filter(organization=obj.organization, subject=subject(reader), installation=consumer, request_key=values['request_key']).first()
    if old:
        if old.fingerprint != fingerprint:
            raise RuleConflict
        authorize_run(old, reader, source)
        return old
    run = DevelopmentRun.objects.create(organization=obj.organization, object_revision=revision, environment=obj.environment,
        installation=consumer, subject=subject(reader), request_key=values['request_key'], fingerprint=fingerprint)
    for principal in principals(reader, source):
        require_access(principal, run, 'development.execute')
    # Bounded local execution commits its result and all quota usage together. There are no remote side effects.
    lineage = []
    result = execute_revision(revision, reader, source, deepcopy(inputs), run, lineage, 'root', all_resources(revision))
    if len(str(result).encode()) > 1048576:
        raise ValidationError('The experiment output exceeds 1 MiB.')
    run.status, run.result, run.lineage, run.completed = 'completed', {'type': authorize_revision(revision, reader, source)['output'], 'value': result}, lineage, timezone.now()
    run.save(update_fields=['status', 'result', 'lineage', 'completed'])
    record_decision(reader, run, 'development.completed', True, 'bounded_local_execution', effective_tags(run))
    return run


def authorize_run(run, reader, source=None):
    revision = load_revision(run.object_revision_id, run.organization)
    if not revision.published:
        raise PermissionDenied('The run recipe has been withdrawn.')
    authorize_revision(revision, reader, source)
    consumer, _ = context(source or reader)
    if run.subject != subject(reader) or run.installation_id != (consumer.pk if consumer else None):
        administrator(reader, run.organization)
    for principal in principals(reader, source):
        require_access(principal, run, 'development.run_read')


def run_document(run, reader, source=None):
    authorize_run(run, reader, source)
    return {'id': str(run.pk), 'organization_id': str(run.organization_id), 'revision_id': str(run.object_revision_id),
        'status': run.status, 'environment': run.environment.name, 'result': run.result, 'lineage': run.lineage,
        'created': run.created.isoformat(), 'completed': run.completed.isoformat() if run.completed else None,
        'engine': 'bounded_local_development_v1'}


def export_bundle(revision, reader, source=None):
    """Portable recipes omit data, credentials, account approvals, tags, and sharing grants."""
    revision = load_revision(revision.pk, revision.development_object.organization)
    authorize_revision(revision, reader, source)
    entries, refs = {}, {}
    def visit(item):
        key = str(item.pk)
        if key in entries:
            return
        authorize_revision(item, reader, source)
        spec = deepcopy(item.spec)
        for field, pk in item.resources.items():
            placeholder = f'{field}:{pk}'
            refs[placeholder] = {'kind': field, 'binding_required': True}
            spec['config'][field] = placeholder
        obj = item.development_object
        entries[key] = {'key': key, 'kind': obj.kind, 'name': obj.name, 'spec': spec}
        for dep in item.dependencies.all():
            visit(dep)
    visit(revision)
    bundle = {'format': 'core-development-bundle-v1', 'root': str(revision.pk), 'objects': list(entries.values()),
        'resources': refs, 'import_policy': 'new_drafts_require_local_bindings_and_administrator_review'}
    bundle['digest'] = checksum(bundle)
    return bundle


@transaction.atomic
def import_bundle(organization, reader, values):
    administrator(reader, organization)
    if not isinstance(values, dict) or set(values) != {'bundle', 'bindings', 'installation_id', 'environment_id', 'prefix'}:
        raise ValidationError('Supply a bundle, explicit local resource bindings, owner, environment, and slug prefix.')
    bundle, bindings, prefix = deepcopy(values['bundle']), values['bindings'], values['prefix']
    if not isinstance(bundle, dict) or set(bundle) != {'format', 'root', 'objects', 'resources', 'import_policy', 'digest'}:
        raise ValidationError('Invalid portable development bundle.')
    expected = bundle.pop('digest')
    if checksum(bundle) != expected or bundle['format'] != 'core-development-bundle-v1' or bundle['import_policy'] != 'new_drafts_require_local_bindings_and_administrator_review':
        raise ValidationError('Bundle integrity or format mismatch.')
    if not isinstance(bindings, dict) or not isinstance(bundle['resources'], dict) or set(bindings) != set(bundle['resources']) or not isinstance(prefix, str) or not re.fullmatch('[a-z][a-z0-9-]{0,39}', prefix):
        raise ValidationError('Bind every external resource explicitly and use a short slug prefix.')
    objects = bundle['objects']
    if not isinstance(objects, list) or not 1 <= len(objects) <= 48:
        raise ValidationError('Import at most 48 object revisions.')
    pending, created = {}, {}
    for entry in objects:
        if not isinstance(entry, dict) or set(entry) != {'key', 'kind', 'name', 'spec'} or not isinstance(entry['key'], str) or entry['key'] in pending:
            raise ValidationError('Bundle object keys must be unique.')
        pending[entry['key']] = entry
    if not isinstance(bundle['root'], str) or bundle['root'] not in pending:
        raise ValidationError('Bundle root is missing.')
    while pending:
        progress = False
        for key, entry in list(pending.items()):
            spec = deepcopy(entry['spec'])
            if not isinstance(spec, dict) or not isinstance(spec.get('config'), dict):
                raise ValidationError('Invalid recipe in bundle.')
            if spec.get('operator') == 'graph':
                nodes = spec['config'].get('nodes')
                if not isinstance(nodes, list) or any(not isinstance(n, dict) or not isinstance(n.get('revision_id'), str) for n in nodes):
                    raise ValidationError('Invalid graph in bundle.')
                if any(node['revision_id'] not in created for node in nodes):
                    continue
                for node in nodes:
                    child = created[node['revision_id']].versions.get(number=1)
                    node['revision_id'], node['digest'] = str(child.pk), child.digest
            for field in RESOURCE_MODELS:
                if field in spec['config']:
                    placeholder = spec['config'][field]
                    if not isinstance(placeholder, str) or bundle['resources'].get(placeholder) != {'kind': field, 'binding_required': True}:
                        raise ValidationError('Imported resource references require declared bindings.')
                    spec['config'][field] = bindings[placeholder]
                    item = resource(spec['config'], field, organization)
                    require_access(reader, item, 'development.import_binding')
                    # Re-pin the explicitly selected local definition, never import a foreign approval.
                    if field == 'dataset_id':
                        spec['config']['schema_digest'] = checksum(item.schema)
                    if field == 'pipeline_id':
                        spec['config']['transforms_digest'] = checksum(item.transforms)
                    if field == 'agent_id':
                        spec['config']['definition_digest'] = checksum({'kind': item.kind, 'configuration': item.configuration})
            obj = create_object(organization, reader, {'slug': f'{prefix}-{len(created)+1}', 'name': entry['name'], 'kind': entry['kind'],
                'installation_id': values['installation_id'], 'environment_id': values['environment_id'], 'spec': spec})
            created[key] = obj
            del pending[key]
            progress = True
        if not progress:
            raise ValidationError('The bundle has cyclic or missing dependencies.')
    return created[bundle['root']], list(created.values())
