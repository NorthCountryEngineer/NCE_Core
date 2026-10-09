import secrets
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseBadRequest
from django.urls import reverse
from django.views.decorators.http import require_POST

from identity.views import audit, edit_form, organization_admin
from .forms import (
    AdoptControllerForm, AgentForm, DatasetForm, DeploymentConfigurationForm, DeploymentForm,
    InstallationForm, InstallationSettingsForm, NodeForm, PipelineForm, RunForm, WorkflowForm,
)
from .models import AgentDefinition, Dataset, Deployment, DeploymentConfiguration, Installation, MarketplacePackage, Pipeline, RuntimeNode, Workflow
from .services import create_deployment, install_package, issue_workload_credential, upgrade_installation
from .workflows import enqueue, snapshot_agents


@organization_admin
def dashboard(request, organization):
    return render(request, "platform_services/dashboard.html", {"organization": organization,
        "installations": organization.installations.select_related("package"),
        "deployments": Deployment.objects.select_related("installation__package", "node").filter(installation__organization=organization),
        "nodes": organization.runtime_nodes.all(), "datasets": organization.datasets.all(),
        "pipelines": organization.pipelines.select_related("dataset"), "agents": organization.agents.all(),
        "workflows": organization.workflows.all(), "runs": organization.workflow_runs.order_by("-created")[:25],
        "billing": getattr(organization, "billing", None),
        "config": getattr(organization, "deployment_configuration", None),
    })


@organization_admin
def marketplace(request, organization):
    return render(request, "platform_services/marketplace.html", {"packages": MarketplacePackage.objects.filter(active=True),
        "organization": organization, "installed_slugs": list(organization.installations.values_list("package__slug", flat=True))})


@organization_admin
def install(request, organization, package_slug):
    package = get_object_or_404(MarketplacePackage, slug=package_slug, active=True)
    form = InstallationForm(request.POST or None, package=package)
    if request.method == "POST" and form.is_valid():
        try:
            install_package(organization, package, form.cleaned_data["grants"], request.user)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Install {package.name}"})


@organization_admin
def installation_settings(request, organization, pk):
    installation = get_object_or_404(organization.installations.select_related("package"), pk=pk)
    form = InstallationSettingsForm(request.POST or None, package=installation.package, manifest=installation.manifest,
        initial={"grants": installation.granted_capabilities, "enabled": installation.enabled})
    if request.method == "POST" and form.is_valid():
        if installation.package.slug == "core-platform" and not form.cleaned_data["enabled"]:
            form.add_error(None, "The current control plane must remain enabled.")
        else:
            with transaction.atomic():
                installation.granted_capabilities = form.cleaned_data["grants"]
                installation.enabled = form.cleaned_data["enabled"]
                installation.save(update_fields=["granted_capabilities", "enabled"])
                installation.service.active = installation.enabled
                installation.service.save(update_fields=["active"])
                if not installation.enabled:
                    installation.deployments.filter(target="kubernetes").update(desired_state="stopped", revision=F("revision") + 1)
                audit(request, "installation.updated", installation.pk)
            return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Access: {installation.package.name}"})


@organization_admin
def upgrade(request, organization, pk):
    installation = get_object_or_404(organization.installations.select_related("package"), pk=pk, package__kind="application")
    form = InstallationForm(request.POST or None, package=installation.package,
                            initial={"grants": installation.granted_capabilities})
    if request.method == "POST" and form.is_valid():
        try:
            upgrade_installation(installation, form.cleaned_data["grants"], request.user)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Approve {installation.package.name} release {installation.package.version}"})


@organization_admin
def configuration(request, organization):
    config = getattr(organization, "deployment_configuration", None) or DeploymentConfiguration(organization=organization)
    form = DeploymentConfigurationForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid() and organization.runtime_nodes.filter(active=True).exclude(namespace=form.cleaned_data["namespace"]).exists():
        form.add_error("namespace", "Disable existing runtimes before changing the namespace.")
    return edit_form(request, form, "Kubernetes deployment configuration", reverse("platform_dashboard", args=[organization.slug]), "deployment_configuration.updated")


