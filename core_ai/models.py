"""Declarative, administrator-owned application query scopes. No stored secrets."""
import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from identity.models import TaggedResource
from platform_services.documentation import validate_permissions


class QuerySource(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE)
    name = models.CharField(max_length=160)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT)
    environment = models.ForeignKey('platform_services.DeploymentEnvironment', on_delete=models.PROTECT)
    dataset = models.ForeignKey('platform_services.Dataset', null=True, blank=True, on_delete=models.PROTECT)
    contract = models.ForeignKey('core_fabric.FabricContract', null=True, blank=True, on_delete=models.PROTECT)
    fields = models.JSONField(help_text='Explicit top-level fields, e.g. ["title", "status"].')
    filters = models.JSONField(default=dict, blank=True, help_text='Fixed exact scalar row filters; callers cannot widen these.')
    required_permissions = models.JSONField(help_text='At least one registered permission on the source application, e.g. ["read"].')
    allowed_roles = models.ManyToManyField('identity.Role', blank=True,
        help_text='Only these roles may query; empty permits organization administrators only.')
    allowed_consumers = models.ManyToManyField('platform_services.Installation', blank=True, related_name='ai_query_scopes',
        help_text='Applications permitted to query this source with a delegated user token. Empty permits the native Core portal only.')
    record_resource_type = models.SlugField(max_length=80, blank=True,
        help_text='Optional row policy: each record needs an active ProtectedResource with this type and external_id equal to its record ID. Local datasets only.')
    active = models.BooleanField(default=True)
    updated = models.DateTimeField(auto_now=True)

    def clean(self):
        from core_fabric.services import canonical
        if bool(self.dataset_id) == bool(self.contract_id):
            raise ValidationError('Choose exactly one local dataset or approved import agreement.')
        if not self.organization_id or not self.installation_id or not self.environment_id:
            raise ValidationError('Choose an organization, application, and environment.')
        for resource in [self.installation, self.environment, self.dataset if self.dataset_id else self.contract]:
            if resource.organization_id != self.organization_id:
                raise ValidationError('Every query source resource must belong to the same organization.')
        if self.installation.package.kind != 'application':
            raise ValidationError('Attribute the source to a marketplace application.')
        from identity.access import effective_tags
        from platform_services.models import DeploymentEnvironment
        resource = self.dataset if self.dataset_id else self.contract
        placements = set(DeploymentEnvironment.objects.filter(organization_id=self.organization_id,
            boundary_tag_id__in=[tag.pk for tag in effective_tags(resource)]).values_list('pk', flat=True))
        if placements != {self.environment_id}:
            raise ValidationError('Assign the dataset or import agreement to exactly the selected environment using its environment tag.')
        if (not isinstance(self.fields, list) or not 1 <= len(self.fields) <= 40 or
                any(not isinstance(field, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}', field) for field in self.fields) or
                len(set(self.fields)) != len(self.fields)):
            raise ValidationError('Choose 1–40 distinct top-level fields.')
        if (not isinstance(self.filters, dict) or len(self.filters) > 20 or
                any(not isinstance(key, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,79}', key) or
                    type(value) not in (str, int, float, bool, type(None)) for key, value in self.filters.items())):
            raise ValidationError('Use at most 20 fixed exact scalar filters.')
        try:
            if len(canonical(self.filters).encode()) > 16384:
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError('Filters must be finite JSON within 16 KiB.')
        validate_permissions(self.required_permissions)
        if not self.required_permissions or set(self.required_permissions) - set(
                self.installation.service.permissions.values_list('codename', flat=True)):
            raise ValidationError('Choose registered source application permissions.')
        if self.contract_id:
            if self.contract.direction != 'import' or not self.contract.allow_read or set(self.fields) - set(self.contract.fields):
                raise ValidationError('A federated source requires a readable import and fields within its approved projection.')
            if self.record_resource_type:
                raise ValidationError('Federated row policy is enforced by the authoritative agreement; local record mappings cannot replace it.')

    def __str__(self):
        return self.name
