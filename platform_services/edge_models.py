"""Tenant-bound edge devices and immutable, explicitly approved work."""
import hashlib
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from identity.models import TaggedResource


def secret_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class EdgeDevice(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, related_name='edge_devices')
    environment = models.ForeignKey('platform_services.DeploymentEnvironment', on_delete=models.PROTECT, null=True, blank=True)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    agent_version = models.CharField(max_length=64, blank=True)
    report = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    current_release = models.ForeignKey('EdgeRelease', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    previous_release = models.ForeignKey('EdgeRelease', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def organization(self):
        return self.installation.organization

    @property
    def organization_id(self):
        return self.installation.organization_id

    @property
    def online(self):
        return bool(self.active and self.last_seen_at and self.last_seen_at > timezone.now() - timedelta(seconds=90))

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if self.environment_id and self.environment.organization_id != self.organization_id:
            raise ValidationError('The device environment must belong to its organization.')
        old = type(self).objects.filter(pk=self.pk).values('installation_id', 'environment_id').first()
        if old and (old['installation_id'] != self.installation_id or old['environment_id'] != self.environment_id):
            raise ValidationError('Device placement is immutable. Enroll another device for a new placement.')
        return super().save(*args, **kwargs)


class EdgeEnrollment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(EdgeDevice, on_delete=models.CASCADE, related_name='enrollments')
    code_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('identity.User', on_delete=models.PROTECT)
    evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.PROTECT, null=True)
    application = models.ForeignKey('identity.Application', on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EdgeCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(EdgeDevice, on_delete=models.CASCADE, related_name='credentials')
    token_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class EdgeRelease(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, related_name='edge_releases')
    version = models.CharField(max_length=80)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    artifact_path = models.CharField(max_length=800, editable=False)
    manifest = models.JSONField(default=dict, editable=False)
    created_by = models.ForeignKey('identity.User', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['installation', 'version'], name='edge_unique_release_version')]

    @property
    def organization(self):
        return self.installation.organization

    @property
    def organization_id(self):
        return self.installation.organization_id

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError('Edge releases are immutable. Upload a new version.')
        return super().save(*args, **kwargs)


class EdgeJob(TaggedResource):
    KINDS = [(value, value) for value in ['status.refresh', 'app.command', 'app.deploy', 'app.rollback', 'os.update.check', 'os.update.apply', 'os.reboot']]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(EdgeDevice, on_delete=models.PROTECT, related_name='jobs')
    kind = models.CharField(max_length=32, choices=KINDS)
    arguments = models.JSONField(default=dict)
    request_id = models.UUIDField()
    request_digest = models.CharField(max_length=64)
    requested_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, related_name='+')
    request_evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.PROTECT, null=True, related_name='+')
    request_application = models.ForeignKey('identity.Application', on_delete=models.PROTECT, null=True, related_name='+')
    purpose = models.CharField(max_length=80, blank=True)
    approved_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True, related_name='+')
    approval_evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.PROTECT, null=True, related_name='+')
    approval_application = models.ForeignKey('identity.Application', on_delete=models.PROTECT, null=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, default='awaiting_approval', choices=[(v, v.replace('_', ' ').title()) for v in
        ['awaiting_approval', 'queued', 'claimed', 'running', 'rebooting', 'succeeded', 'failed', 'cancelled', 'uncertain']])
    lease_hash = models.CharField(max_length=64, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    credential = models.ForeignKey(EdgeCredential, on_delete=models.PROTECT, null=True, related_name='jobs')
    attempts = models.PositiveSmallIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    result = models.JSONField(default=dict, blank=True)
    result_digest = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['device', 'request_id'], name='edge_unique_device_request')]
        indexes = [models.Index(fields=['device', 'status', 'created_at'], name='edge_device_job_queue')]

    @property
    def organization(self):
        return self.device.organization

    @property
    def organization_id(self):
        return self.device.organization_id

    @property
    def installation(self):
        return self.device.installation

    def save(self, *args, **kwargs):
        fields = ['device_id', 'kind', 'arguments', 'request_id', 'request_digest', 'requested_by_id',
                  'request_evidence_id', 'request_application_id', 'purpose', 'expires_at']
        old = type(self).objects.filter(pk=self.pk).values(*fields).first()
        if old and any(old[field] != getattr(self, field) for field in fields):
            from django.core.exceptions import ValidationError
            raise ValidationError('Job requests are immutable. Submit a new request for different work.')
        return super().save(*args, **kwargs)