@organization_admin
def node_add(request, organization):
    config = getattr(organization, "deployment_configuration", None)
    if config is None:
        return redirect("deployment_configuration", slug=organization.slug)
    form = NodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        raw = "cr_" + secrets.token_urlsafe(48)
        from .models import WorkloadCredential
        with transaction.atomic():
            node = RuntimeNode.objects.create(organization=organization, name=form.cleaned_data["name"], namespace=config.namespace,
                credential_hash=WorkloadCredential.digest(raw))
            audit(request, "runtime.created", node.pk)
        return render(request, "platform_services/runtime_credential.html", {"node": node, "runtime_token": raw})
    return render(request, "identity/form.html", {"form": form, "title": "Connect a Kubernetes runtime"})


@organization_admin
@require_POST
def node_disable(request, organization, pk):
    with transaction.atomic():
        node = get_object_or_404(organization.runtime_nodes, pk=pk)
        node.active = False
        node.save(update_fields=["active"])
        audit(request, "runtime.disabled", node.pk)
    messages.info(request, "Runtime access revoked. Existing pods must be stopped using your cluster administration tools.")
    return redirect("platform_dashboard", slug=organization.slug)


@organization_admin
@require_POST
def issue_key(request, organization, pk):
    installation = get_object_or_404(organization.installations.select_related("package"), pk=pk, enabled=True)
    with transaction.atomic():
        credential, raw = issue_workload_credential(installation, "Manual integration")
        audit(request, "workload_credential.created", credential.pk)
    return render(request, "platform_services/workload_credential.html", {"installation": installation,
        "workload_token": raw, "credential": credential})


@organization_admin
@require_POST
def revoke_key(request, organization, pk):
    from .models import WorkloadCredential
    with transaction.atomic():
        credential = get_object_or_404(WorkloadCredential, pk=pk, installation__organization=organization)
        credential.active = False
        credential.save(update_fields=["active"])
        audit(request, "workload_credential.revoked", credential.pk)
    return redirect("platform_dashboard", slug=organization.slug)


@organization_admin
def deploy(request, organization, pk):
    installation = get_object_or_404(organization.installations.select_related("package"), pk=pk, enabled=True)
    config = getattr(organization, "deployment_configuration", None)
    if config is None:
        return redirect("deployment_configuration", slug=organization.slug)
    form = DeploymentForm(request.POST or None, organization=organization, initial={"endpoint": config.application_origin})
    if request.method == "POST" and form.is_valid():
        try:
            create_deployment(installation, form.cleaned_data["node"], form.cleaned_data["endpoint"], actor=request.user,
                              desired_state="running" if config.auto_deploy else "stopped")
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": "Register and configure deployment"})


@organization_admin
def adopt_controller(request, organization):
    deployment = get_object_or_404(Deployment, installation__organization=organization, installation__package__slug="core-platform")
    form = AdoptControllerForm(request.POST or None, organization=organization)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            deployment.node = form.cleaned_data["node"]
            deployment.namespace = deployment.node.namespace
            deployment.resource_name = form.cleaned_data["resource_name"]
            deployment.target = "kubernetes-controller"
            deployment.action = "observe"
            deployment.observed_state = "pending"
            deployment.last_seen = None
            deployment.save()
            audit(request, "controller.adopted", deployment.pk)
        return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": "Manage this controller in Kubernetes"})


