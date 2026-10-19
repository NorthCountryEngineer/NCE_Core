"""Local directory administration; no provider calls or implicit access grants."""
from dataclasses import replace
from datetime import timedelta
import secrets
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .access import decision, effective_tags, record_decision, require_access
from .directory_models import EmployeeProfile, ExternalPersonLink, ReportingAssignment
from .models import AuthenticationEvidence, Invitation, Membership, Organization, User
from .policy_evaluation import policy_evaluation


class DirectoryVersionConflict(Exception):
    """The person changed after the administrator opened the form."""


class DirectoryPage(list):
    """Visible rows plus a conservative scan-limit warning, without raw counts."""
    def __init__(self, rows, *, truncated=False):
        super().__init__(rows)
        self.truncated = truncated


PROFILE_FIELDS = {"work_email", "first_name", "last_name", "title", "department", "employee_code", "status", "is_manager"}


def _principal(principal):
    if (not principal.user or principal.credential or principal.runtime or principal.agent_binding or principal.agent_credential):
        raise PermissionDenied("A current human organization administrator is required.")
    user = User.objects.filter(pk=principal.user.pk, is_active=True).first()
    if not user:
        raise PermissionDenied("The account is unavailable.")
    evidence = principal.evidence
    if evidence:
        evidence = AuthenticationEvidence.objects.select_related("identity__provider", "user").filter(pk=evidence.pk, user=user).first()
        if not evidence:
            raise PermissionDenied("The authentication evidence is unavailable.")
    return replace(principal, user=user, evidence=evidence)


def require_directory_admin(principal, organization):
    principal = _principal(principal)
    organization = get_object_or_404(Organization, pk=organization.pk, active=True)
    member = Membership.objects.select_related("user").filter(organization=organization, user=principal.user, active=True,
        kind=Membership.Kind.ADMINISTRATOR).first()
    if not member and not principal.user.is_superuser:
        raise PermissionDenied("A current organization administrator is required.")
    require_access(principal, organization, "directory.administration")
    if member:
        require_access(principal, member, "directory.administrator_membership")
    return principal, organization


def _lock(principal, organization):
    organization = get_object_or_404(Organization.objects.select_for_update(), pk=organization.pk, active=True)
    return require_directory_admin(principal, organization)


def _check_profile(principal, profile):
    require_access(principal, profile, "directory.person_access")
    if profile.membership_id:
        require_access(principal, profile.membership, "directory.person_membership")
    return profile


def _profile(principal, organization, profile):
    return _check_profile(principal, get_object_or_404(EmployeeProfile.objects.select_for_update(of=("self",)).select_related("organization", "membership__user", "invitation"),
        pk=profile.pk, organization=organization))


def _revision(profile, expected_revision):
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValidationError("Supply the person's current revision.")
    if profile.revision != expected_revision:
        raise DirectoryVersionConflict("This person changed. Refresh before saving.")


