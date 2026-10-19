from .models import Membership
from .policy_evaluation import policy_evaluation, policy_fact


def active_membership(user, organization):
    return policy_fact(("membership", str(organization.pk), str(user.pk)),
        lambda: Membership.objects.filter(user=user, organization=organization, active=True).first()) if user else None


def membership_for(user, service):
    if not user or not user.is_active or not service.active or not service.organization.active:
        return None
    return active_membership(user, service.organization)


@policy_evaluation()
def authorization_for(membership, service, principal=None):
    """Evaluate current persisted policy, always constrained to this tenant and service."""
    if membership.organization_id != service.organization_id or not membership.active:
        return {"roles": [], "permissions": []}
    roles = membership.roles.filter(organization_id=service.organization_id)
    administrator = membership.kind == Membership.Kind.ADMINISTRATOR
    permissions = set(service.permissions.filter(
        roles__in=roles, roles__organization_id=service.organization_id,
    ).values_list("codename", flat=True))
    from .app_accounts import application_grants
    app_permissions, app_roles = application_grants(membership, principal) if principal and principal.application and principal.application.service_id == service.pk else (set(), set())
    if app_permissions:
        permissions.update(app_permissions & set(service.permissions.values_list('codename', flat=True)))
    if administrator:
        permissions = set(service.permissions.values_list("codename", flat=True)) | {"admin"}
    # The reserved capability is only granted by the built-in organization role.
    elif "admin" in permissions:
        permissions.remove("admin")
    if principal is not None:
        from .access import decision, record_decision
        if not decision(principal, service)[0]:
            permissions = set()
        else:
            by_name = {p.codename: p for p in service.permissions.prefetch_related("permission_tags")}
            filtered = set()
            for name in permissions:
                if name == "admin":
                    filtered.add(name)
                    continue
                allowed, reason, tags = decision(principal, by_name[name])
                if tags:
                    record_decision(principal, by_name[name], "authorization.permission", allowed, reason, tags)
                if allowed:
                    filtered.add(name)
            permissions = filtered
    return {"roles": sorted({membership.kind, *roles.values_list("slug", flat=True), *app_roles}),
            "permissions": sorted(permissions)}
