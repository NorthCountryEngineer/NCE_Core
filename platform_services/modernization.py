"""Administrator-approved adoption of existing, namespace-confined Deployments."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.access import require_access
from identity.models import AuditEvent, Organization
from .models import Deployment, MarketplacePackage, RuntimeApplication
from .services import CAPABILITY_PACKAGE, create_deployment, install_package


@transaction.atomic
def adopt_application(organization, inventory, container, slug, name, endpoint, capabilities, callback_path, principal):
    Organization.objects.select_for_update().get(pk=organization.pk)
    if not principal.user or not (principal.user.is_superuser or organization.memberships.filter(
            user=principal.user, active=True, kind='administrator').exists()):
        raise PermissionDenied('An organization administrator must approve modernization.')
    inventory = RuntimeApplication.objects.select_related('node__environment', 'node__organization').get(pk=inventory.pk)
    node = inventory.node
    if (node.organization_id != organization.pk or not node.active or not node.online or not node.environment_id or
            not node.environment.active or inventory.observed_at < timezone.now() - timedelta(seconds=90)):
        raise ValidationError('Refresh the connected environment runtime inventory before adopting this application.')
    require_access(principal, node, 'modernization.runtime')
    if container not in [item['name'] for item in inventory.containers]:
        raise ValidationError('Choose a container from the current inventory.')
    from .kubernetes import resource_name
    deployments = Deployment.objects.filter(node=node)
    if deployments.filter(Q(resource_uid=inventory.resource_uid) | Q(resource_name=inventory.name)).exists() or inventory.name in {
            resource_name(deployment) for deployment in deployments.filter(target='kubernetes')}:
        raise ValidationError('This Kubernetes Deployment is already registered.')
    # Private catalog entries do not disclose customer applications to other tenants.
    package = MarketplacePackage(organization=organization, slug='modern-' + inventory.resource_uid,
        name=name, kind='application', description='Existing Kubernetes application: ' + slug,
        manifest={'existing_workload': True, 'capabilities': capabilities,
            'dependencies': sorted({CAPABILITY_PACKAGE[c] for c in capabilities}), 'callback_path': callback_path})
    package.full_clean()
    package.save()
    installation = install_package(organization, package, capabilities, principal.user)
    deployment = create_deployment(installation, node, endpoint, actor=principal.user, adopted=inventory)
    deployment.target = 'kubernetes-adopted'
    deployment.container_name = container
    deployment.save(update_fields=['target', 'container_name'])
    AuditEvent.objects.create(organization=organization, actor=principal.user, action='application.modernized', target=str(deployment.pk))
    return deployment
