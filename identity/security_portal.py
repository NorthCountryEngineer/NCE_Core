from django import forms
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.utils.http import url_has_allowed_host_and_scheme
import re

from .access import request_principal, require_access, visible_resources
from .models import AuthenticationPolicy, FederatedIdentity, IdentityProvider, Membership, PermissionTag, Role
from .views import audit, edit_form, organization_admin


@login_required
@require_POST
def access_purpose(request, slug):
    from .models import Organization
    from .access import authentication_valid
    organization = get_object_or_404(Organization, slug=slug, active=True)
    request.organization = organization
    principal = request_principal(request)
    if (not authentication_valid(request.user, organization, principal.evidence) or
            not (request.user.is_superuser or organization.memberships.filter(user=request.user, active=True).exists())):
        raise PermissionDenied
    purpose = request.POST.get("purpose_code", "")
    if purpose and not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", purpose):
        raise PermissionDenied("Use a configured purpose code.")
    choices = dict(request.session.get("access_purposes", {}))
    choices[str(organization.pk)] = purpose
    request.session["access_purposes"] = choices
    audit(request, "access.purpose_selected", organization.pk)
    target = request.POST.get("next", "")
    if not target.startswith("/") or target.startswith("//") or not url_has_allowed_host_and_scheme(target, allowed_hosts=set()):
        target = reverse("home")
    return redirect(target)


def resource_types():
    from . import models as identity
    from platform_services import models as core
    names = ["Organization", "Membership", "Role", "Service", "ServicePermission", "Application", "Invitation", "IdentityProvider", "ProtectedResource"]
    result = {name.lower(): getattr(identity, name) for name in names}
    result.update({name.lower(): getattr(core, name) for name in ["OrganizationProfile", "BillingProfile", "DeploymentConfiguration",
        "Installation", "RuntimeNode", "Deployment", "WorkloadCredential", "Dataset", "Pipeline", "AgentDefinition", "Workflow", "WorkflowRun",
        'AgentAccount', 'AgentBinding', 'AgentCredential', 'AgentTask',
        'TrainingScenario', 'TrainingRun', 'TrainingEvent', 'TrainingFeedback', 'DeploymentEnvironment',
        'IntegrationDefinition', 'IntegrationConnection', 'ApplicationIntegration', 'AppProject', 'DocumentationPage']})
    from core_chat.models import Conversation, FederationConnection, Message, Participant
    result.update({'chatconversation': Conversation, 'chatconnection': FederationConnection, 'chatmessage': Message, 'chatparticipant': Participant})
    from core_fabric.models import FabricContract, FabricPeer
    result.update({'fabriccontract': FabricContract, 'fabricpeer': FabricPeer})
    return result


def tenant_filter(model, organization):
    from .models import Organization
    fields = {f.name for f in model._meta.fields}
    if model is Organization:
        return {"pk": organization.pk}
    if "organization" in fields:
        return {"organization": organization}
    if "service" in fields:
        return {"service__organization": organization}
    if "installation" in fields:
        return {"installation__organization": organization}
    if "conversation" in fields:
        return {"conversation__organization": organization}
    if 'binding' in fields:
        return {'binding__account__organization': organization}
    if 'run' in fields:
        return {'run__organization': organization}
    raise ValueError("Unsupported resource boundary.")


def enforce_portal_access(request, organization, view_name, kwargs):
    principal = request_principal(request)
    require_access(principal, organization, "administration.access")
    mapping = {
        "membership_edit": "membership", "role_edit": "role", "service_detail": "service", "service_edit": "service",
        "permission_add": "service", "application_edit": "application", "rotate_client_secret": "application",
        "revoke_invitation": "invitation", "installation_settings": "installation", "upgrade": "installation",
        "issue_key": "installation", "deploy": "installation", "revoke_key": "workloadcredential",
        "node_disable": "runtimenode", "deployment_action": "deployment", "dataset_edit": "dataset",
        "pipeline_edit": "pipeline", "agent_edit": "agentdefinition", "workflow_edit": "workflow",
        "run_workflow": "workflow", "run_detail": "workflowrun", "retry_run": "workflowrun", "provider_edit": "identityprovider",
        "protected_resource_edit": "protectedresource",
    }
    if kwargs.get("pk") and view_name in mapping:
        model = resource_types()[mapping[view_name]]
        resource = get_object_or_404(model, pk=kwargs["pk"], **tenant_filter(model, organization))
        require_access(principal, resource, "administration.resource_access")
    if kwargs.get("service_pk"):
        from .models import Service
        require_access(principal, get_object_or_404(Service, pk=kwargs["service_pk"], organization=organization), "administration.service_access")
    if view_name in {"configuration", "node_add", "deploy"}:
        configuration = getattr(organization, "deployment_configuration", None)
        if configuration:
            require_access(principal, configuration, "administration.configuration_access")
    if view_name == "adopt_controller":
        from platform_services.models import Deployment
        root = Deployment.objects.filter(installation__organization=organization, installation__package__slug="core-platform").first()
        if root:
            require_access(principal, root, "administration.controller_access")


class PermissionTagForm(forms.ModelForm):
    class Meta:
        model = PermissionTag
        fields = ["slug", "name", "description", "classification", "requires_mfa", "max_auth_age", "allowed_purposes", "allow_workloads", "active"]


class ProviderForm(forms.ModelForm):
    class Meta:
        model = IdentityProvider
        fields = ["slug", "name", "kind", "issuer", "client_id", "client_secret_file", "token_auth_method", "mfa_amr", "mfa_acr", "active"]


class AuthenticationPolicyForm(forms.ModelForm):
    class Meta:
        model = AuthenticationPolicy
        fields = ["require_federated", "require_mfa", "max_session_age"]


