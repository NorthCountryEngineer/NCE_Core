from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from identity.models import AuditEvent, Membership, Organization
from .forms import DeploymentConfigurationForm, MetadataForm, NodeForm, OrganizationOnboardingForm, ReviewForm
from .models import BillingProfile, DeploymentConfiguration, Onboarding, OrganizationProfile, RuntimeNode, WorkloadCredential
from .services import install_core


@login_required
@never_cache
def onboarding(request):
    if not (request.user.is_developer or request.user.is_superuser):
        raise PermissionDenied("Developer onboarding requires an operator-approved account.")
    state, _ = Onboarding.objects.get_or_create(user=request.user)
    if state.complete:
        return redirect("platform_dashboard", slug=state.organization.slug)
    if state.organization_id:
        request.organization = state.organization
        if not state.organization.active or not state.organization.memberships.filter(
            user=request.user, active=True, kind="administrator").exists():
            raise PermissionDenied("Administrator membership is required to continue onboarding.")
        from identity.access import request_principal, require_access
        principal = request_principal(request)
        require_access(principal, state.organization, "onboarding.resume")
        for name in ["profile", "billing", "deployment_configuration"]:
            resource = getattr(state.organization, name, None)
            if resource:
                require_access(principal, resource, "onboarding.metadata_read")
    if request.method == "POST" and state.step == 5 and request.POST.get("action") == "reconnect":
        with transaction.atomic():
            locked = Onboarding.objects.select_for_update().get(pk=state.pk)
            if locked.step == 5 and not locked.complete:
                if locked.runtime_id:
                    RuntimeNode.objects.filter(pk=locked.runtime_id).update(active=False)
                locked.runtime = None
                locked.step = 4
                locked.save(update_fields=["runtime", "step"])
        return redirect("onboarding")
    data = request.POST if request.method == "POST" else None
    titles = {1: "Establish your organization", 2: "Organization and billing details", 3: "Deployment configuration",
              4: "Connect your Kubernetes runtime", 5: "Review and activate your core workspace"}
    if state.step == 1:
        form = OrganizationOnboardingForm(data)
    elif state.step == 2:
        form = MetadataForm(data)
    elif state.step == 3:
        form = DeploymentConfigurationForm(data, instance=DeploymentConfiguration(organization=state.organization))
    elif state.step == 4:
        form = NodeForm(data)
        form.fields["name"].required = state.organization.deployment_configuration.auto_deploy
        form.fields["name"].help_text = "Name your runtime to connect it now. Leave blank to connect later when automatic deployment is disabled."
    else:
        form = ReviewForm(data)
    if request.method == "POST" and state.step == 5 and form.is_valid() and state.organization.deployment_configuration.auto_deploy and (
        not state.runtime_id or not state.runtime.online):
        form.add_error(None, "Connect the configured runtime before enabling automatic deployment. No infrastructure will be created.")
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                locked = Onboarding.objects.select_for_update().get(pk=state.pk)
                if locked.step != state.step or locked.complete:
                    return redirect("onboarding")
                if state.step == 1:
                    organization = form.save()
                    Membership.objects.create(organization=organization, user=request.user, kind="administrator")
                    locked.organization = organization
                    AuditEvent.objects.create(organization=organization, actor=request.user, action="organization.onboarded", target=organization.slug)
                elif state.step == 2:
                    values = form.cleaned_data
                    OrganizationProfile.objects.update_or_create(organization=state.organization,
                        defaults={k: values[k] for k in ["legal_name", "country", "support_email", "timezone"]})
                    BillingProfile.objects.update_or_create(organization=state.organization,
                        defaults={k: values[k] for k in ["provider", "billing_email", "currency"]})
                elif state.step == 3:
                    form.save()
                elif state.step == 4 and form.cleaned_data["name"]:
                    import secrets
                    raw = "cr_" + secrets.token_urlsafe(48)
                    locked.runtime = RuntimeNode.objects.create(organization=state.organization, name=form.cleaned_data["name"],
                        namespace=state.organization.deployment_configuration.namespace, credential_hash=WorkloadCredential.digest(raw))
                    AuditEvent.objects.create(organization=state.organization, actor=request.user, action="runtime.created", target=str(locked.runtime.pk))
                elif state.step == 4:
                    pass
                else:
                    install_core(state.organization, actor=request.user)
                    locked.complete = True
                    locked.completed_at = timezone.now()
                    AuditEvent.objects.create(organization=state.organization, actor=request.user, action="onboarding.completed", target=state.organization.slug)
                locked.step = min(state.step + 1, 5)
                locked.save()
        except IntegrityError:
            form.add_error(None, "The organization name is already in use. Choose another slug.")
        else:
            if state.step == 4 and form.cleaned_data["name"]:
                return render(request, "platform_services/runtime_credential.html", {
                    "node": locked.runtime, "runtime_token": raw, "onboarding": True})
            return redirect("onboarding")
    return render(request, "platform_services/onboarding.html", {"form": form, "title": titles[state.step], "state": state})
