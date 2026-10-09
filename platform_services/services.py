import secrets
from copy import deepcopy
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F

from identity.models import Application, AuditEvent, Membership, Organization, Service, ServicePermission
from .models import Deployment, Installation, MarketplacePackage, RuntimeNode, WorkloadCredential
from .secrets import encrypt_bundle
from .validation import CAPABILITIES, validate_manifest

CAPABILITY_PACKAGE = {"data.read": "core-data", "data.write": "core-data", "agents.run": "core-agents",
                      "workflows.run": "core-workflows", "workflows.read": "core-workflows",
                      **{c: 'core-chat' for c in ['chat.read', 'chat.send', 'chat.manage', 'chat.ai', 'chat.federate']}}


def feature_enabled(organization, package_slug):
    return organization.active and Installation.objects.filter(organization=organization, organization__active=True, enabled=True,
        service__active=True, package__slug=package_slug, package__active=True).exists()


def credential_allows(credential, capabilities):
    if not credential or not credential.active:
        return False
    installation = credential.installation
    if not installation.enabled or not installation.service.active or not installation.organization.active or not installation.package.active:
        return False
    return all(c in installation.granted_capabilities and c in CAPABILITIES and
               feature_enabled(installation.organization, CAPABILITY_PACKAGE[c]) for c in capabilities)


@transaction.atomic
def install_package(organization, package, grants=(), actor=None, _path=()):
    Organization.objects.select_for_update().get(pk=organization.pk)
    package.full_clean()
    if not package.active or not organization.active:
        raise ValidationError("The organization and package must be active.")
    if set(grants) - set(package.manifest.get("capabilities", [])):
        raise ValidationError("A grant must be declared in the marketplace manifest.")
    existing = Installation.objects.filter(organization=organization, package=package).first()
    if existing:
        return existing
    if package.slug in _path:
        raise ValidationError("Marketplace dependencies contain a cycle.")
    if package.slug != "core-platform":
        install_package(organization, MarketplacePackage.objects.get(slug="core-platform", active=True),
                        actor=actor, _path=(*_path, package.slug))
    for dependency in package.manifest.get("dependencies", []):
        install_package(organization, MarketplacePackage.objects.get(slug=dependency, active=True), actor=actor, _path=(*_path, package.slug))
    service, _ = Service.objects.get_or_create(organization=organization, slug=package.slug, defaults={"name": package.name})
    for capability in package.manifest.get("capabilities", []):
        ServicePermission.objects.get_or_create(service=service, codename=capability.replace(".", "-"), defaults={"name": capability})
    installation = Installation.objects.create(organization=organization, package=package, service=service,
        version=package.version, manifest=deepcopy(package.manifest), granted_capabilities=sorted(set(grants)))
    if package.kind == "core":
        parent = Deployment.objects.filter(installation__organization=organization, installation__package__slug="core-platform",
                                           target__in=["embedded", "kubernetes-controller"]).first()
        Deployment.objects.create(installation=installation, name="control-plane" if package.slug == "core-platform" else package.slug,
            target="embedded", endpoint=settings.ISSUER, parent=parent)
    AuditEvent.objects.create(organization=organization, actor=actor, action="marketplace.installed", target=package.slug)
    return installation


def install_core(organization, actor=None):
    for slug in ["core-platform", "core-data", "core-agents", "core-workflows", "core-chat"]:
        install_package(organization, MarketplacePackage.objects.get(slug=slug), actor=actor)


def issue_workload_credential(installation, name):
    raw = "cw_" + secrets.token_urlsafe(48)
    credential = WorkloadCredential.objects.create(installation=installation, name=name, token_hash=WorkloadCredential.digest(raw))
    return credential, raw


