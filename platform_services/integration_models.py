"""Tenant-owned integration catalog, environment connections, and consumer grants."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from identity.models import Organization, Service, TaggedResource
from .integration_validation import validate_configuration, validate_configuration_schema


def immutable(instance, keys):
    old = type(instance).objects.filter(pk=instance.pk).first()
    if old and any(getattr(old, key) != getattr(instance, key) for key in keys):
        raise ValidationError('Published identity, schema, and placement are immutable. Create a new record.')


class IntegrationDefinition(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='integration_definitions')
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    provider = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, null=True, blank=True, related_name='published_integrations')
    slug = models.SlugField(max_length=30)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=[('api', 'External API'), ('application', 'Application'), ('core', 'Core service')], default='api')
    configuration_schema = models.JSONField(default=dict)
    active = models.BooleanField(default=True)

    @property
    def use_permission(self):
        return 'integration-' + self.slug + '-use'

    class Meta:
        app_label = 'platform_services'
        constraints = [models.UniqueConstraint(fields=['organization', 'slug'], name='unique_org_integration_definition')]

    def clean(self):
        validate_configuration_schema(self.configuration_schema)
        if self.service_id and self.service.organization_id != self.organization_id:
            raise ValidationError('The authorization service must belong to this organization.')
        if self.provider_id and (self.provider.organization_id != self.organization_id or self.provider.service_id != self.service_id):
            raise ValidationError('The provider and authorization service must match this organization.')
        if self.kind != 'api' and not self.provider_id:
            raise ValidationError('Application and Core integrations require an installed provider.')
        if self.provider_id and self.kind != self.provider.package.kind:
            raise ValidationError('Select the provider package kind for application and Core integrations.')
        immutable(self, ['organization_id', 'service_id', 'provider_id', 'slug', 'kind', 'configuration_schema'])

    def __str__(self):
        return self.name


class IntegrationConnection(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='integration_connections')
    definition = models.ForeignKey(IntegrationDefinition, on_delete=models.PROTECT, related_name='connections')
    environment = models.ForeignKey('platform_services.DeploymentEnvironment', on_delete=models.PROTECT)
    configuration = models.JSONField(default=dict, blank=True)
    encrypted_credential = models.TextField(blank=True, editable=False)
    active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        app_label = 'platform_services'
        constraints = [models.UniqueConstraint(fields=['definition', 'environment'], name='unique_platform_integration_connection')]

    def clean(self):
        if self.definition_id:
            if self.definition.organization_id != self.organization_id:
                raise ValidationError('The integration must belong to this organization.')
            validate_configuration(self.configuration, self.definition.configuration_schema)
        if self.environment_id and self.environment.organization_id != self.organization_id:
            raise ValidationError('The environment must belong to this organization.')
        immutable(self, ['organization_id', 'definition_id', 'environment_id'])

    def __str__(self):
        return f'{self.definition.name} · {self.environment.get_name_display()}'


class ApplicationIntegration(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='application_integrations')
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.CASCADE, related_name='integrations')
    definition = models.ForeignKey(IntegrationDefinition, on_delete=models.PROTECT)
    environment = models.ForeignKey('platform_services.DeploymentEnvironment', on_delete=models.PROTECT)
    mode = models.CharField(max_length=20, choices=[('disabled', 'Disabled'), ('platform', 'Inherit platform connection'), ('application', 'Application configuration')], default='disabled')
    connection = models.ForeignKey(IntegrationConnection, on_delete=models.PROTECT, null=True, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    encrypted_credential = models.TextField(blank=True, editable=False)
    allow_inheritance = models.BooleanField(default=False)
    allow_application_configuration = models.BooleanField(default=False)
    workload_access = models.BooleanField(default=False, help_text='Permit the application service account to consume this integration.')
    credential_access = models.BooleanField(default=False, help_text='Permit the trusted application backend to retrieve the selected encrypted credential. Agents cannot retrieve credentials.')
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        app_label = 'platform_services'
        constraints = [models.UniqueConstraint(fields=['installation', 'definition', 'environment'], name='unique_application_integration')]

    def clean(self):
        for key in ['installation', 'definition', 'environment', 'connection']:
            item = getattr(self, key, None)
            if item and item.organization_id != self.organization_id:
                raise ValidationError('Every integration resource must belong to this organization.')
        if self.connection_id and (self.connection.definition_id != self.definition_id or self.connection.environment_id != self.environment_id):
            raise ValidationError('The platform connection must match the integration and environment.')
        if self.mode == 'platform' and (not self.allow_inheritance or not self.connection_id):
            raise ValidationError('Platform inheritance requires explicit approval and a selected connection.')
        if self.mode == 'application' or self.configuration:
            validate_configuration(self.configuration, self.definition.configuration_schema)
        immutable(self, ['organization_id', 'installation_id', 'definition_id', 'environment_id'])

    def __str__(self):
        return f'{self.installation.package.name} · {self.definition.name} · {self.environment.get_name_display()}'
