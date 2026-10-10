"""App requests are tenant and environment resources, never executable prompts."""
import uuid
from django.db import models
from identity.models import Organization, TaggedResource, User


class AppProject(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='app_projects')
    environment = models.ForeignKey('DeploymentEnvironment', on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    description = models.TextField()
    specification = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    build = models.OneToOneField('WorkflowRun', on_delete=models.PROTECT, null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    package = models.OneToOneField('MarketplacePackage', on_delete=models.PROTECT, null=True, blank=True)
    source_digest = models.CharField(max_length=64, blank=True)
    verification_reference = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        from .app_factory import validate_specification
        validate_specification(self.specification)
        if self.environment.organization_id != self.organization_id or not self.environment.active or self.environment.name == 'production':
            raise ValidationError('Application development requires an active non-production environment in this organization.')
        if self.build_id and self.build.organization_id != self.organization_id or self.package_id and self.package.organization_id != self.organization_id:
            raise ValidationError('Builds and private packages must belong to this organization.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and (old.build_id or old.environment_id != self.environment_id):
            if any(getattr(old, field) != getattr(self, field) for field in
                   ['organization_id', 'environment_id', 'description', 'name', 'specification']):
                raise ValidationError('Approved requests and environment placement are immutable. Create a new request for revisions.')