@transaction.atomic
def upgrade_installation(installation, grants, actor):
    package = installation.package
    package.full_clean()
    if package.kind != "application" or not package.active:
        raise ValidationError("Core runtime upgrades are operator releases. Choose an active application release.")
    if set(grants) - set(package.manifest.get("capabilities", [])):
        raise ValidationError("Approve only capabilities declared by this release.")
    for slug in package.manifest.get("dependencies", []):
        install_package(installation.organization, MarketplacePackage.objects.get(slug=slug, active=True), actor=actor)
    installation.version = package.version
    installation.manifest = deepcopy(package.manifest)
    installation.granted_capabilities = list(grants)
    installation.save(update_fields=["version", "manifest", "granted_capabilities"])
    for capability in installation.manifest.get("capabilities", []):
        ServicePermission.objects.get_or_create(service=installation.service, codename=capability.replace(".", "-"), defaults={"name": capability})
    for deployment in installation.deployments.select_related("application"):
        if deployment.application_id:
            deployment.application.redirect_uris = deployment.endpoint + installation.manifest.get("callback_path", "/oauth/callback")
            deployment.application.full_clean()
            deployment.application.save(update_fields=["redirect_uris"])
        deployment.revision += 1
        deployment.healthy = False
        deployment.save(update_fields=["revision", "healthy"])
    AuditEvent.objects.create(organization=installation.organization, actor=actor, action="installation.upgraded", target=str(installation.pk))


@transaction.atomic
def create_deployment(installation, node, endpoint, actor=None, desired_state="running"):
    from .validation import validate_endpoint
    validate_endpoint(endpoint)
    if installation.package.kind != "application" or node.organization_id != installation.organization_id or not node.active:
        raise ValidationError("Choose an active runtime from this organization for an application package.")
    if node.namespace != installation.organization.deployment_configuration.namespace:
        raise ValidationError("Runtime namespace does not match the organization's deployment configuration.")
    if node.deployments.count() >= 100:
        raise ValidationError("Connect another runtime; each runtime supports up to 100 instances.")
    parent = Deployment.objects.filter(installation__organization=installation.organization, installation__package__slug="core-platform",
                                      target__in=["embedded", "kubernetes-controller"]).first()
    config = installation.organization.deployment_configuration
    deployment = Deployment.objects.create(installation=installation, node=node, parent=parent, target="kubernetes",
        name=installation.package.slug, namespace=node.namespace, endpoint=endpoint.rstrip("/"),
        ingress_class=config.ingress_class, tls_secret_name=config.tls_secret_name, desired_state=desired_state)
    callback = installation.manifest.get("callback_path", "/oauth/callback")
    application = Application(service=installation.service, name=f"{installation.package.name} {deployment.pk}",
        client_type="confidential", authorization_grant_type="authorization-code", algorithm="RS256",
        redirect_uris=deployment.endpoint + callback)
    raw_client_secret = application.client_secret
    application.full_clean()
    application.save()
    credential, raw = issue_workload_credential(installation, f"deployment:{deployment.pk}")
    deployment.application = application
    deployment.encrypted_bundle = encrypt_bundle({"workload_token": raw, "oidc_client_secret": raw_client_secret})
    deployment.save(update_fields=["application", "encrypted_bundle"])
    AuditEvent.objects.create(organization=installation.organization, actor=actor, action="deployment.created", target=str(deployment.pk))
    return deployment


def integration_document(installation):
    origin = settings.ISSUER
    return {
        "version": 1, "issuer": origin, "organization_id": str(installation.organization_id),
        "service_id": str(installation.service_id), "audience": installation.service.audience,
        "discovery_url": origin + "/.well-known/openid-configuration",
        "core_services_url": origin + "/api/platform/v1",
        "chat_url": origin + "/api/platform/v1/chat",
        "capabilities": [c for c in installation.granted_capabilities if feature_enabled(installation.organization, CAPABILITY_PACKAGE[c])],
        "services": {i.package.slug: {"service_id": str(i.service_id), "version": i.version}
                     for i in Installation.objects.select_related("package").filter(organization=installation.organization, enabled=True,
                         service__active=True, package__active=True, package__kind="core")},
    }
