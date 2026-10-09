import base64
from urllib.parse import urlparse

from django.conf import settings
from .secrets import decrypt_bundle


def resource_name(deployment):
    return "core-" + deployment.pk.hex


def deployment_spec(deployment):
    organization_id = str(deployment.installation.organization_id)
    spec = {"deployment_id": str(deployment.pk), "organization_id": organization_id, "namespace": deployment.namespace,
        "revision": deployment.revision, "desired_state": deployment.desired_state,
        "mode": "controller" if deployment.target == "kubernetes-controller" else "application"}
    if spec["mode"] == "controller":
        spec.update({"resource_name": deployment.resource_name, "action": deployment.action})
        return spec
    running = deployment.desired_state == "running" and deployment.installation.enabled and deployment.installation.service.active
    spec["desired_state"] = "running" if running else "stopped"
    name = resource_name(deployment)
    labels = {"core.platform/organization": organization_id, "core.platform/deployment": str(deployment.pk)}
    metadata = {"name": name, "namespace": deployment.namespace, "labels": labels}
    if not running:
        spec.update({"resource_name": name, "manifests": []})
        return spec
    bundle = decrypt_bundle(deployment.encrypted_bundle)
    manifest = deployment.installation.manifest
    port = manifest.get("port", 8080)
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": metadata, "type": "Opaque", "data": {
        key: base64.b64encode(bundle[key].encode()).decode() for key in ["workload_token", "oidc_client_secret"]}}
    container = {"name": "application", "image": manifest["image"], "ports": [{"containerPort": port}],
        "env": [{"name": key, "value": value} for key, value in {
            "CORE_ISSUER": settings.ISSUER, "CORE_ORGANIZATION_ID": organization_id,
            "CORE_SERVICE_ID": str(deployment.installation.service_id), "CORE_OIDC_CLIENT_ID": deployment.application.client_id,
            "CORE_SERVICES_URL": settings.ISSUER + "/api/platform/v1", "CORE_WORKLOAD_TOKEN_FILE": "/run/core/workload_token",
            "CORE_OIDC_CLIENT_SECRET_FILE": "/run/core/oidc_client_secret", "CORE_APPLICATION_ORIGIN": deployment.endpoint,
        }.items()],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True, "runAsUser": 10001, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        "volumeMounts": [{"name": "integration", "mountPath": "/run/core", "readOnly": True}, {"name": "scratch", "mountPath": "/tmp"}],
        "readinessProbe": {"httpGet": {"path": manifest.get("health_path", "/health"), "port": port}, "initialDelaySeconds": 2}}
    workload = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
        "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {
            "metadata": {"labels": labels, "annotations": {"core.platform/revision": str(deployment.revision)}},
            "spec": {"automountServiceAccountToken": False, "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container], "volumes": [{"name": "integration", "secret": {"secretName": name, "defaultMode": 288}},
                                                     {"name": "scratch", "emptyDir": {"sizeLimit": "128Mi"}}]}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
        "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": port}]}}
    host = urlparse(deployment.endpoint).hostname
    ingress_spec = {"rules": [{"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix",
        "backend": {"service": {"name": name, "port": {"number": 80}}}}]}}]}
    if deployment.ingress_class:
        ingress_spec["ingressClassName"] = deployment.ingress_class
    if deployment.tls_secret_name:
        ingress_spec["tls"] = [{"hosts": [host], "secretName": deployment.tls_secret_name}]
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": metadata, "spec": ingress_spec}
    spec.update({"resource_name": name, "manifests": [secret, workload, service, ingress]})
    return spec
