import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from identity.models import Organization, Service, User, TaggedResource
from .validation import CORE_PACKAGES, validate_endpoint, validate_manifest, validate_schema, validate_transforms


class OrganizationProfile(TaggedResource):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="profile")
    legal_name = models.CharField(max_length=200)
    country = models.CharField(max_length=2)
    support_email = models.EmailField()
    timezone = models.CharField(max_length=80, default="UTC")


class BillingProfile(TaggedResource):
    PROVIDERS = [("placeholder", "Configure later"), ("stripe", "Stripe"), ("paddle", "Paddle"), ("adyen", "Adyen"), ("braintree", "Braintree")]
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="billing")
    provider = models.CharField(max_length=20, choices=PROVIDERS, default="placeholder")
    billing_email = models.EmailField()
    currency = models.CharField(max_length=3, default="USD")
    # A selected provider is a preference, never evidence of a payment integration.
    status = models.CharField(max_length=30, default="not_configured", editable=False)
    provider_reference = models.CharField(max_length=255, blank=True, editable=False)


class Onboarding(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="onboarding")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True)
    step = models.PositiveSmallIntegerField(default=1)
    complete = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    runtime = models.ForeignKey("RuntimeNode", on_delete=models.SET_NULL, null=True, blank=True)


class DeploymentConfiguration(TaggedResource):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="deployment_configuration")
    environment = models.CharField(max_length=20, choices=[("development", "Development"), ("staging", "Staging"), ("production", "Production")])
    namespace = models.CharField(max_length=63)
    application_origin = models.URLField(validators=[validate_endpoint])
    ingress_class = models.CharField(max_length=80, blank=True)
    tls_secret_name = models.CharField(max_length=63, blank=True)
    auto_deploy = models.BooleanField(default=False)

    def clean(self):
        import re
        for field in ["namespace", "tls_secret_name"]:
            value = getattr(self, field)
            if value and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
                raise ValidationError({field: "Use a Kubernetes DNS label."})
        if self.environment == "production" and not self.application_origin.startswith("https://"):
            raise ValidationError({"application_origin": "Production requires HTTPS."})


class MarketplacePackage(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    version = models.CharField(max_length=40, default="0.1.0")
    description = models.TextField()
    kind = models.CharField(max_length=20, choices=[("core", "Core service"), ("application", "Application")])
    manifest = models.JSONField(default=dict, validators=[validate_manifest])
    active = models.BooleanField(default=True)

    def clean(self):
        if not isinstance(self.manifest, dict):
            raise ValidationError("A marketplace manifest must be an object.")
        if self.kind == "core" and self.slug not in CORE_PACKAGES:
            raise ValidationError("Unknown built-in core package.")
        if self.kind == "application" and (self.slug in CORE_PACKAGES or not self.manifest.get("image")):
            raise ValidationError("Applications require a digest-pinned image and a nonreserved slug.")

    def __str__(self):
        return f"{self.name} {self.version}"


class Installation(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="installations")
    package = models.ForeignKey(MarketplacePackage, on_delete=models.PROTECT)
    service = models.OneToOneField(Service, on_delete=models.PROTECT)
    version = models.CharField(max_length=40)
    manifest = models.JSONField(default=dict)
    access_tags = models.ManyToManyField("identity.PermissionTag", blank=True, related_name="workload_grants")
    granted_capabilities = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "package"], name="unique_package_installation")]


class RuntimeNode(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="runtime_nodes")
    name = models.CharField(max_length=100)
    namespace = models.CharField(max_length=63)
    credential_hash = models.CharField(max_length=64, unique=True)
    access_tags = models.ManyToManyField("identity.PermissionTag", blank=True, related_name="runtime_grants")
    active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    cluster_version = models.CharField(max_length=100, blank=True)

    @property
    def online(self):
        return self.active and self.last_seen is not None and self.last_seen > timezone.now() - timedelta(seconds=90)


