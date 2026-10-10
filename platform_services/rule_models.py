"""Central policy and accounting. Usage rows are never exposed through raw admin."""
import uuid
from django.db import models
from identity.models import Organization, TaggedResource


class AIRulePolicy(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='ai_rules')
    baseline = models.JSONField(default=dict, blank=True)
    overrides = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    updated = models.DateTimeField(auto_now=True)


class AIRuleRevision(models.Model):
    policy = models.ForeignKey(AIRulePolicy, on_delete=models.PROTECT, related_name='history')
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    actor_id = models.UUIDField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['policy', 'revision'], name='unique_ai_rule_revision')]


class AIUsageMeter(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    subject = models.CharField(max_length=100)
    day = models.DateField()
    used = models.JSONField(default=dict)
    held = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'subject', 'day'], name='unique_ai_usage_meter')]


class AIUsageReservation(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    policy = models.ForeignKey(AIRulePolicy, on_delete=models.PROTECT)
    installation = models.ForeignKey('Installation', on_delete=models.PROTECT, null=True)
    environment = models.ForeignKey('DeploymentEnvironment', on_delete=models.PROTECT, null=True)
    subject = models.CharField(max_length=100)
    consumer = models.CharField(max_length=100)
    request_key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    operation = models.CharField(max_length=20)
    revision = models.PositiveIntegerField()
    day = models.DateField()
    estimated = models.JSONField()
    actual = models.JSONField(null=True)
    state = models.CharField(max_length=20, default='reserved')
    overrun_acknowledged = models.BooleanField(default=False)
    expires = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'consumer', 'subject', 'request_key'], name='unique_ai_usage_request')]
        indexes = [models.Index(fields=['organization', 'day', 'state'])]
