"""Environment boundaries supplement, and never replace, existing role/tag checks."""
from django.core.exceptions import ValidationError
from django.db import transaction

from identity.models import Organization, PermissionTag
from .models import DeploymentEnvironment


@transaction.atomic
def create_environment(organization, **values):
    Organization.objects.select_for_update().get(pk=organization.pk)
    name = values['name']
    tag, created = PermissionTag.objects.get_or_create(organization=organization, slug='environment-' + name,
        defaults={'name': name.title() + ' environment', 'allow_workloads': True})
    if not created and not DeploymentEnvironment.objects.filter(boundary_tag=tag).exists():
        raise ValidationError('The reserved environment tag already exists. Rename that custom tag first.')
    environment = DeploymentEnvironment(organization=organization, boundary_tag=tag, **values)
    environment.full_clean()
    environment.save()
    return environment


def default_environment(organization):
    config = organization.deployment_configuration
    existing = organization.environments.filter(name=config.environment).first()
    if existing:
        if existing.namespace != config.namespace:
            raise ValidationError('The default environment namespace is already registered. Use its environment configuration.')
        return existing
    return create_environment(organization, name=config.environment, namespace=config.namespace,
        application_origin=config.application_origin, ingress_class=config.ingress_class, tls_secret_name=config.tls_secret_name, auto_deploy=config.auto_deploy)


SCOPED_RESOURCES = {'platform_services.dataset', 'platform_services.pipeline', 'platform_services.agentdefinition',
    'platform_services.workflow', 'platform_services.workflowrun', 'platform_services.trainingrun',
    'platform_services.trainingscenario', 'core_chat.conversation', 'core_fabric.fabriccontract', 'identity.protectedresource'}


def unassigned_resources(organization, chain):
    from identity.access import effective_tags, resource_chain
    for item in chain:
        if item._meta.label_lower in SCOPED_RESOURCES:
            assigned = DeploymentEnvironment.objects.filter(organization=organization,
                boundary_tag_id__in=[tag.pk for tag in effective_tags(item)]).exists() or any(
                    part._meta.label_lower == 'platform_services.deploymentenvironment' for part in resource_chain(item))
            if not assigned:
                return True
    return False


def boundary_decision(principal, organization, chain, tags):
    """Return denial reason and automatic grants for matching environment tags only.

    Unscoped pre-migration keys work only in organizations with no registered
    environments. Registering an environment requires rotating those keys.
    """
    tag_environments = list(DeploymentEnvironment.objects.filter(organization=organization,
        boundary_tag_id__in=[tag.pk for tag in tags]))
    names = {env.name for env in tag_environments}
    names.update(item.name for item in chain if item._meta.label_lower == 'platform_services.deploymentenvironment')
    machine = principal.credential or principal.runtime or principal.agent_binding
    if not machine:
        from .models import Deployment
        deployment = Deployment.objects.select_related('environment', 'node').filter(application=principal.application).first() if principal.application else None
        if deployment and deployment.environment_id:
            if not deployment.environment.active or deployment.desired_state != 'running' or deployment.node_id and not deployment.node.active:
                return 'environment_disabled', set()
            if names and names != {deployment.environment.name}:
                return 'environment_mismatch', set()
            if unassigned_resources(organization, chain):
                return 'environment_assignment_required', set()
        if any(not env.active for env in tag_environments) and any(item._meta.label_lower in SCOPED_RESOURCES for item in chain):
            return 'environment_disabled', set()
        from identity.models import Membership
        admin = principal.user and (principal.user.is_superuser or Membership.objects.filter(
            organization=organization, user=principal.user, active=True, kind='administrator').exists())
        return None, {env.boundary_tag_id for env in tag_environments} if admin else set()
    source_environment = None
    if principal.credential:
        from .models import WorkloadCredential
        current = WorkloadCredential.objects.select_related('environment', 'deployment__node', 'deployment__application').filter(
            pk=principal.credential.pk, active=True).first()
        if not current:
            return 'credential_revoked', set()
        source_environment = current.environment
        if current.deployment_id and (current.deployment.installation_id != current.installation_id or
                current.deployment.environment_id != current.environment_id or current.deployment.desired_state != 'running' or
                not current.deployment.node_id or not current.deployment.node.active or
                not current.deployment.application_id or not current.deployment.application.active):
            return 'deployment_unavailable', set()
    elif principal.runtime:
        from .models import RuntimeNode
        current = RuntimeNode.objects.select_related('environment').filter(pk=principal.runtime.pk, active=True).first()
        if not current:
            return 'runtime_revoked', set()
        source_environment = current.environment
    else:
        source_environment = organization.environments.filter(name=principal.agent_binding.environment).first()
    if source_environment and not source_environment.active:
        return 'environment_disabled', set()
    if source_environment or organization.environments.exists():
        names.update(item.environment for item in chain if item._meta.label_lower in {
            'platform_services.agentbinding', 'platform_services.agenttask', 'platform_services.workflowrun'})
    expected = source_environment.name if source_environment else principal.agent_binding.environment if principal.agent_binding else None
    if not source_environment and organization.environments.exists():
        return 'environment_assignment_required', set()
    if names and (expected is None or names != {expected}):
        return 'environment_mismatch', set()
    if any(not env.active for env in tag_environments):
        return 'environment_disabled', set()
    if source_environment and unassigned_resources(organization, chain):
        return 'environment_assignment_required', set()
    # Access-tag grants can never override the explicit environment comparison.
    return None, {env.boundary_tag_id for env in tag_environments}


def principal_environment(principal):
    if principal.credential:
        return DeploymentEnvironment.objects.filter(pk=principal.credential.environment_id).first()
    if principal.runtime:
        return DeploymentEnvironment.objects.filter(pk=principal.runtime.environment_id).first()
    if principal.agent_binding:
        return principal.agent_binding.organization.environments.filter(name=principal.agent_binding.environment).first()
    if principal.application:
        from .models import Deployment
        deployment = Deployment.objects.select_related('environment').filter(application=principal.application).first()
        return deployment.environment if deployment else None
    return None