@organization_admin
def protected_resource_edit(request, organization, service_pk, pk=None):
    from .models import ProtectedResource, Service
    service = get_object_or_404(Service, pk=service_pk, organization=organization)
    resource = get_object_or_404(service.protected_resources, pk=pk) if pk else ProtectedResource(service=service)
    class ResourceForm(forms.ModelForm):
        class Meta:
            model = ProtectedResource
            fields = ["resource_type", "external_id", "name", "active"]
    return edit_form(request, ResourceForm(request.POST or None, instance=resource), "Register application resource",
                     reverse("service", args=[organization.slug, service_pk]), "protected_resource.updated")


class IdentityLinkForm(forms.ModelForm):
    class Meta:
        model = FederatedIdentity
        fields = ["provider", "subject", "user", "active"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import User
        self.fields["provider"].queryset = organization.identity_providers.all()
        self.fields["user"].queryset = User.objects.filter(is_active=True, memberships__organization=organization, memberships__active=True)


@organization_admin
def security(request, organization):
    if request.GET.get("resource_type"):
        return redirect("resource_tags", slug=organization.slug, resource_type=request.GET["resource_type"], resource_id=request.GET.get("resource_id", "missing"))
    principal = request_principal(request)
    providers = visible_resources(principal, organization.identity_providers.all())
    return render(request, "identity/security.html", {"organization": organization,
        "issuer": settings.ISSUER,
        "tags": organization.permission_tag_definitions.all(), "providers": providers,
        "identities": FederatedIdentity.objects.filter(provider__in=providers).select_related("provider", "user"),
        "policy": getattr(organization, "authentication_policy", None), "resource_types": resource_types()})


@organization_admin
def tag_edit(request, organization, pk=None):
    tag = get_object_or_404(organization.permission_tag_definitions, pk=pk) if pk else PermissionTag(organization=organization)
    if pk:
        require_access(request_principal(request), tag, "permission_tag.controls")
    if pk and tag.requires_mfa and not (request_principal(request).evidence and request_principal(request).evidence.mfa):
        raise PermissionDenied("Verified MFA is required to change this tag's controls.")
    return edit_form(request, PermissionTagForm(request.POST or None, instance=tag), "Permission tag controls",
                     reverse("security", args=[organization.slug]), "permission_tag.updated")


@organization_admin
def provider_edit(request, organization, pk=None):
    provider = get_object_or_404(organization.identity_providers, pk=pk) if pk else IdentityProvider(organization=organization)
    return edit_form(request, ProviderForm(request.POST or None, instance=provider), "Upstream OIDC identity provider",
                     reverse("security", args=[organization.slug]), "identity_provider.updated")


@organization_admin
def policy_edit(request, organization):
    policy = getattr(organization, "authentication_policy", None) or AuthenticationPolicy(organization=organization)
    form = AuthenticationPolicyForm(request.POST or None, instance=policy)
    if request.method == "POST" and form.is_valid():
        evidence = request_principal(request).evidence
        if form.cleaned_data["require_federated"] and not (evidence and evidence.identity_id):
            form.add_error(None, "Sign in through a linked provider before requiring federation.")
        if form.cleaned_data["require_mfa"] and not (evidence and evidence.mfa):
            form.add_error(None, "Establish verified MFA before requiring it organization-wide.")
        if form.errors:
            return render(request, "identity/form.html", {"form": form, "title": "Organization authentication policy"})
    return edit_form(request, form, "Organization authentication policy", reverse("security", args=[organization.slug]), "authentication_policy.updated")


@organization_admin
def identity_link(request, organization, pk=None):
    identity = get_object_or_404(FederatedIdentity, pk=pk, provider__organization=organization) if pk else FederatedIdentity()
    principal = request_principal(request)
    if pk:
        require_access(principal, identity.provider, "federated_identity.manage")
    form = IdentityLinkForm(request.POST or None, instance=identity, organization=organization)
    form.fields["provider"].queryset = organization.identity_providers.filter(
        pk__in=[provider.pk for provider in visible_resources(principal, organization.identity_providers.all())])
    return edit_form(request, form,
                     "Link an upstream subject to an existing member", reverse("security", args=[organization.slug]), "federated_identity.updated")


@organization_admin
def resource_tags(request, organization, resource_type, resource_id):
    model = resource_types().get(resource_type)
    if model is None:
        from django.http import Http404
        raise Http404
    try:
        filters = tenant_filter(model, organization)
        if "pk" in filters and str(filters.pop("pk")) != resource_id:
            from django.http import Http404
            raise Http404
        resource = get_object_or_404(model, pk=resource_id, **filters)
    except (ValidationError, ValueError):
        from django.http import Http404
        raise Http404
    require_access(request_principal(request), resource, "permission_tags.manage")
    class AssignmentForm(forms.Form):
        permission_tags = forms.ModelMultipleChoiceField(queryset=organization.permission_tag_definitions.all(), required=False)
    form = AssignmentForm(request.POST or None, initial={"permission_tags": resource.permission_tags.all()})
    if hasattr(resource, "access_tags"):
        form.fields["access_tags"] = forms.ModelMultipleChoiceField(queryset=organization.permission_tag_definitions.filter(active=True),
            required=False, label="Explicit access-tag grants", initial=resource.access_tags.all())
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            resource.permission_tags.set(form.cleaned_data["permission_tags"])
            if "access_tags" in form.cleaned_data:
                resource.access_tags.set(form.cleaned_data["access_tags"])
            audit(request, "permission_tags.updated", resource.pk)
        return redirect("security", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Tags and grants: {resource_type} {resource.pk}"})
