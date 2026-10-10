import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from identity.models import Organization, Service, User, TaggedResource
from .validation import CORE_PACKAGES, validate_endpoint, validate_manifest, validate_schema, validate_transforms

ENVIRONMENTS = [('sandbox', 'Sandbox'), ('development', 'Development'), ('staging', 'Staging'), ('uat', 'UAT'), ('production', 'Production')]


class DeploymentEnvironment(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='environments')
    name = models.CharField(max_length=20, choices=ENVIRONMENTS)
    namespace = models.CharField(max_length=63)
    application_origin = models.URLField(validators=[validate_endpoint])
    ingress_class = models.CharField(max_length=80, blank=True)
    tls_secret_name = models.CharField(max_length=63, blank=True)
    auto_deploy = models.BooleanField(default=False, help_text='Start newly registered marketplace deployments in this environment.')
    boundary_tag = models.OneToOneField('identity.PermissionTag', on_delete=models.PROTECT, related_name='deployment_environment')
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'name'], name='unique_org_environment'),
                       models.UniqueConstraint(fields=['organization', 'namespace'], name='unique_environment_namespace')]

    def clean(self):
        import re
        for field in ['namespace', 'tls_secret_name']:
            value = getattr(self, field)
            if value and not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value):
                raise ValidationError({field: 'Use a Kubernetes DNS label.'})
        if self.name == 'production' and not self.application_origin.startswith('https://'):
            raise ValidationError({'application_origin': 'Production requires HTTPS.'})
        if self.boundary_tag_id and self.boundary_tag.organization_id != self.organization_id:
            raise ValidationError('The environment tag must belong to this organization.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in ['organization_id', 'name', 'namespace', 'boundary_tag_id']):
            raise ValidationError('Environment identity and namespace are immutable. Create a new environment.')

    def __str__(self):
        return f'{self.get_name_display()} · {self.namespace}'


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
    environment = models.CharField(max_length=20, choices=ENVIRONMENTS)
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
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='private_packages')
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
        if self.kind == 'core' and self.organization_id:
            raise ValidationError('Built-in core packages use the shared marketplace catalog.')
        if self.kind == "application" and (self.slug in CORE_PACKAGES or not (self.manifest.get("image") or self.manifest.get('existing_workload'))):
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
    environment = models.ForeignKey(DeploymentEnvironment, on_delete=models.PROTECT, null=True, blank=True, related_name='runtimes')
    credential_hash = models.CharField(max_length=64, unique=True)
    access_tags = models.ManyToManyField("identity.PermissionTag", blank=True, related_name="runtime_grants")
    active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    cluster_version = models.CharField(max_length=100, blank=True)

    def clean(self):
        if self.environment_id and (self.environment.organization_id != self.organization_id or self.environment.namespace != self.namespace):
            raise ValidationError('Runtime organization and namespace must match its environment.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in ['organization_id', 'namespace', 'environment_id']):
            raise ValidationError('Runtime placement is immutable. Register a separate runtime.')

    @property
    def online(self):
        return self.active and self.last_seen is not None and self.last_seen > timezone.now() - timedelta(seconds=90)


class Deployment(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="deployments")
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    node = models.ForeignKey(RuntimeNode, on_delete=models.PROTECT, null=True, blank=True, related_name="deployments")
    name = models.CharField(max_length=100)
    target = models.CharField(max_length=30, choices=[("embedded", "In core runtime"), ("kubernetes", "Kubernetes"), ("kubernetes-controller", "Existing Kubernetes controller"), ('kubernetes-adopted', 'Modernized Kubernetes application')])
    environment = models.ForeignKey(DeploymentEnvironment, on_delete=models.PROTECT, null=True, blank=True, related_name='deployments')
    resource_uid = models.CharField(max_length=80, blank=True)
    container_name = models.CharField(max_length=63, blank=True)
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
    environment = models.ForeignKey(DeploymentEnvironment, on_delete=models.PROTECT, null=True, blank=True, related_name='credentials')
    deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, null=True, blank=True, related_name='credentials')
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()


