"""Versioned development recipes and protected experiment results."""
import uuid
from django.core.exceptions import ValidationError
from django.db import models
from identity.models import Organization, TaggedResource


class DevelopmentObject(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='development_objects')
    installation = models.ForeignKey('Installation', on_delete=models.PROTECT)
    environment = models.ForeignKey('DeploymentEnvironment', on_delete=models.PROTECT)
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=[(k, k.replace('_', ' ').title()) for k in
        ['connection', 'pipeline', 'fusion', 'model', 'prediction', 'agent', 'workflow']])
    consumers = models.ManyToManyField('Installation', blank=True, related_name='shared_development_objects')
    revision = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'environment', 'slug'], name='unique_development_object')]

    def clean(self):
        if self.installation.organization_id != self.organization_id or self.environment.organization_id != self.organization_id:
            raise ValidationError('Object placement must belong to this organization.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, name) != getattr(self, name) for name in ['organization_id', 'installation_id', 'environment_id', 'kind', 'slug']):
            raise ValidationError('Object identity, kind, owner, and environment are immutable.')

    def __str__(self):
        return f'{self.name} · {self.environment.name}'


class DevelopmentRevision(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    development_object = models.ForeignKey(DevelopmentObject, on_delete=models.PROTECT, related_name='versions')
    number = models.PositiveIntegerField()
    spec = models.JSONField()
    digest = models.CharField(max_length=64)
    dependencies = models.ManyToManyField('self', symmetrical=False, blank=True, related_name='dependents')
    resources = models.JSONField(default=dict)
    published = models.BooleanField(default=False)
    author = models.CharField(max_length=100)
    reviewer = models.CharField(max_length=100, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['development_object', 'number'], name='unique_development_revision')]

    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in
                ['development_object_id', 'number', 'spec', 'digest', 'resources', 'author']):
            raise ValidationError('Revision content is immutable. Create a new revision.')
        return super().save(*args, **kwargs)


class DevelopmentRun(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    object_revision = models.ForeignKey(DevelopmentRevision, on_delete=models.PROTECT, related_name='runs')
    installation = models.ForeignKey('Installation', on_delete=models.PROTECT, null=True)
    environment = models.ForeignKey('DeploymentEnvironment', on_delete=models.PROTECT)
    subject = models.CharField(max_length=100)
    request_key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=20, default='running')
    result = models.JSONField(default=dict)
    lineage = models.JSONField(default=list)
    error = models.CharField(max_length=80, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'subject', 'installation', 'request_key'], name='unique_development_run'),
            models.UniqueConstraint(fields=['organization', 'subject', 'request_key'], condition=models.Q(installation__isnull=True), name='unique_native_development_run')]
