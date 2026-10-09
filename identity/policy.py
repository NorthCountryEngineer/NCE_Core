from .models import Membership


def membership_for(user, service):
    if not user or not user.is_active or not service.active or not service.organization.active:
        return None
    return Membership.objects.filter(user=user, organization=service.organization, active=True).first()


def authorization_for(membership, service):
    """Evaluate current persisted policy, always constrained to this tenant and service."""
    if membership.organization_id != service.organization_id or not membership.active:
        return {"roles": [], "permissions": []}
    roles = membership.roles.filter(organization_id=service.organization_id)
    administrator = membership.kind == Membership.Kind.ADMINISTRATOR
    permissions = set(service.permissions.filter(
        roles__in=roles, roles__organization_id=service.organization_id,
    ).values_list("codename", flat=True))
    if administrator:
        permissions = set(service.permissions.values_list("codename", flat=True)) | {"admin"}
    # The reserved capability is only granted by the built-in organization role.
    elif "admin" in permissions:
        permissions.remove("admin")
    return {"roles": sorted({membership.kind, *roles.values_list("slug", flat=True)}),
            "permissions": sorted(permissions)}
