"""Live, tenant-bound tag decisions shared by browser, OAuth, core APIs, and workers."""
import hashlib
import re
import uuid
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone

from .models import AuditEvent, AuthenticationEvidence, Membership, OAuthEvidence, Organization, PermissionTag


@dataclass
class Principal:
    user: object = None
    evidence: object = None
    credential: object = None
    runtime: object = None
    purpose: str = ""
    request_id: object = None


def binding_for(raw, kind="access"):
    return OAuthEvidence.objects.select_related("evidence__identity__provider", "evidence__user").filter(
        token_hash=hashlib.sha256(raw.encode()).hexdigest(), kind=kind).first() if raw else None


def evidence_for(raw, kind="access"):
    binding = binding_for(raw, kind)
    return binding.evidence if binding else None


def session_evidence(request):
    try:
        return AuthenticationEvidence.objects.select_related("identity__provider", "user").filter(
            pk=request.session.get("authentication_evidence"), user=request.user).first()
    except (ValueError, TypeError):
        return None


def request_principal(request):
    organization = getattr(request, "organization", None)
    selected = request.session.get("access_purposes", {}).get(str(organization.pk), "") if organization else ""
    purpose = (request.headers.get("X-Core-Purpose", "") or request.POST.get("purpose_code", "") or
               request.GET.get("purpose_code", "") or selected)
    return Principal(user=request.user if request.user.is_authenticated else None, evidence=session_evidence(request),
                     purpose=purpose, request_id=getattr(request, "security_request_id", None))


def authentication_valid(user, organization, evidence):
    if not user or not user.is_active or not organization.active:
        return False
    policy = getattr(organization, "authentication_policy", None)
    if evidence:
        if evidence.user_id != user.pk or evidence.revoked:
            return False
        if evidence.identity_id:
            identity = evidence.identity
            provider = identity.provider
            if (not identity.active or not provider.active or not provider.organization.active or
                    provider.organization_id != organization.pk or provider.configuration_version != evidence.provider_version or
                    identity.user_id != user.pk):
                return False
        if evidence.verified_at > timezone.now() or (
                (timezone.now() - evidence.verified_at).total_seconds() > (policy.max_session_age if policy else 28800)):
            return False
    if policy and (not evidence or (policy.require_federated and not evidence.identity_id) or
                   (policy.require_mfa and not evidence.mfa)):
        return False
    return True


def resource_organization(resource):
    if isinstance(resource, Organization):
        return resource
    if hasattr(resource, "organization_id"):
        return resource.organization
    for field in ["installation", "service", "dataset", "run", "conversation"]:
        parent = getattr(resource, field, None)
        if parent is not None:
            return resource_organization(parent)
    raise ValueError("Resource has no organization boundary.")


def resource_chain(resource):
    result, seen = [], set()
    def visit(item):
        key = (item._meta.label_lower, str(item.pk))
        if key in seen:
            return
        seen.add(key)
        result.append(item)
        for field in ["organization", "service", "installation", "dataset", "node", "run", "conversation", "connection"]:
            parent = getattr(item, field, None)
            if parent is not None:
                visit(parent)
        if item._meta.label_lower == "platform_services.workflowrun":
            from platform_services.models import AgentDefinition, Dataset
            if item.source_workflow_id:
                visit(item.source_workflow)
            for step in item.snapshot:
                for agent in AgentDefinition.objects.filter(organization=item.organization, slug=step["slug"]):
                    visit(agent)
                tools = [step] if step["kind"] != "rules" else [
                    {"kind": tool["tool"], "configuration": tool.get("configuration", {})}
                    for tool in [*step["configuration"]["rules"], step["configuration"]["default"]]]
                for tool in tools:
                    if tool["kind"] == "publish":
                        for dataset in Dataset.objects.filter(organization=item.organization, slug=tool["configuration"]["dataset"]):
                            visit(dataset)
                    if tool['kind'] == 'chat_send':
                        from core_chat.models import Conversation
                        for conversation in Conversation.objects.filter(organization=item.organization, pk=tool['configuration']['conversation_id']):
                            visit(conversation)
    visit(resource)
    return result


