import json
from functools import wraps

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .data import IdempotencyConflict, publish, transform
from .models import AgentDefinition, Dataset, Deployment, Pipeline, RuntimeNode, Workflow, WorkflowRun, WorkloadCredential
from .services import credential_allows, feature_enabled, integration_document
from .workflows import enqueue, snapshot_agents
from identity.access import Principal, require_access


def response(data, status=200):
    result = JsonResponse(data, status=status)
    result["Cache-Control"] = "no-store"
    if status == 401:
        result["WWW-Authenticate"] = "Bearer"
    return result


def body(request):
    if len(request.body) > 262144:
        raise ValidationError("Request exceeds the 256 KiB limit.")
    try:
        def reject_constant(value):
            raise ValueError("Non-JSON number")
        value = json.loads(request.body, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("Use a JSON object body.")
    if not isinstance(value, dict):
        raise ValidationError("Use a JSON object body.")
    return value


def bearer(request):
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" and 1 <= len(value) <= 256 else None


def workload_api(capabilities=(), methods=("GET",)):
    def decorator(view):
        @csrf_exempt
        @require_http_methods(methods)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            raw = bearer(request)
            credential = WorkloadCredential.objects.select_related("installation__organization", "installation__service", "installation__package").filter(
                token_hash=WorkloadCredential.digest(raw), active=True).first() if raw else None
            if not credential or not credential_allows(credential, []):
                return response({"error": "invalid_credential"}, 401)
            if not credential_allows(credential, capabilities):
                return response({"error": "insufficient_permission"}, 403)
            request.credential = credential
            request.organization = credential.installation.organization
            request.access_principal = Principal(credential=credential, purpose=request.headers.get("X-Core-Purpose", ""),
                                                 request_id=getattr(request, "security_request_id", None))
            try:
                require_access(request.access_principal, credential, "core.api_access")
                return view(request, *args, **kwargs)
            except ValidationError as error:
                return response({"error": "invalid_request", "detail": error.messages[0]}, 400)
            except IdempotencyConflict:
                return response({"error": "idempotency_conflict"}, 409)
            except PermissionDenied:
                return response({"error": "insufficient_permission"}, 403)
        return wrapped
    return decorator


@workload_api()
def integration(request):
    return response(integration_document(request.credential.installation))


@workload_api(["data.write"], methods=("POST",))
def ingest(request, slug):
    pipeline = get_object_or_404(Pipeline.objects.select_related("dataset__organization"),
        organization=request.organization, slug=slug, active=True, dataset__active=True)
    values = body(request)
    require_access(request.access_principal, pipeline, "data.ingest")
    record, created = publish(pipeline.dataset, values.get("event_id"), transform(values.get("data"), pipeline.transforms))
    return response({"record_id": record.pk, "created": created}, 201 if created else 200)


@workload_api(["data.read"])
def records(request, slug):
    dataset = get_object_or_404(Dataset, organization=request.organization, slug=slug, active=True)
    require_access(request.access_principal, dataset, "data.read")
    try:
        after, limit = int(request.GET.get("after", 0)), int(request.GET.get("limit", 100))
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        raise ValidationError("Use a nonnegative cursor and a limit from 1 to 200.")
    rows = list(dataset.records.filter(pk__gt=after)[:limit])
    return response({"records": [{"id": r.pk, "event_id": r.event_id, "data": r.data, "created": r.created.isoformat()} for r in rows],
                     "next_cursor": rows[-1].pk if rows else after})


@workload_api(["agents.run"], methods=("POST",))
def run_agent(request, slug):
    agent = get_object_or_404(AgentDefinition, organization=request.organization, slug=slug, active=True)
    values = body(request)
    require_access(request.access_principal, agent, "agent.submit")
    run, created = enqueue(request.organization, values.get("request_id"), agent.name, "agent",
        snapshot_agents(request.organization, [slug]), values.get("input"), credential=request.credential, principal=request.access_principal)
    return response({"run_id": str(run.pk), "status": run.status, "created": created}, 202 if created else 200)


@workload_api(["workflows.run"], methods=("POST",))
def run_workflow(request, slug):
    workflow = get_object_or_404(Workflow, organization=request.organization, slug=slug, active=True)
    workflow.full_clean()
    values = body(request)
    require_access(request.access_principal, workflow, "workflow.submit")
    run, created = enqueue(request.organization, values.get("request_id"), workflow.name, "workflow",
        snapshot_agents(request.organization, workflow.steps), values.get("input"), credential=request.credential,
        principal=request.access_principal, resource=workflow)
    return response({"run_id": str(run.pk), "status": run.status, "created": created}, 202 if created else 200)


@workload_api()
def run_status(request, pk):
    run = get_object_or_404(WorkflowRun, pk=pk, organization=request.organization)
    if run.credential_id != request.credential.pk and not credential_allows(request.credential, ["workflows.read"]):
        raise PermissionDenied
    require_access(request.access_principal, run, "workflow.result_read")
    return response({"run_id": str(run.pk), "status": run.status, "output": run.output,
        "error": run.error, "steps": list(run.step_results.values("index", "agent_slug", "tool", "output"))})


@csrf_exempt
@require_http_methods(["POST"])
def runtime_sync(request):
    from .kubernetes import deployment_spec
    raw = bearer(request)
    node = RuntimeNode.objects.select_related("organization").filter(active=True, organization__active=True,
        credential_hash=WorkloadCredential.digest(raw)).first() if raw else None
    if node is None:
        return response({"error": "invalid_runtime"}, 401)
    try:
        principal = Principal(runtime=node, request_id=getattr(request, "security_request_id", None))
        require_access(principal, node, "runtime.sync")
        values = body(request)
        reports = values.get("reports", [])
        if not isinstance(reports, list) or len(reports) > 100:
            raise ValidationError("Use at most 100 runtime reports.")
        for report in reports:
            if (not isinstance(report, dict) or report.get("state") not in {"pending", "running", "stopped", "failed", "degraded"} or
                    type(report.get("revision")) is not int or report["revision"] < 1 or type(report.get("healthy")) is not bool):
                raise ValidationError("Invalid runtime report.")
            from uuid import UUID
            UUID(str(report.get("deployment_id")))
        node.last_seen = timezone.now()
        node.cluster_version = str(values.get("cluster_version", ""))[:100]
        node.save(update_fields=["last_seen", "cluster_version"])
        for report in reports:
            # A runtime can observe only its explicitly assigned instances in its namespace.
            deployment = Deployment.objects.filter(pk=report.get("deployment_id"), node=node, namespace=node.namespace,
                installation__organization=node.organization, revision=report["revision"]).first()
            if deployment is None:
                continue
            try:
                require_access(principal, deployment, "runtime.health_report")
            except PermissionDenied:
                continue
            Deployment.objects.filter(pk=deployment.pk, revision=report["revision"]).update(
                observed_state=report["state"], observed_revision=report["revision"], healthy=report["healthy"],
                last_seen=timezone.now(), message="Kubernetes reconciliation failed." if report["state"] == "failed" else "")
        deployments = Deployment.objects.select_related("installation__package", "installation__organization", "installation__service", "application").filter(
            node=node, installation__organization=node.organization, namespace=node.namespace)
        visible = []
        for deployment in deployments:
            try:
                require_access(principal, deployment, "runtime.deployment_config")
            except PermissionDenied:
                continue
            visible.append(deployment_spec(deployment))
        return response({"version": 1, "node_id": str(node.pk), "namespace": node.namespace, "deployments": visible})
    except PermissionDenied:
        return response({"error": "insufficient_permission"}, 403)
    except (ValidationError, ValueError, TypeError):
        return response({"error": "invalid_request"}, 400)