class RuntimeApplication(models.Model):
    """Bounded inventory; contains no pod environment variables or Secret contents."""
    node = models.ForeignKey(RuntimeNode, on_delete=models.CASCADE, related_name='applications')
    name = models.CharField(max_length=63)
    resource_uid = models.CharField(max_length=80)
    containers = models.JSONField(default=list)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['node', 'name'], name='unique_runtime_application')]

    def __str__(self):
        return f'{self.node.environment} · {self.name}'


class Dataset(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="datasets")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    schema = models.JSONField(default=dict, blank=True, validators=[validate_schema])
    active = models.BooleanField(default=True)
    data_class = models.CharField(max_length=20, choices=[('operational', 'Operational'), ('synthetic', 'Synthetic / training')], default='operational')

    def clean(self):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).first()
            if original and original.data_class != self.data_class:
                raise ValidationError('Dataset classification is immutable. Create a separate synthetic dataset for training.')

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
    synthetic = models.BooleanField(default=False, editable=False)
    training_run_id = models.UUIDField(null=True, blank=True, editable=False)
    federation = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset", "event_id"], name="unique_data_event")]
        ordering = ["pk"]


class AgentDefinition(TaggedResource):
    KINDS = [("inspect", "Inspect a JSON record"), ("select", "Select record fields"), ("publish", "Publish a data record"),
             ("rules", "Choose a tool using record rules"), ("chat_send", "Send an AI chat message")]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="agents")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=KINDS)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    account = models.OneToOneField('AgentAccount', on_delete=models.PROTECT, related_name='definition', blank=True)

    def save(self, *args, **kwargs):
        # A tool definition never borrows its submitter's account.
        from django.db import transaction
        with transaction.atomic():
            if not self.account_id:
                self.account = AgentAccount.objects.create(organization=self.organization, name=self.name)
            return super().save(*args, **kwargs)

    def clean(self):
        from .agent_tools import validate_configuration
        validate_configuration(self.organization_id, self.kind, self.configuration)
        if self.account_id and self.account.organization_id != self.organization_id:
            raise ValidationError('The agent account must belong to this organization.')

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
    trigger_binding = models.ForeignKey('AgentBinding', on_delete=models.PROTECT, null=True, blank=True)
    trigger_credential = models.ForeignKey('AgentCredential', on_delete=models.PROTECT, null=True, blank=True)
    consumer = models.ForeignKey(Installation, on_delete=models.PROTECT, null=True, blank=True)
    environment = models.CharField(max_length=20, default='development')
    task = models.ForeignKey('AgentTask', on_delete=models.PROTECT, null=True, blank=True)
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
    training_run = models.ForeignKey('TrainingRun', on_delete=models.PROTECT, null=True, blank=True, related_name='workflow_runs')

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "request_id"], name="unique_run_request")]


class RunStep(models.Model):
    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="step_results")
    index = models.PositiveSmallIntegerField()
    agent_slug = models.SlugField()
    tool = models.CharField(max_length=20, blank=True)
    agent_account_id = models.UUIDField(null=True, blank=True)
    output = models.JSONField()
    completed = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "index"], name="unique_run_step")]
        ordering = ["index"]


AGENT_ENVIRONMENTS = ENVIRONMENTS


class AgentListing(models.Model):
    """Platform-curated, versioned agent templates; installation grants no permissions."""
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField()
    version = models.CharField(max_length=40, default='0.1.0')
    kind = models.CharField(max_length=20, choices=AgentDefinition.KINDS)
    configuration = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.name} {self.version}'