def _audit(principal, resource, action):
    # Stable identifiers/revisions only; no names, email, provider IDs or bodies.
    if isinstance(resource, EmployeeProfile):
        target = f"{resource.pk}:{resource.revision}"
        from .models import AuditEvent
        import re
        AuditEvent.objects.create(organization=resource.organization, actor=principal.user,
            action=action, target=target, request_id=principal.request_id,
            purpose_code=principal.purpose if re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", principal.purpose or "") else "")
    else:
        record_decision(principal, resource, action, True, "administrator_approved", effective_tags(resource))


def _save_profile(profile):
    profile.full_clean()
    profile.save()
    return profile


def _values(values):
    if not isinstance(values, dict) or set(values) - PROFILE_FIELDS:
        raise ValidationError("Use only the supported directory fields.")
    if "is_manager" in values and type(values["is_manager"]) is not bool:
        raise ValidationError("Choose whether this person is a manager.")
    if any(not isinstance(value, str) for key, value in values.items() if key != "is_manager"):
        raise ValidationError("Directory text fields must contain text.")
    return {key: value.strip() if isinstance(value, str) else value for key, value in values.items()}


@transaction.atomic
def create_profile(principal, organization, values):
    principal, organization = _lock(principal, organization)
    profile = EmployeeProfile(organization=organization, **_values(values))
    _save_profile(profile)
    _audit(principal, profile, "directory.person_created")
    return profile


def _revoke_invitation(principal, profile):
    if profile.invitation_id and not profile.invitation.accepted:
        invitation = get_object_or_404(Invitation.objects.select_for_update(), pk=profile.invitation_id, organization=profile.organization)
        require_access(principal, invitation, "directory.invitation_revoke")
        profile.invitation = None
        _audit(principal, invitation, "directory.invitation_revoked")
        invitation.delete()


@transaction.atomic
def update_profile(principal, profile, values, expected_revision):
    principal, organization = _lock(principal, profile.organization)
    profile = _profile(principal, organization, profile)
    _revision(profile, expected_revision)
    values = _values(values)
    if ("work_email" in values and values["work_email"].lower() != profile.work_email) or values.get("status", profile.status) != EmployeeProfile.Status.ACTIVE:
        _revoke_invitation(principal, profile)
    for key, value in values.items():
        setattr(profile, key, value)
    # Invalidated relationships remain present as inactive history. Login access
    # is deliberately independent of employment status.
    edges = ReportingAssignment.objects.filter(organization=organization, active=True)
    if profile.status != EmployeeProfile.Status.ACTIVE:
        edges = edges.filter(Q(manager=profile) | Q(employee=profile))
    elif not profile.is_manager:
        edges = edges.filter(manager=profile)
    else:
        edges = edges.none()
    affected = set()
    for edge in edges.select_related("organization", "manager__membership", "employee__membership"):
        _check_edge(principal, edge)
        edge.active = False
        edge.save(update_fields=["active", "updated_at"])
        affected.add(edge.employee_id)
        _audit(principal, edge, "directory.reporting_disabled")
    for person in EmployeeProfile.objects.select_for_update().filter(pk__in=affected).exclude(pk=profile.pk):
        person.revision += 1
        person.save(update_fields=["revision", "updated_at"])
        _audit(principal, person, "directory.reporting_updated")
    profile.revision += 1
    _save_profile(profile)
    _audit(principal, profile, "directory.person_updated")
    return profile


def _active_endpoint(profile):
    return profile.status == EmployeeProfile.Status.ACTIVE and (not profile.membership_id or
        profile.membership.active and profile.membership.user.is_active)


def _check_edge(principal, edge):
    require_access(principal, edge, "directory.reporting_access")
    _check_profile(principal, edge.manager)
    _check_profile(principal, edge.employee)


@transaction.atomic
def replace_manager_assignments(principal, employee, assignments, expected_revision):
    principal, organization = _lock(principal, employee.organization)
    employee = _profile(principal, organization, employee)
    _revision(employee, expected_revision)
    if not isinstance(assignments, list) or len(assignments) > 30:
        raise ValidationError("Choose up to 30 manager assignments.")
    selected, primary_count = {}, 0
    for value in assignments:
        if not isinstance(value, dict) or set(value) != {"manager_id", "is_primary"} or type(value["is_primary"]) is not bool:
            raise ValidationError("Supply manager identifiers and primary flags.")
        try:
            manager_id = uuid.UUID(str(value["manager_id"]))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Choose a manager from this organization.")
        if manager_id in selected:
            raise ValidationError("Choose each manager once.")
        if manager_id == employee.pk:
            raise ValidationError("An employee cannot report to themselves.")
        manager = _profile(principal, organization, EmployeeProfile(pk=manager_id))
        if not manager.is_manager or not _active_endpoint(manager) or not _active_endpoint(employee):
            raise ValidationError("Assignments require active people and an active manager.")
        selected[manager_id] = (manager, value["is_primary"])
        primary_count += value["is_primary"]
    if primary_count > 1:
        raise ValidationError("Choose at most one primary manager.")
    existing = list(ReportingAssignment.objects.select_for_update(of=("self",)).select_related("organization", "manager__membership__user", "employee__membership__user")
        .filter(organization=organization, employee=employee))
    for edge in existing:
        # Never replace a hidden assignment using a partially visible form.
        _check_edge(principal, edge)
    graph = {}
    rows = list(ReportingAssignment.objects.filter(organization=organization, active=True).exclude(employee=employee)
        .values_list("manager_id", "employee_id")[:10001])
    if len(rows) > 10000:
        raise ValidationError("The reporting graph exceeds the interactive editing limit.")
    for manager_id, employee_id in rows:
        graph.setdefault(manager_id, set()).add(employee_id)
    descendants, pending = set(), [employee.pk]
    while pending:
        current = pending.pop()
        if current not in descendants:
            descendants.add(current)
            pending.extend(graph.get(current, ()))
    if any(manager_id in descendants for manager_id in selected):
        raise ValidationError("This assignment would create a reporting cycle.")
    by_manager = {edge.manager_id: edge for edge in existing}
    # Deactivate first so the partial unique constraint permits a new primary.
    # Removed edges retain their prior primary flag as part of their history.
    for edge in existing:
        edge.active = False
        edge.save(update_fields=["active", "updated_at"])
    for manager_id, (manager, is_primary) in selected.items():
        edge = by_manager.get(manager_id) or ReportingAssignment(organization=organization, manager=manager, employee=employee)
        edge.active, edge.is_primary = True, is_primary
        edge.full_clean()
        edge.save()
        _audit(principal, edge, "directory.reporting_primary_assigned" if is_primary else "directory.reporting_secondary_assigned")
    for edge in existing:
        if edge.manager_id not in selected:
            _audit(principal, edge, "directory.reporting_disabled")
    employee.revision += 1
    _save_profile(employee)
    _audit(principal, employee, "directory.reporting_updated")
    return employee


@transaction.atomic
def attach_membership(principal, profile, membership, expected_revision):
    principal, organization = _lock(principal, profile.organization)
    profile = _profile(principal, organization, profile)
    _revision(profile, expected_revision)
    if membership is not None:
        membership = get_object_or_404(Membership.objects.select_for_update().select_related("user"), pk=membership.pk, organization=organization)
        require_access(principal, membership, "directory.membership_link")
        if not membership.active or not membership.user.is_active:
            raise ValidationError("Choose an active organization membership.")
    if profile.membership_id == (membership.pk if membership else None):
        return profile
    _revoke_invitation(principal, profile)
    profile.membership = membership
    profile.revision += 1
    _save_profile(profile)
    _audit(principal, profile, "directory.membership_linked" if membership else "directory.membership_unlinked")
    return profile


@transaction.atomic
def add_external_reference(principal, profile, values, expected_revision):
    principal, organization = _lock(principal, profile.organization)
    profile = _profile(principal, organization, profile)
    _revision(profile, expected_revision)
    fields = {"source_namespace", "object_type", "external_id", "source_revision"}
    if not isinstance(values, dict) or set(values) - fields or not fields - {"source_revision"} <= set(values) or any(not isinstance(value, str) for value in values.values()):
        raise ValidationError("Supply a bounded namespaced external reference.")
    values = {key: value.strip() for key, value in values.items()}
    reference = ExternalPersonLink.objects.select_for_update().filter(organization=organization,
        source_namespace=values["source_namespace"], object_type=values["object_type"], external_id=values["external_id"]).first()
    if reference:
        require_access(principal, reference, "directory.external_reference_access")
        if reference.profile_id != profile.pk:
            raise ValidationError("This external reference is already linked to another person.")
        if reference.source_revision == values.get("source_revision", ""):
            return reference
        reference.source_revision = values.get("source_revision", "")
    else:
        reference = ExternalPersonLink(organization=organization, profile=profile, **values)
    reference.full_clean()
    reference.save()
    profile.revision += 1
    _save_profile(profile)
    _audit(principal, reference, "directory.external_reference_saved")
    _audit(principal, profile, "directory.external_reference_updated")
    return reference


@transaction.atomic
def invite_profile(principal, profile, expected_revision):
    principal, organization = _lock(principal, profile.organization)
    profile = _profile(principal, organization, profile)
    _revision(profile, expected_revision)
    if profile.membership_id or profile.status != EmployeeProfile.Status.ACTIVE:
        raise ValidationError("Invite an active person who does not already have a linked account.")
    if profile.invitation_id:
        invitation = get_object_or_404(Invitation.objects.select_for_update(), pk=profile.invitation_id, organization=organization)
        require_access(principal, invitation, "directory.invitation_access")
        if invitation.usable and invitation.email.lower() == profile.work_email and invitation.kind == Membership.Kind.MEMBER:
            return invitation, None
        _revoke_invitation(principal, profile)
    raw = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(organization=organization, email=profile.work_email, kind=Membership.Kind.MEMBER,
        token_hash=Invitation.digest(raw), expires=timezone.now() + timedelta(hours=48))
    profile.invitation = invitation
    profile.revision += 1
    _save_profile(profile)
    _audit(principal, invitation, "directory.invitation_created")
    _audit(principal, profile, "directory.person_invited")
    return invitation, raw


@transaction.atomic
def link_accepted_invitation(invitation, membership, principal=None):
    """Bind only the explicitly invited profile, inside standard acceptance."""
    organization = get_object_or_404(Organization.objects.select_for_update(), pk=invitation.organization_id, active=True)
    invitation = get_object_or_404(Invitation.objects.select_for_update(), pk=invitation.pk, organization=organization)
    profile = EmployeeProfile.objects.select_for_update().filter(organization=organization, invitation=invitation).first()
    if not profile:
        return None
    membership = get_object_or_404(Membership.objects.select_related("user"), pk=membership.pk, organization=organization)
    if (not invitation.usable or not membership.active or not membership.user.is_active or profile.status != EmployeeProfile.Status.ACTIVE or
            invitation.kind != Membership.Kind.MEMBER or invitation.email.strip().lower() != profile.work_email or
            membership.user.email.strip().lower() != profile.work_email):
        raise PermissionDenied("The directory invitation is no longer available.")
    if profile.membership_id and profile.membership_id != membership.pk:
        raise PermissionDenied("This person is already linked to an account.")
    if not principal or not principal.user or principal.user.pk != membership.user_id:
        raise PermissionDenied("The matching authenticated account is required.")
    principal = _principal(principal)
    require_access(principal, organization, "directory.invitation_accept")
    require_access(principal, invitation, "directory.invitation_accept")
    _check_profile(principal, profile)
    require_access(principal, membership, "directory.invitation_membership")
    profile.membership = membership
    profile.revision += 1
    _save_profile(profile)
    _audit(principal, profile, "directory.invitation_linked")
    return profile


def _can_read_profile(principal, profile):
    return decision(principal, profile)[0] and (not profile.membership_id or decision(principal, profile.membership)[0])


@policy_evaluation()
def visible_profiles(principal, organization, search="", limit=100, offset=0, status="", is_manager=None):
    principal, organization = require_directory_admin(principal, organization)
    if (not isinstance(search, str) or len(search) > 160 or type(limit) is not int or not 1 <= limit <= 1000 or
            type(offset) is not int or not 0 <= offset <= 10000 or not isinstance(status, str) or status not in {"", *EmployeeProfile.Status.values} or
            is_manager is not None and type(is_manager) is not bool):
        raise ValidationError("Use a bounded directory search and page.")
    rows = EmployeeProfile.objects.filter(organization=organization).select_related("organization", "membership__user", "membership__organization").prefetch_related(
        "permission_tags", "membership__permission_tags")
    if status:
        rows = rows.filter(status=status)
    if is_manager is not None:
        rows = rows.filter(is_manager=is_manager)
    if search.strip():
        rows = rows.filter(Q(first_name__icontains=search.strip()) | Q(last_name__icontains=search.strip()) |
            Q(work_email__icontains=search.strip()) | Q(department__icontains=search.strip()) | Q(title__icontains=search.strip()) | Q(employee_code__icontains=search.strip()))
    candidates = list(rows[offset:offset + limit + 1])
    return DirectoryPage([profile for profile in candidates[:limit] if _can_read_profile(principal, profile)],
        truncated=len(candidates) > limit)


@policy_evaluation()
def profile_detail(principal, organization, pk):
    principal, organization = require_directory_admin(principal, organization)
    return _check_profile(principal, get_object_or_404(EmployeeProfile.objects.select_related("organization", "membership__user", "membership__organization", "invitation")
        .prefetch_related("permission_tags", "membership__permission_tags"), pk=pk, organization=organization))


@policy_evaluation()
def visible_assignments(principal, organization, employee=None, active_only=True, manager=None):
    principal, organization = require_directory_admin(principal, organization)
    if type(active_only) is not bool:
        raise ValidationError("Choose whether to include inactive assignments.")
    rows = ReportingAssignment.objects.filter(organization=organization).select_related("organization", "manager__organization", "manager__membership__user", "manager__membership__organization",
        "employee__organization", "employee__membership__user", "employee__membership__organization").prefetch_related(
            "permission_tags", "manager__permission_tags", "manager__membership__permission_tags", "employee__permission_tags", "employee__membership__permission_tags")
    if employee:
        if employee.organization_id != organization.pk:
            raise PermissionDenied("Choose an employee from this organization.")
        rows = rows.filter(employee=employee)
    if manager:
        if manager.organization_id != organization.pk:
            raise PermissionDenied("Choose a manager from this organization.")
        rows = rows.filter(manager=manager)
    if active_only:
        rows = rows.filter(active=True)
    else:
        rows = rows.order_by("-active", "-is_primary", "created_at", "id")
    candidates = list(rows[:1001])
    return DirectoryPage([edge for edge in candidates[:1000] if decision(principal, edge)[0] and _can_read_profile(principal, edge.manager) and
        _can_read_profile(principal, edge.employee) and (not active_only or _active_endpoint(edge.manager) and _active_endpoint(edge.employee) and edge.manager.is_manager)],
        truncated=len(candidates) > 1000)
