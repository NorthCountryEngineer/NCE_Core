"""Metadata-only, independent application handover records.

Customer records and snapshot decryption keys never belong in these tables.
"""
import uuid

from django.db import models


class ApplicationTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, related_name='transfers')
    created_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, related_name='+')
    created = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    pairing_secret_hash = models.CharField(max_length=64)
    paired_at = models.DateTimeField(null=True)
    source_identity = models.JSONField(default=dict)
    receiver_claim = models.JSONField(default=dict)
    receiver_claim_hash = models.CharField(max_length=64, blank=True)
    plan = models.JSONField(default=dict)
    plan_hash = models.CharField(max_length=64, blank=True)
    approved_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True, related_name='+')
    approved_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=24, default='offered')
    fence_epoch = models.PositiveBigIntegerField(default=0)
    accepted_sequence = models.PositiveIntegerField(default=0)
    accepted_receipt_hash = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(null=True)


class TransferReceipt(models.Model):
    transfer = models.ForeignKey(ApplicationTransfer, on_delete=models.PROTECT, related_name='receipts')
    sequence = models.PositiveIntegerField()
    stage = models.CharField(max_length=24)
    envelope = models.JSONField()
    digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True)
    accepted_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['transfer', 'sequence'], name='unique_transfer_receipt_sequence')]
        ordering = ['sequence']


class ApplicationPlacementReference(models.Model):
    """A catalog reference, explicitly never a controller or data-fetch target."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.OneToOneField('platform_services.Installation', on_delete=models.PROTECT, related_name='independent_placement')
    transfer = models.OneToOneField(ApplicationTransfer, on_delete=models.PROTECT, related_name='placement')
    destination_origin = models.URLField()
    destination_mode = models.CharField(max_length=24)
    source_identity = models.JSONField()
    plan_hash = models.CharField(max_length=64)
    final_receipt_hash = models.CharField(max_length=64)
    reference_only = models.BooleanField(default=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)
