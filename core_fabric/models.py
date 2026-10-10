import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from identity.models import TaggedResource


def permit_expiration():
    return timezone.now() + timedelta(seconds=60)


class FabricPeer(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE, related_name='fabric_peers')
    name = models.CharField(max_length=160)
    issuer = models.URLField()
    remote_organization_id = models.UUIDField()
    inbound_installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT,
        help_text='Dedicated local application identity for authenticated calls from this peer.')
    credential_file = models.CharField(max_length=500, help_text='Read-only mounted credential for calls to the peer; enter the path only.')
    active = models.BooleanField(default=True)

    def clean(self):
        from .transport import validate_peer
        validate_peer(self)
        if self.inbound_installation_id and (self.inbound_installation.organization_id != self.organization_id or
                self.inbound_installation.package.kind != 'application'):
            raise ValidationError('Choose a dedicated application installation from this organization.')
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in
                ['organization_id', 'issuer', 'remote_organization_id', 'inbound_installation_id']):
            raise ValidationError('Peer identities are immutable; create a new peer to change the boundary.')

    def __str__(self):
        return self.name

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'inbound_installation'], name='fabric_dedicated_peer')]


class FabricContract(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement_id = models.UUIDField(default=uuid.uuid4, help_text='Shared agreement UUID, identical on both deployments.')
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE, related_name='fabric_contracts')
    name = models.CharField(max_length=160)
    peer = models.ForeignKey(FabricPeer, on_delete=models.PROTECT)
    direction = models.CharField(max_length=10, choices=[('export', 'Share owned data'), ('import', 'Consume shared data')])
    dataset = models.ForeignKey('platform_services.Dataset', on_delete=models.PROTECT, null=True, blank=True)
    remote_dataset_id = models.UUIDField(null=True, blank=True)
    data_class = models.CharField(max_length=20, choices=[('operational', 'Operational'), ('synthetic', 'Synthetic / training')])
    purpose_code = models.SlugField(max_length=80)
    fields = models.JSONField(help_text='Explicit top-level fields shared by the agreement; future fields are excluded.')
    filters = models.JSONField(default=dict, blank=True, help_text='Exact scalar equality conditions on source records.')
    allow_read = models.BooleanField(default=True)
    allow_write = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    allowed_installations = models.ManyToManyField('platform_services.Installation', blank=True, related_name='fabric_contracts')
    allowed_bindings = models.ManyToManyField('platform_services.AgentBinding', blank=True, related_name='fabric_contracts')
    approved_by = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    approved_hash = models.CharField(max_length=64, blank=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)

    def clean(self):
        from .services import terms, validate_terms
        if self.peer_id and self.peer.organization_id != self.organization_id:
            raise ValidationError('The peer must belong to this organization.')
        if self.direction == 'export':
            if not self.dataset_id or self.remote_dataset_id or self.dataset.organization_id != self.organization_id:
                raise ValidationError('Exports require a local dataset and no remote dataset.')
            if self.data_class != self.dataset.data_class:
                raise ValidationError('Classification must match the owned dataset.')
        elif self.direction != 'import' or self.dataset_id or not self.remote_dataset_id:
            raise ValidationError('Imports require the source dataset UUID and no local dataset.')
        validate_terms(terms(self))
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in
                ['organization_id', 'agreement_id', 'peer_id', 'direction', 'dataset_id', 'remote_dataset_id', 'data_class']):
            raise ValidationError('Agreement routing is immutable; create a new agreement to change its boundaries.')

    def save(self, *args, **kwargs):
        from .services import fingerprint
        if self.approved_hash and self.approved_hash != fingerprint(self):
            self.approved_hash, self.approved_by, self.approved_at = '', None, None
            if kwargs.get('update_fields'):
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'approved_hash', 'approved_by', 'approved_at'}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'agreement_id'], name='fabric_local_agreement')]


class FabricPermit(models.Model):
    """Short-lived operation binding. Stores a digest, never record bodies or tokens."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(FabricContract, on_delete=models.CASCADE)
    operation = models.CharField(max_length=10)
    request_hash = models.CharField(max_length=64)
    terms_hash = models.CharField(max_length=64)
    actor = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True)
    credential = models.ForeignKey('platform_services.WorkloadCredential', on_delete=models.PROTECT, null=True)
    agent_binding = models.ForeignKey('platform_services.AgentBinding', on_delete=models.PROTECT, null=True)
    agent_credential = models.ForeignKey('platform_services.AgentCredential', on_delete=models.PROTECT, null=True)
    evidence = models.ForeignKey('identity.AuthenticationEvidence', on_delete=models.PROTECT, null=True)
    expires_at = models.DateTimeField(default=permit_expiration)
    created = models.DateTimeField(auto_now_add=True)