@organization_admin
@require_POST
def deployment_action(request, organization, pk):
    action = request.POST.get("action")
    if action not in {"start", "stop", "restart"}:
        return HttpResponseBadRequest("Unknown deployment action.")
    with transaction.atomic():
        deployment = get_object_or_404(Deployment.objects.select_for_update().select_related("installation__package"), pk=pk,
            installation__organization=organization)
        if deployment.target == "embedded" or (deployment.target == "kubernetes-controller" and action != "restart"):
            messages.error(request, "Embedded services follow their installation settings. A connected controller supports rolling restart only.")
        elif action != "stop" and not deployment.installation.enabled:
            messages.error(request, "Enable this installation before starting it.")
        else:
            deployment.desired_state = "stopped" if action == "stop" else "running"
            deployment.action = "restart" if deployment.target == "kubernetes-controller" else "reconcile"
            deployment.revision += 1
            deployment.healthy = False
            deployment.save(update_fields=["desired_state", "action", "revision", "healthy"])
            if deployment.application_id:
                deployment.application.active = action != "stop"
                deployment.application.save(update_fields=["active"])
                if action == "stop":
                    from oauth2_provider.models import get_access_token_model, get_refresh_token_model, get_grant_model
                    get_access_token_model().objects.filter(application=deployment.application).delete()
                    for refresh in get_refresh_token_model().objects.filter(application=deployment.application, revoked=None):
                        refresh.revoke()
                    get_grant_model().objects.filter(application=deployment.application).delete()
                    deployment.installation.credentials.filter(name=f"deployment:{deployment.pk}").update(active=False)
                elif action == "start":
                    from .secrets import decrypt_bundle, encrypt_bundle
                    bundle = decrypt_bundle(deployment.encrypted_bundle)
                    deployment.installation.credentials.filter(name=f"deployment:{deployment.pk}").update(active=False)
                    _, raw = issue_workload_credential(deployment.installation, f"deployment:{deployment.pk}")
                    bundle["workload_token"] = raw
                    deployment.encrypted_bundle = encrypt_bundle(bundle)
                    deployment.save(update_fields=["encrypted_bundle"])
            audit(request, f"deployment.{action}", deployment.pk)
    return redirect("platform_dashboard", slug=organization.slug)


@organization_admin
def dataset_edit(request, organization, pk=None):
    obj = get_object_or_404(organization.datasets, pk=pk) if pk else Dataset(organization=organization)
    return edit_form(request, DatasetForm(request.POST or None, instance=obj), "Dataset",
        reverse("platform_dashboard", args=[organization.slug]), "dataset.updated")


@organization_admin
def pipeline_edit(request, organization, pk=None):
    obj = get_object_or_404(organization.pipelines, pk=pk) if pk else Pipeline(organization=organization)
    return edit_form(request, PipelineForm(request.POST or None, instance=obj, organization=organization), "Data pipeline",
        reverse("platform_dashboard", args=[organization.slug]), "pipeline.updated")


@organization_admin
def agent_edit(request, organization, pk=None):
    obj = get_object_or_404(organization.agents, pk=pk) if pk else AgentDefinition(organization=organization)
    return edit_form(request, AgentForm(request.POST or None, instance=obj), "Core agent",
        reverse("platform_dashboard", args=[organization.slug]), "agent.updated")


@organization_admin
def workflow_edit(request, organization, pk=None):
    obj = get_object_or_404(organization.workflows, pk=pk) if pk else Workflow(organization=organization)
    return edit_form(request, WorkflowForm(request.POST or None, instance=obj), "Agent workflow",
        reverse("platform_dashboard", args=[organization.slug]), "workflow.updated")


@organization_admin
def run_workflow(request, organization, pk):
    workflow = get_object_or_404(organization.workflows, pk=pk, active=True)
    form = RunForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from .services import feature_enabled
        if not feature_enabled(organization, "core-workflows"):
            form.add_error(None, "Install and enable Core Workflows first.")
        else:
            enqueue(organization, secrets.token_hex(32), workflow.name, "workflow",
                    snapshot_agents(organization, workflow.steps), form.cleaned_data["input"], actor=request.user)
            return redirect("platform_dashboard", slug=organization.slug)
    return render(request, "identity/form.html", {"form": form, "title": f"Run {workflow.name}"})


@organization_admin
def run_detail(request, organization, pk):
    run = get_object_or_404(organization.workflow_runs, pk=pk)
    return render(request, "platform_services/run.html", {"run": run, "steps": run.step_results.all()})


@organization_admin
@require_POST
def retry_run(request, organization, pk):
    with transaction.atomic():
        run = get_object_or_404(organization.workflow_runs.select_for_update(), pk=pk)
        if run.status == "failed":
            run.status = "queued"
            run.error = ""
            run.completed = None
            run.attempts = 0
            run.save(update_fields=["status", "error", "completed", "attempts"])
            audit(request, "workflow.retry_requested", run.pk)
    return redirect("run_detail", slug=organization.slug, pk=pk)