class Deployment(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="deployments")
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    node = models.ForeignKey(RuntimeNode, on_delete=models.PROTECT, null=True, blank=True, related_name="deployments")
    name = models.CharField(max_length=100)
    target = models.CharField(max_length=30, choices=[("embedded", "In core runtime"), ("kubernetes", "Kubernetes"), ("kubernetes-controller", "Existing Kubernetes controller")])
    resource_name = models.CharField(max_length=63, blank=True)
    action = models.CharField(max_length=20, default="reconcile")
    namespace = models.CharField(max_length=63, blank=True)
    endpoint = models.URLField(blank=True)
    ingress_class = models.CharField(max_length=80, blank=True)
    tls_secret_name = models.CharField(max_length=63, blank=True)
    desired_state = models.CharField(max_length=20, default="running", choices=[("running", "Running"), ("stopped", "Stopped")])
    revision = models.PositiveIntegerField(default=1)
    observed_revision = models.PositiveIntegerField(default=0)
    observed_state = models.CharField(max_length=20, default="pending")
    last_seen = models.DateTimeField(null=True, blank=True)
    healthy = models.BooleanField(default=False)
    message = models.CharField(max_length=300, blank=True)
    # Contains the integration bundle, encrypted with the deployment's secret key.
    encrypted_bundle = models.TextField(blank=True)
    application = models.OneToOneField("identity.Application", on_delete=models.SET_NULL, null=True, blank=True)

    @property
    def status(self):
        if self.last_seen and self.last_seen < timezone.now() - timedelta(seconds=90):
            return "stale"
        if self.observed_revision < self.revision:
            return "pending"
        return self.observed_state


class WorkloadCredential(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="credentials")
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()


class Dataset(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="datasets")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    schema = models.JSONField(default=dict, blank=True, validators=[validate_schema])
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_dataset")]

    def __str__(self):
        return self.name


class Pipeline(TaggedResource):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="pipelines")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name="pipelines")
    transforms = models.JSONField(default=list, blank=True, validators=[validate_transforms])
    active = models.BooleanField(default=True)

    def clean(self):
        if self.dataset_id and self.dataset.organization_id != self.organization_id:
            raise ValidationError("The dataset must belong to this organization.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_pipeline")]


class DataRecord(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="records")
    event_id = models.CharField(max_length=160)
    data = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset", "event_id"], name="unique_data_event")]
        ordering = ["pk"]


class AgentDefinition(TaggedResource):
    KINDS = [("inspect", "Inspect a JSON record"), ("select", "Select record fields"), ("publish", "Publish a data record"),
             ("rules", "Choose a tool using record rules")]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="agents")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=KINDS)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    def clean(self):
        from .agent_tools import validate_configuration
        validate_configuration(self.organization_id, self.kind, self.configuration)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_agent")]


class Workflow(TaggedResource):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="workflows")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    steps = models.JSONField(default=list, help_text='Ordered agent slugs, e.g. ["select-fields", "publish-record"]')
    active = models.BooleanField(default=True)

    def clean(self):
        if not isinstance(self.steps, list) or not 1 <= len(self.steps) <= 20 or any(not isinstance(s, str) for s in self.steps):
            raise ValidationError("Use a list of 1–20 agent slugs.")
        if set(self.steps) - set(AgentDefinition.objects.filter(organization_id=self.organization_id, active=True).values_list("slug", flat=True)):
            raise ValidationError("Every agent must be active in this organization.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_workflow")]


class WorkflowRun(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="workflow_runs")
    credential = models.ForeignKey(WorkloadCredential, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    request_id = models.CharField(max_length=160)
    definition_name = models.CharField(max_length=160)
    source = models.CharField(max_length=20, choices=[("agent", "Agent"), ("workflow", "Workflow")])
    source_workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, null=True, blank=True)
    snapshot = models.JSONField()
    input = models.JSONField(default=dict)
    output = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, default="queued")
    purpose_code = models.SlugField(max_length=80, blank=True)
    authentication_evidence = models.ForeignKey("identity.AuthenticationEvidence", on_delete=models.PROTECT, null=True, blank=True)
    required_capabilities = models.JSONField(default=list)
    lease_owner = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "request_id"], name="unique_run_request")]


class RunStep(models.Model):
    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="step_results")
    index = models.PositiveSmallIntegerField()
    agent_slug = models.SlugField()
    tool = models.CharField(max_length=20, blank=True)
    output = models.JSONField()
    completed = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "index"], name="unique_run_step")]
        ordering = ["index"]
