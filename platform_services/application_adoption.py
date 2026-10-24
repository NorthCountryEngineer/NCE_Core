"""Operator-only, reviewed ownership adoption; never provisions or contacts a provider.

This deliberately supports the external application graph, not arbitrary tenant
merging. Unknown consumer relationships are blockers, so an operator cannot
silently strand an integration, agent approval, or federation agreement.
"""
import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from oauth2_provider.models import (get_access_token_model, get_grant_model,
    get_id_token_model, get_refresh_token_model)

from identity.models import (ApplicationAccountAccess, AuditEvent, AuthenticationPolicy, Membership,
    Organization, PermissionTag, Role, User)
from .cloud_models import CloudApplication, CloudHostingAssignment
from .models import (ApplicationPin, Deployment, DeploymentEnvironment,
    DocumentationPage, Installation, MarketplacePackage, WorkloadCredential)


def _digest(value):
    return hashlib.sha256(json.dumps(value, cls=DjangoJSONEncoder,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _row(value):
    # Only fingerprints leave this function: hashes/ciphertext/configuration,
    # OAuth client secrets and private manifests never appear in a plan.
    fields = {f.attname: getattr(value, f.attname) for f in value._meta.concrete_fields}
    fields['_m2m'] = {f.name: sorted(str(pk) for pk in getattr(value, f.name).values_list('pk', flat=True))
        for f in value._meta.many_to_many}
    return {'model': value._meta.label_lower, 'id': str(value.pk), 'fingerprint': _digest(fields)}


def _authorize(actor, source, target):
    if not actor.is_active or not source.active or not target.active:
        raise PermissionDenied('An active operator and active source and target organizations are required.')
    if not actor.is_superuser and Membership.objects.filter(user=actor, organization__in=[source, target],
            active=True, kind='administrator').count() != 2:
        raise PermissionDenied('The operator must be a superuser or an active administrator of both organizations.')


def _tag_values(tag):
    return {f.name: getattr(tag, f.name) for f in PermissionTag._meta.concrete_fields
        if f.name not in {'id', 'organization'}}


def _related_blockers(resource, allowed):
    blockers, related = [], []
    for relation in resource._meta.related_objects:
        name = relation.get_accessor_name()
        if not name or name == '+':
            continue
        # Query the relation directly rather than following a cached reverse
        # one-to-one descriptor. Reverse M2M relations use the same filter.
        query = relation.related_model.objects.filter(**{relation.field.name: resource})
        if relation.related_model._meta.label_lower in allowed:
            continue
        rows = list(query.order_by('pk'))
        if rows:
            blockers.append({'code': 'unsupported_dependency', 'resource': resource._meta.label_lower,
                'resource_id': str(resource.pk), 'relation': name,
                'model': relation.related_model._meta.label_lower, 'ids': [str(row.pk) for row in rows]})
            related.extend(rows)
    return blockers, related


def _collect(application, target, environment, actor, member_user_ids):
    source = application.organization
    provider_environment = application.environment
    _authorize(actor, source, target)
    blockers, state = [], [application, source, target, application.platform, application.environment, environment, actor]

    def block(code, **details):
        blockers.append({'code': code, **details})

    policies = {p.organization_id: p for p in AuthenticationPolicy.objects.filter(organization__in=[source, target])}
    state += list(policies.values())
    policy_defaults = {'require_federated': False, 'require_mfa': False, 'max_session_age': 28800}
    source_policy = {key: getattr(policies.get(source.pk), key, value) for key, value in policy_defaults.items()}
    target_policy = {key: getattr(policies.get(target.pk), key, value) for key, value in policy_defaults.items()}
    if (any(source_policy[key] and not target_policy[key] for key in ['require_federated', 'require_mfa']) or
            target_policy['max_session_age'] > source_policy['max_session_age']):
        block('target_authentication_policy_weaker')

    if source.pk == target.pk:
        block('same_organization')
    if environment.organization_id != target.pk or not environment.active or environment.name != application.environment.name:
        block('target_environment_mismatch')
    if not application.active or not application.installation_id:
        block('active_installed_application_required')
    if CloudHostingAssignment.objects.filter(application=application).exists():
        block('already_has_hosting_assignment')
    if application.platform.organization_id != source.pk:
        block('source_platform_ownership_mismatch')
    if CloudApplication.objects.filter(organization=target, slug=application.slug).exists():
        block('target_application_slug_conflict')
    if not application.installation_id:
        return {'blockers': blockers, 'state': state}
    installation = application.installation
    service, package = installation.service, installation.package
    deployments = list(installation.deployments.select_related('environment').order_by('pk'))
    releases = list(application.releases.order_by('pk'))
    state += [installation, service, package, *deployments, *releases]
    target_parents = list(Deployment.objects.filter(installation__organization=target,
        installation__package__slug='core-platform', installation__enabled=True, installation__service__active=True,
        target='embedded', desired_state='running').order_by('pk'))
    target_parent = target_parents[0] if len(target_parents) == 1 else None
    parent_mapping = {}
    for deployment in deployments:
        if deployment.parent_id:
            parent = deployment.parent
            state += [parent, parent.installation, parent.installation.service]
            if (parent.target != 'embedded' or parent.installation.organization_id != source.pk or
                    parent.installation.package.slug != 'core-platform' or not target_parent):
                block('unsupported_deployment_parent', id=str(deployment.pk))
            else:
                parent_mapping[deployment.pk] = target_parent
    if target_parent:
        state += [target_parent, target_parent.installation, target_parent.installation.service]
    if installation.organization_id != source.pk or service.organization_id != source.pk:
        block('source_installation_ownership_mismatch')
    if Installation.objects.filter(organization=target, package=package).exists():
        block('target_package_already_installed')
    if target.services.filter(slug=service.slug).exists():
        block('target_service_slug_conflict')
    if any(r.status not in {'succeeded', 'failed', 'cancelled'} for r in releases):
        block('unfinished_releases', ids=[str(r.pk) for r in releases if r.status not in {'succeeded', 'failed', 'cancelled'}])
    if not deployments or any(d.target != 'external' or d.environment_id != application.environment_id
            or d.node_id for d in deployments):
        block('external_single_environment_required')
    if CloudApplication.objects.filter(installation=installation).exclude(pk=application.pk).exists():
        block('installation_shared_by_cloud_applications')
    private_package = package.organization_id is not None
    if private_package and (package.organization_id != source.pk or
            Installation.objects.filter(package=package).exclude(pk=installation.pk).exists()):
        block('private_package_shared_or_foreign')

    permissions = list(service.permissions.order_by('pk'))
    clients = list(service.applications.order_by('pk'))
    resources = list(service.protected_resources.order_by('pk'))
    pages = list(DocumentationPage.objects.filter(Q(installation=installation) | Q(deployment__in=deployments)).distinct().order_by('pk'))
    credentials = list(WorkloadCredential.objects.filter(installation=installation).order_by('pk'))
    accounts = list(ApplicationAccountAccess.objects.filter(deployment__in=deployments).order_by('pk'))
    pins = list(ApplicationPin.objects.filter(deployment__in=deployments).order_by('pk'))
    roles = list(Role.objects.filter(permissions__in=permissions).distinct().order_by('pk'))
    state += [*permissions, *clients, *resources, *pages, *credentials, *accounts, *pins, *roles]
    state += [revision for page in pages for revision in page.revisions.order_by('pk')]
    expected_clients = {c for d in deployments for c in [d.application_id, d.resource_application_id] if c}
    if {c.pk for c in clients} != expected_clients:
        block('unbound_oauth_clients')
    token_models = [get_access_token_model(), get_refresh_token_model(), get_grant_model(), get_id_token_model()]
    oauth_rows = [row for model in token_models for row in model.objects.filter(application__in=clients).order_by('pk')]
    state += oauth_rows
    if any(p.organization_id != source.pk or p.environment_id not in {None, application.environment_id} for p in pages):
        block('documentation_placement_mismatch')
    if any(c.environment_id != application.environment_id or c.deployment_id not in {d.pk for d in deployments} for c in credentials):
        block('credential_placement_mismatch')
    if any(r.organization_id != source.pk for r in roles):
        block('foreign_permission_role')
    for resource, allowed in [(installation, {'platform_services.cloudapplication', 'platform_services.deployment',
            'platform_services.documentationpage', 'platform_services.workloadcredential'}),
            (service, {'platform_services.installation', 'identity.servicepermission', 'identity.application', 'identity.protectedresource'}),
            *[(d, {'identity.applicationaccountaccess', 'platform_services.applicationpin', 'platform_services.documentationpage',
                'platform_services.workloadcredential'}) for d in deployments],
            *[(client, {'platform_services.deployment', 'identity.applicationconsent',
                *[model._meta.label_lower for model in token_models]}) for client in clients],
            *[(resource, set()) for resource in resources],
            *[(credential, set()) for credential in credentials],
            *[(role, {'identity.membership', 'identity.servicepermission'}) for role in roles],
            *[(page, {'platform_services.documentationrevision'}) for page in pages]]:
        found, rows = _related_blockers(resource, allowed)
        blockers += found
        state += rows
    if private_package:
        found, rows = _related_blockers(package, {'platform_services.installation', 'platform_services.documentationsource'})
        blockers += found
        state += rows
    # Agent task scopes use JSON UUID lists, not foreign keys. Refuse dependent
    # approvals owned by another installation instead of moving their authority.
    from .models import AgentDefinition, AgentTask
    moved_ids = {str(o.pk) for o in [application, installation, service, *deployments, *resources, *pages]}
    for model, field in [(AgentDefinition, 'configuration'), (AgentTask, 'resources')]:
        for item in model.objects.filter(organization=source).order_by('pk'):
            if any(value in json.dumps(getattr(item, field), cls=DjangoJSONEncoder) for value in moved_ids):
                block('dependent_agent_scope', model=item._meta.label_lower, id=str(item.pk))
                state.append(item)

    explicit = set(str(v) for v in member_user_ids)
    selected_memberships = set(a.membership_id for a in [*accounts, *pins])
    selected_memberships.update(Membership.objects.filter(organization=source, roles__in=roles).values_list('pk', flat=True))
    selected_memberships.update(Membership.objects.filter(organization=source, user_id__in=explicit).values_list('pk', flat=True))
    memberships = list(Membership.objects.filter(pk__in=selected_memberships).select_related('user').order_by('pk'))
    if explicit - {str(m.user_id) for m in memberships}:
        block('explicit_members_must_belong_to_source')
    if any(m.organization_id != source.pk for m in memberships):
        block('foreign_application_membership')
    state += memberships + [m.user for m in memberships]
    supporting_roles = list(Role.objects.filter(organization=source, memberships__in=memberships).distinct().order_by('pk'))
    state += supporting_roles
    target_memberships = list(Membership.objects.filter(organization=target).order_by('pk'))
    state += target_memberships
    if any(not m.active or not m.user.is_active for m in memberships if str(m.user_id) in explicit):
        block('explicit_members_must_be_active')
    target_by_user = {m.user_id: m for m in target_memberships}
    if any(not m.active and m.user_id in target_by_user and target_by_user[m.user_id].active for m in memberships):
        block('inactive_source_membership_would_gain_access')
    if any(m.active and m.user_id in target_by_user and not target_by_user[m.user_id].active for m in memberships):
        block('target_selected_membership_is_inactive')
    if not any(m.active and m.kind == 'administrator' and User.objects.filter(pk=m.user_id, is_active=True).exists()
            for m in target_memberships):
        block('target_requires_existing_active_administrator')
    from identity.app_accounts import selected_roles
    scoped_account_memberships = set()
    for account in accounts:
        if account.active:
            try:
                selected_roles(account.deployment, account.role_ids)
                scoped_account_memberships.add(account.membership_id)
            except ValidationError:
                block('invalid_application_account_roles', id=str(account.pk))
    for member in memberships:
        current_target = target_by_user.get(member.user_id)
        if (member.active and member.kind == 'administrator' and not
                (current_target and current_target.active and current_target.kind == 'administrator') and
                member.pk not in scoped_account_memberships and not member.roles.filter(pk__in=[r.pk for r in roles]).exists()):
            block('administrator_requires_explicit_target_access', user_id=str(member.user_id))
    role_slugs = {r.pk: f'adopt-{application.pk.hex[:8]}-{r.pk}' for r in roles}
    if target.roles.filter(slug__in=role_slugs.values()).exists():
        block('target_role_slug_conflict')

    moved = [application, installation, service, *deployments, *permissions, *clients, *resources, *pages, *credentials]
    tags = {t.pk: t for o in moved for t in o.permission_tags.all()}
    tags.update({t.pk: t for t in installation.access_tags.all()})
    tags.update({t.pk: t for t in source.permission_tags.all()})
    tags.update({t.pk: t for t in provider_environment.permission_tags.all()})
    tags.update({t.pk: t for role in roles for t in role.permission_tags.all()})
    tags[application.environment.boundary_tag_id] = application.environment.boundary_tag
    if any(t.organization_id != source.pk for t in tags.values()):
        block('foreign_policy_tag')
    other_tag_environments = list(DeploymentEnvironment.objects.filter(boundary_tag_id__in=tags).exclude(
        pk=provider_environment.pk).order_by('pk'))
    state += other_tag_environments
    if other_tag_environments:
        block('unsupported_environment_policy_tag', environment_ids=[str(env.pk) for env in other_tag_environments],
            tag_ids=[str(env.boundary_tag_id) for env in other_tag_environments])
    target_tags = list(PermissionTag.objects.filter(organization=target).order_by('pk'))
    state += list(tags.values()) + target_tags
    target_by_slug = {t.slug: t for t in target_tags}
    mapped_slugs = {tag.pk: (environment.boundary_tag.slug if tag.pk == provider_environment.boundary_tag_id
        else f'adopt-{application.pk.hex}-{tag.pk}') for tag in tags.values()}
    for tag in tags.values():
        if tag.pk == provider_environment.boundary_tag_id:
            if _tag_values(tag) != _tag_values(environment.boundary_tag):
                block('target_policy_tag_conflict', slug=tag.slug)
        elif mapped_slugs[tag.pk] in target_by_slug:
            # Equal policy settings do not mean equal authority. Reusing a
            # destination tag could grant access to unrelated target resources.
            block('target_policy_tag_namespace_conflict', slug=mapped_slugs[tag.pk])
    state += list(target.roles.filter(slug__in=role_slugs.values()))
    return locals()


def plan_adoption(application_id, target_organization_id, target_environment_id, actor_id, member_user_ids=()):
    """Return a deterministic public plan. Does not write, decrypt, or call cloud APIs."""
    app = CloudApplication.objects.select_related('organization', 'platform', 'environment', 'installation__service',
        'installation__package').get(pk=application_id)
    graph = _collect(app, Organization.objects.get(pk=target_organization_id),
        DeploymentEnvironment.objects.get(pk=target_environment_id), User.objects.get(pk=actor_id), member_user_ids)
    rows = {_row(row)['model'] + ':' + str(row.pk): _row(row) for row in graph['state']}
    plan = {'version': 1, 'operation': 'adopt_external_application', 'application_id': str(app.pk),
        'source_organization_id': str(app.organization_id), 'target_organization_id': str(target_organization_id),
        'target_environment_id': str(target_environment_id), 'actor_id': str(actor_id),
        'explicit_member_user_ids': sorted(set(str(v) for v in member_user_ids)),
        'state': sorted(rows.values(), key=lambda v: (v['model'], v['id'])), 'blockers': graph['blockers'],
        'preserves': ['application_installation_service_deployment_oauth_ids', 'encrypted_credentials',
            'completed_release_snapshots', 'existing_cloud_resources', 'application_origin_and_dns'],
        'cloud_operations': [],
        'member_user_ids': sorted(str(m.user_id) for m in graph.get('memberships', [])),
        'policy_tag_slugs': sorted(t.slug for t in graph.get('tags', {}).values()),
        'policy_tag_mappings': [{'source_tag_id': str(tag.pk), 'source_slug': tag.slug,
            'target_slug': graph['mapped_slugs'][tag.pk],
            'reuses_environment_boundary': tag.pk == graph['provider_environment'].boundary_tag_id}
            for tag in sorted(graph.get('tags', {}).values(), key=lambda value: value.pk)],
        'revokes_oauth_records': {model._meta.label_lower: sum(type(row) is model for row in graph.get('oauth_rows', []))
            for model in graph.get('token_models', [])},
        'runtime_data': 'Application database, files, agent definitions and integrations require separate application-side inventory and adoption.'}
    plan['plan_hash'] = _digest(plan)
    return plan


@transaction.atomic
def apply_adoption(application_id, target_organization_id, target_environment_id, actor_id, *, expected_plan_hash,
        member_user_ids=()):
    """Apply exactly one reviewed plan atomically; no provider-side operations."""
    if not isinstance(expected_plan_hash, str) or len(expected_plan_hash) != 64:
        raise ValidationError('Supply the exact reviewed plan hash.')
    app = CloudApplication.objects.select_for_update().get(pk=application_id)
    # Lock organizations before re-reading the graph. Standard environment
    # creation also locks its organization; release preparation locks the app.
    list(Organization.objects.select_for_update().filter(pk__in=[app.organization_id, target_organization_id]).order_by('pk'))
    list(Installation.objects.select_for_update().filter(pk=app.installation_id))
    plan = plan_adoption(application_id, target_organization_id, target_environment_id, actor_id, member_user_ids)
    if plan['plan_hash'] != expected_plan_hash:
        raise ValidationError('Adoption state changed. Review a fresh plan before applying it.')
    if plan['blockers']:
        raise ValidationError('Resolve every reported adoption blocker before applying this plan.')
    app = CloudApplication.objects.select_related('organization', 'platform', 'environment', 'installation__service',
        'installation__package').get(pk=application_id)
    graph = _collect(app, Organization.objects.get(pk=target_organization_id),
        DeploymentEnvironment.objects.get(pk=target_environment_id), User.objects.get(pk=actor_id), member_user_ids)
    # Existing graph rows and their foreign-key insert boundaries are locked
    # before one final fingerprint comparison. A concurrent edit cannot be
    # substituted after the reviewed state has been accepted.
    grouped = {}
    for item in graph['state']:
        grouped.setdefault(type(item), set()).add(item.pk)
    for model in sorted(grouped, key=lambda value: value._meta.label_lower):
        list(model.objects.select_for_update().filter(pk__in=grouped[model]).order_by('pk'))
        for field in model._meta.many_to_many:
            list(field.remote_field.through.objects.select_for_update().filter(
                **{field.m2m_field_name() + '_id__in': grouped[model]}).order_by('pk'))
    if plan_adoption(application_id, target_organization_id, target_environment_id, actor_id,
            member_user_ids)['plan_hash'] != expected_plan_hash:
        raise ValidationError('Adoption state changed. Review a fresh plan before applying it.')
    target, environment, source, actor = (graph[k] for k in ['target', 'environment', 'source', 'actor'])
    mapped_tags = {}
    for old in graph['tags'].values():
        if old.pk == graph['provider_environment'].boundary_tag_id:
            mapped_tags[old.pk] = environment.boundary_tag
        else:
            mapped_tags[old.pk] = PermissionTag.objects.create(organization=target,
                **{**_tag_values(old), 'slug': graph['mapped_slugs'][old.pk]})
    target_members = {m.user_id: m for m in graph['target_memberships']}
    member_mapping = {}
    for old in graph['memberships']:
        member = target_members.get(old.user_id)
        if not member:
            member = Membership.objects.create(organization=target, user_id=old.user_id, active=old.active, kind='member')
        member_mapping[old.pk] = member
        effective_grants = set(old.access_tags.values_list('pk', flat=True))
        effective_grants.update(PermissionTag.objects.filter(role_grants__memberships=old,
            role_grants__organization=source).values_list('pk', flat=True))
        member.access_tags.add(*(mapped_tags[tag_id] for tag_id in effective_grants if tag_id in mapped_tags))
    permissions = graph['permissions']
    for old in graph['roles']:
        role = Role.objects.create(organization=target, slug=graph['role_slugs'][old.pk], name=old.name)
        role.permissions.set(p for p in permissions if old.permissions.filter(pk=p.pk).exists())
        role.access_tags.set(mapped_tags[t.pk] for t in old.access_tags.all() if t.pk in mapped_tags)
        role.permission_tags.set(mapped_tags[t.pk] for t in old.permission_tags.all() if t.pk in mapped_tags)
        for member in graph['memberships']:
            if member.roles.filter(pk=old.pk).exists():
                member_mapping[member.pk].roles.add(role)
        old.permissions.remove(*permissions)
    # Deliberate operator override of ordinary placement immutability. All
    # updates are inside this transaction after exact graph revalidation.
    for item in graph['moved']:
        changes = {}
        if hasattr(item, 'organization_id'):
            changes['organization_id'] = target.pk
        if isinstance(item, (CloudApplication, Deployment, WorkloadCredential)) or isinstance(item, DocumentationPage) and item.environment_id:
            changes['environment_id'] = environment.pk
        if isinstance(item, DocumentationPage):
            changes['scope_key'] = ':'.join(str(v or '-') for v in [item.installation_id,
                environment.pk if item.environment_id else None, item.deployment_id])
        if isinstance(item, Deployment) and item.pk in graph['parent_mapping']:
            changes['parent_id'] = graph['parent_mapping'][item.pk].pk
        if changes:
            type(item).objects.filter(pk=item.pk).update(**changes)
        item.permission_tags.set(mapped_tags[t.pk] for t in item.permission_tags.all())
        item.permission_tags.add(*(mapped_tags[t.pk] for t in [*source.permission_tags.all(),
            *graph['provider_environment'].permission_tags.all()]))
    # Retain restrictions previously inherited from the source organization.
    graph['installation'].permission_tags.add(*(mapped_tags[t.pk] for t in source.permission_tags.all()))
    graph['installation'].access_tags.set(mapped_tags[t.pk] for t in graph['installation'].access_tags.all())
    if graph['private_package']:
        MarketplacePackage.objects.filter(pk=graph['package'].pk).update(organization=target)
    for item in [*graph['accounts'], *graph['pins']]:
        type(item).objects.filter(pk=item.pk).update(membership=member_mapping[item.membership_id])
    # Fresh sign-in is required at the new authority boundary. Only these
    # clients' credentials are revoked; browser sessions and other apps stay.
    get_refresh_token_model().objects.filter(application__in=graph['clients'], revoked__isnull=True).update(
        revoked=timezone.now(), access_token=None)
    get_access_token_model().objects.filter(application__in=graph['clients']).delete()
    get_grant_model().objects.filter(application__in=graph['clients']).delete()
    get_id_token_model().objects.filter(application__in=graph['clients']).delete()
    app.refresh_from_db()
    assignment = CloudHostingAssignment(application=app, platform=app.platform,
        provider_organization=source, owner_organization=target, provider_environment=graph['provider_environment'],
        owner_environment=environment, slug=app.slug, repository=app.repository, archetype=app.archetype,
        public_origin=app.public_origin, actor=actor)
    assignment.save()
    app.full_clean()
    for organization, action in [(source, 'cloud.application_adopted_out'), (target, 'cloud.application_adopted_in')]:
        AuditEvent.objects.create(organization=organization, actor=actor, action=action, target=str(app.pk),
            reason_code='operator_adoption', tags=[{'plan_hash': expected_plan_hash,
                'source_organization_id': str(source.pk), 'target_organization_id': str(target.pk)}])
    return {'application_id': str(app.pk), 'source_organization_id': str(source.pk),
        'target_organization_id': str(target.pk), 'hosting_assignment_id': str(assignment.pk),
        'plan_hash': expected_plan_hash, 'cloud_operations': []}