class AgentAccount(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='agent_accounts')
    name = models.CharField(max_length=160)
    listing = models.ForeignKey(AgentListing, on_delete=models.PROTECT, null=True, blank=True)
    listing_version = models.CharField(max_length=40, blank=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class AgentTask(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='agent_tasks')
    name = models.CharField(max_length=160)
    description = models.TextField(help_text='Approved task scope and acceptance criteria.')
    resources = models.JSONField(default=dict, blank=True,
        help_text='Explicit resource scope: datasets, conversations, fabric_contracts, and service_resources contain approved UUID lists. Empty lists deny access.')
    installation = models.ForeignKey(Installation, on_delete=models.PROTECT)
    environment = models.CharField(max_length=20, choices=AGENT_ENVIRONMENTS, default='uat')
    expires_at = models.DateTimeField()
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)

    def clean(self):
        from django.apps import apps
        if not isinstance(self.resources, dict) or set(self.resources) - {'datasets', 'conversations', 'service_resources', 'fabric_contracts'}:
            raise ValidationError('Task resources must contain dataset, conversation, fabric contract, or registered service resource ID lists.')
        for key, label in [('datasets', 'platform_services.Dataset'), ('conversations', 'core_chat.Conversation'), ('service_resources', 'identity.ProtectedResource'), ('fabric_contracts', 'core_fabric.FabricContract')]:
            ids = self.resources.get(key, [])
            if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(value, str) for value in ids):
                raise ValidationError('Task resource scopes use lists of at most 100 IDs.')
            try:
                filters = {'service__organization_id': self.organization_id} if key == 'service_resources' else {'organization_id': self.organization_id}
                found = apps.get_model(label).objects.filter(**filters, pk__in=ids).count()
            except (ValueError, ValidationError):
                raise ValidationError('Use valid resource IDs from this organization.')
            if found != len(set(ids)):
                raise ValidationError('Every approved task resource must belong to this organization.')
        if self.installation_id and self.installation.organization_id != self.organization_id:
            raise ValidationError('The task application must belong to this organization.')
        if self.active and self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError('The task expiration must be in the future.')
        if self.approved_by_id and not self.approved_by.is_superuser and not self.organization.memberships.filter(
                user=self.approved_by, user__is_active=True, active=True, kind='administrator').exists():
            raise ValidationError('A current organization administrator must approve the task.')

    def __str__(self):
        return self.name


