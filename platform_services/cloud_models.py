"""Reviewed cloud deployments, separate from runtime credentials and application code."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models
from identity.models import TaggedResource
from .secrets import decrypt_bundle, encrypt_bundle

ARCHETYPES = [('amplify-static', 'Amplify website'), ('amplify-serverless', 'Amplify website and Lambda API'),
              ('amplify-fargate', 'Amplify website and Fargate application')]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CloudPlatform(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE, related_name='cloud_platforms')
    name = models.CharField(max_length=160)
    mode = models.CharField(max_length=10, choices=[('demo', 'Simulation'), ('aws', 'AWS')], default='demo')
    account_id = models.CharField(max_length=12, blank=True)
    region = models.CharField(max_length=30, blank=True)
    role_arn = models.CharField(max_length=255, blank=True)
    encrypted_external_id = models.TextField(blank=True, editable=False)
    runner_project = models.CharField(max_length=255, blank=True)
    connection_arn = models.CharField(max_length=255, blank=True)
    prefix = models.CharField(max_length=16, default='nce')
    allowed_repositories = models.JSONField(default=list)
    config = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_digest = models.CharField(max_length=64, blank=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account_id', 'region', 'prefix'], condition=models.Q(mode='aws'),
            name='cloud_unique_aws_resource_prefix'), models.UniqueConstraint(fields=['role_arn'], condition=models.Q(mode='aws'),
            name='cloud_unique_aws_control_role')]

    @property
    def external_id(self):
        return decrypt_bundle(self.encrypted_external_id).get('external_id', '') if self.encrypted_external_id else ''

    @external_id.setter
    def external_id(self, value):
        self.encrypted_external_id = encrypt_bundle({'external_id': value}) if value else ''

    def fingerprint(self):
        return digest({key: getattr(self, key) for key in ['organization_id', 'mode', 'account_id', 'region', 'role_arn',
            'encrypted_external_id', 'runner_project', 'connection_arn', 'prefix', 'allowed_repositories', 'config']}
            | {'organization_id': str(self.organization_id)})

    def clean(self):
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,15}', self.prefix):
            raise ValidationError('Resource prefixes use 3–16 lowercase letters, digits, and hyphens.')
        if not isinstance(self.allowed_repositories, list) or not self.allowed_repositories or any(
                not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', repo)
                for repo in self.allowed_repositories):
            raise ValidationError('Configure exact approved GitHub repositories.')
        if not isinstance(self.config, dict) or not self.config.get('environment_ids') or not self.config.get('archetypes'):
            raise ValidationError('Choose at least one environment and application template.')
        if set(self.config['archetypes']) - {key for key, _ in ARCHETYPES}:
            raise ValidationError('Unsupported application template.')
        from .models import DeploymentEnvironment
        ids = self.config['environment_ids']
        try:
            if DeploymentEnvironment.objects.filter(organization_id=self.organization_id, active=True, pk__in=ids).count() != len(set(ids)):
                raise ValidationError('Choose active environments in this organization.')
        except (ValueError, TypeError):
            raise ValidationError('Invalid environment identifiers.') from None
        if self.mode == 'aws':
            from .cloud_aws import AWSAdapterError, configuration
            try:
                configuration(self)
                if not self.encrypted_external_id:
                    raise ValidationError('Configure the role external ID.')
            except AWSAdapterError as error:
                raise ValidationError(str(error)) from None

    def save(self, *args, **kwargs):
        if self.verified_digest and self.verified_digest != self.fingerprint():
            self.active, self.verified_at, self.verified_digest = False, None, ''
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class CloudApplication(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE, related_name='cloud_applications')
    platform = models.ForeignKey(CloudPlatform, on_delete=models.PROTECT, related_name='applications')
    environment = models.ForeignKey('platform_services.DeploymentEnvironment', on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=63)
    repository = models.CharField(max_length=160)
    branch = models.CharField(max_length=200, default='main')
    commit = models.CharField(max_length=40)
    archetype = models.CharField(max_length=30, choices=ARCHETYPES)
    public_origin = models.URLField(blank=True)
    config = models.JSONField(default=dict, blank=True)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, null=True, blank=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'slug'], name='cloud_application_org_slug')]

    def clean(self):
        if self.platform.organization_id != self.organization_id or self.environment.organization_id != self.organization_id:
            raise ValidationError('The platform and environment must belong to this organization.')
        if not re.fullmatch(r'[a-f0-9]{40}', self.commit):
            raise ValidationError('Use the exact 40-character GitHub commit SHA.')
        if (not self.branch or self.branch.startswith(('-', '/')) or self.branch.endswith(('/', '.', '.lock')) or
                any(v in self.branch for v in ['..', '@{', '//']) or re.search(r'[\s~^:?*\[\\\x00-\x1f\x7f]', self.branch)):
            raise ValidationError('Invalid branch name.')
        if self.repository not in self.platform.allowed_repositories:
            raise ValidationError('Choose an approved repository.')
        if (str(self.environment_id) not in self.platform.config.get('environment_ids', []) or
                self.archetype not in self.platform.config.get('archetypes', [])):
            raise ValidationError('This environment or template is not configured for the platform.')
        if self.archetype == 'amplify-fargate' and self.platform.mode == 'aws' and self.repository not in self.platform.config.get('fargate_apps', {}):
            raise ValidationError('Approve this repository’s container resource references in the deployment platform first.')
        if self.public_origin:
            value = urlsplit(self.public_origin)
            if value.scheme != 'https' or not value.netloc or value.username or value.password or value.path or value.query or value.fragment:
                raise ValidationError('Use an HTTPS application origin without a path.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in ['organization_id', 'slug', 'platform_id', 'environment_id', 'archetype', 'public_origin']):
            raise ValidationError('Application placement and origin are immutable. Create a separate application to change them.')
        if self.config:
            raise ValidationError('Additional application configuration is not supported by these templates.')

    def __str__(self):
        return self.name


class CloudRelease(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(CloudApplication, on_delete=models.PROTECT, related_name='releases')
    requested_by = models.ForeignKey('identity.User', on_delete=models.PROTECT)
    evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.SET_NULL, null=True, blank=True)
    purpose = models.CharField(max_length=80, blank=True)
    snapshot = models.JSONField(default=dict)
    digest = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=[(v, v.title()) for v in ['draft', 'queued', 'running', 'succeeded', 'failed', 'cancelled']], default='draft')
    steps = models.JSONField(default=list)
    outputs = models.JSONField(default=dict)
    provider_state = models.JSONField(default=dict, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_owner = models.UUIDField(null=True, blank=True)
    next_poll = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=400, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    @property
    def organization(self):
        return self.application.organization

    @property
    def organization_id(self):
        return self.application.organization_id

    @property
    def environment(self):
        return self.application.environment