def effective_tags(resource):
    tags = {}
    if isinstance(resource, PermissionTag):
        tags[resource.pk] = resource
    for item in resource_chain(resource):
        if hasattr(item, "permission_tags"):
            tags.update({tag.pk: tag for tag in item.permission_tags.all()})
    return list(tags.values())


def decision(principal, resource):
    organization = resource_organization(resource)
    tags = effective_tags(resource)
    control_tag = isinstance(resource, PermissionTag)
    if not organization.active:
        return False, "organization_disabled", tags
    if any(tag.organization_id != organization.pk or (not tag.active and not (control_tag and tag.pk == resource.pk)) for tag in tags):
        return False, "invalid_resource_tag", tags
    if principal.credential or principal.runtime:
        source = principal.credential.installation if principal.credential else principal.runtime
        if source.organization_id != organization.pk:
            return False, "wrong_organization", tags
        grants = set(source.access_tags.filter(organization=organization, active=True).values_list("pk", flat=True))
        if any(not tag.allow_workloads or tag.requires_mfa or tag.max_auth_age for tag in tags):
            return False, "human_authentication_required", tags
    else:
        if not authentication_valid(principal.user, organization, principal.evidence):
            return False, "authentication_policy", tags
        membership = Membership.objects.filter(organization=organization, user=principal.user, active=True).first()
        if not membership and not principal.user.is_superuser:
            return False, "membership_required", tags
        active_grants = Q(active=True) | Q(pk=resource.pk) if control_tag else Q(active=True)
        grants = set(PermissionTag.objects.filter(organization=organization).filter(active_grants).filter(
            Q(member_grants=membership) | Q(role_grants__memberships=membership,
                                          role_grants__organization=organization)).values_list("pk", flat=True)) if membership else set()
        if any(tag.requires_mfa and not (principal.evidence and principal.evidence.mfa) for tag in tags):
            return False, "mfa_required", tags
        if any(tag.max_auth_age and (not principal.evidence or not principal.evidence.authenticated_at or
                not 0 <= (timezone.now() - principal.evidence.authenticated_at).total_seconds() <= tag.max_auth_age) for tag in tags):
            return False, "fresh_authentication_required", tags
    if any(tag.pk not in grants for tag in tags):
        return False, "tag_grant_required", tags
    if principal.purpose and not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", principal.purpose):
        return False, "invalid_purpose", tags
    if any(tag.allowed_purposes and principal.purpose not in tag.allowed_purposes for tag in tags):
        return False, "purpose_required", tags
    return True, "policy_satisfied", tags


def record_decision(principal, resource, action, allowed, reason, tags):
    AuditEvent.objects.create(organization=resource_organization(resource), actor=principal.user,
        principal_id=principal.runtime.pk if principal.runtime else principal.credential.pk if principal.credential else principal.user.pk if principal.user else None,
        principal_kind="runtime" if principal.runtime else "workload" if principal.credential else "user",
        request_id=principal.request_id or uuid.uuid4(), action=action[:80], target=str(resource.pk),
        outcome="allowed" if allowed else "denied", reason_code=reason,
        purpose_code=principal.purpose if re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", principal.purpose or "") else "",
        tags=sorted(tag.slug for tag in tags))


def require_access(principal, resource, action="resource.access"):
    allowed, reason, tags = decision(principal, resource)
    # Reads of protected resources and every denial have an audit trail; no record bodies or secrets.
    if tags or not allowed:
        record_decision(principal, resource, action, allowed, reason, tags)
    if not allowed:
        raise PermissionDenied("Resource access policy denied this request.")
    return tags


def visible_resources(principal, resources):
    return [resource for resource in resources if decision(principal, resource)[0]]


class SecurityContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.security_request_id = uuid.uuid4()
        response = self.get_response(request)
        response["X-Core-Request-ID"] = str(request.security_request_id)
        return response