class AgentBinding(TaggedResource):
    """An administrator's approval for one agent in one consuming application and environment."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(AgentAccount, on_delete=models.PROTECT, related_name='bindings')
    installation = models.ForeignKey(Installation, on_delete=models.PROTECT, related_name='agent_bindings')
    environment = models.CharField(max_length=20, choices=AGENT_ENVIRONMENTS)
    task = models.ForeignKey(AgentTask, on_delete=models.PROTECT, null=True, blank=True)
    roles = models.ManyToManyField('identity.Role', blank=True, related_name='agent_bindings')
    access_tags = models.ManyToManyField('identity.PermissionTag', blank=True, related_name='agent_grants')
    active = models.BooleanField(default=True)

    @property
    def organization(self):
        return self.account.organization

    @property
    def organization_id(self):
        return self.account.organization_id

    def clean(self):
        if self.account_id and self.installation_id and self.account.organization_id != self.installation.organization_id:
            raise ValidationError('The account and consuming application must belong to one organization.')
        if self.account_id and self.active and self.organization.environments.exists() and not self.organization.environments.filter(name=self.environment, active=True).exists():
            raise ValidationError({'environment': 'Register an active deployment environment before approving agents there.'})
        if self.environment == 'uat' and not self.task_id:
            raise ValidationError('UAT access requires an approved, expiring task.')
        if self.task_id and (self.task.organization_id != self.organization_id or
                self.task.installation_id != self.installation_id or self.task.environment != self.environment):
            raise ValidationError('The task must match this application, organization, and environment.')
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and any(getattr(old, f) != getattr(self, f) for f in ('account_id', 'installation_id', 'environment', 'task_id')):
                raise ValidationError('Approval boundaries are immutable. Revoke this approval and create another.')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'installation', 'environment'], condition=models.Q(task__isnull=True), name='unique_agent_placement'),
            models.UniqueConstraint(fields=['account', 'installation', 'environment', 'task'], condition=models.Q(task__isnull=False), name='unique_agent_task_placement'),
        ]

    def __str__(self):
        return f'{self.account} · {self.installation.package.name} · {self.environment}'


class AgentCredential(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    binding = models.ForeignKey(AgentBinding, on_delete=models.PROTECT, related_name='credentials')
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    @property
    def organization(self):
        return self.binding.organization

    @property
    def organization_id(self):
        return self.binding.organization_id


class TrainingScenario(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='training_scenarios')
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    goal = models.TextField(max_length=4000, help_text='Learning objective or acceptance criteria for the AI scenario designer.')
    plan = models.JSONField(help_text='Seeded cases, field generators, and expected outcomes.')
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT)
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, null=True, blank=True)
    runner_binding = models.ForeignKey(AgentBinding, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField(default=1, editable=False)
    active = models.BooleanField(default=True)

    def clean(self):
        from .training_plan import validate_plan
        validate_plan(self.plan)
        if self.pipeline_id and (self.pipeline.organization_id != self.organization_id or self.pipeline.dataset.data_class != 'synthetic'):
            raise ValidationError('Training requires a pipeline into a synthetic dataset in this organization.')
        if self.workflow_id and self.workflow.organization_id != self.organization_id:
            raise ValidationError('Choose a workflow from this organization.')
        if self.runner_binding_id and (self.runner_binding.organization_id != self.organization_id or self.runner_binding.environment == 'production'):
            raise ValidationError('Choose a non-production agent approval from this organization.')
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and any(getattr(old, field) != getattr(self, field) for field in ('organization_id', 'pipeline_id', 'workflow_id', 'runner_binding_id')):
            raise ValidationError('Scenario routing is immutable; create a new scenario to change its destinations or agent.')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'slug'], name='unique_training_scenario')]

    def __str__(self):
        return self.name


class TrainingRun(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='training_runs')
    scenario = models.ForeignKey(TrainingScenario, on_delete=models.PROTECT, related_name='runs')
    runner_binding = models.ForeignKey(AgentBinding, on_delete=models.PROTECT)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    credential = models.ForeignKey(WorkloadCredential, on_delete=models.PROTECT, null=True, blank=True)
    trigger_binding = models.ForeignKey(AgentBinding, on_delete=models.PROTECT, related_name='+', null=True, blank=True)
    trigger_credential = models.ForeignKey(AgentCredential, on_delete=models.PROTECT, null=True, blank=True)
    authentication_evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.PROTECT, null=True, blank=True)
    purpose_code = models.SlugField(max_length=80, blank=True)
    request_id = models.CharField(max_length=160)
    snapshot = models.JSONField()
    status = models.CharField(max_length=20, default='queued')
    lease_owner = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_index = models.PositiveSmallIntegerField(default=0)
    result = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    completed = models.DateTimeField(null=True, blank=True)

    @property
    def pipeline(self):
        return self.scenario.pipeline

    @property
    def workflow(self):
        return self.scenario.workflow

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='unique_training_request')]


class TrainingEvent(TaggedResource):
    run = models.ForeignKey(TrainingRun, on_delete=models.PROTECT, related_name='events')
    index = models.PositiveSmallIntegerField()
    case_name = models.CharField(max_length=80)
    input = models.JSONField()
    record = models.ForeignKey(DataRecord, on_delete=models.PROTECT, null=True, blank=True)
    workflow_run = models.OneToOneField(WorkflowRun, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, default='pending')
    evaluation = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'index'], name='unique_training_event')]
        ordering = ['index']


class TrainingFeedback(TaggedResource):
    run = models.ForeignKey(TrainingRun, on_delete=models.PROTECT, related_name='feedback')
    request_id = models.CharField(max_length=160)
    verdict = models.CharField(max_length=10, choices=[('pass', 'Pass'), ('fail', 'Fail'), ('review', 'Review')])
    rationale = models.TextField(max_length=4000)
    suggestions = models.JSONField(default=list, blank=True)
    principal_kind = models.CharField(max_length=20)
    principal_id = models.UUIDField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'principal_kind', 'principal_id', 'request_id'], name='unique_training_feedback')]
