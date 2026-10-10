import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from identity.models import TaggedResource


class FederationConnection(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE)
    name = models.CharField(max_length=160)
    issuer = models.URLField()
    remote_organization_id = models.UUIDField()
    credential_file = models.CharField(max_length=500)
    active = models.BooleanField(default=True)

    def clean(self):
        from .federation import validate_connection
        validate_connection(self)

    def __str__(self):
        return self.name


class Conversation(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('identity.Organization', on_delete=models.CASCADE, related_name='chat_conversations')
    title = models.CharField(max_length=160)
    ai_enabled = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    connection = models.ForeignKey(FederationConnection, on_delete=models.PROTECT, null=True, blank=True)
    remote_conversation_id = models.UUIDField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(connection__isnull=True, remote_conversation_id__isnull=True) |
            Q(connection__isnull=False, remote_conversation_id__isnull=False)), name='chat_authority_binding')]

    def clean(self):
        if bool(self.connection_id) != bool(self.remote_conversation_id):
            raise ValidationError('A shared conversation requires both connection and remote conversation ID.')
        if self.connection_id and self.connection.organization_id != self.organization_id:
            raise ValidationError('The connection must belong to this organization.')

    def __str__(self):
        return self.title


class Participant(TaggedResource):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey('identity.User', on_delete=models.PROTECT, null=True, blank=True)
    installation = models.ForeignKey('platform_services.Installation', on_delete=models.PROTECT, null=True, blank=True)
    agent = models.ForeignKey('platform_services.AgentAccount', on_delete=models.PROTECT, null=True, blank=True)
    role = models.CharField(max_length=10, choices=[('owner', 'Owner'), ('member', 'Member'), ('reader', 'Reader')], default='member')
    allow_ai = models.BooleanField(default=False)
    federated_issuer = models.URLField(blank=True, help_text='Exact issuer of an approved remote Core instance; services only.')
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(Q(user__isnull=False, installation__isnull=True, agent__isnull=True) |
                Q(user__isnull=True, installation__isnull=False, agent__isnull=True) |
                Q(user__isnull=True, installation__isnull=True, agent__isnull=False)), name='chat_one_participant_identity'),
            models.UniqueConstraint(fields=['conversation', 'user'], condition=Q(user__isnull=False), name='chat_unique_user'),
            models.UniqueConstraint(fields=['conversation', 'installation'], condition=Q(installation__isnull=False), name='chat_unique_installation'),
            models.UniqueConstraint(fields=['conversation', 'agent'], condition=Q(agent__isnull=False), name='chat_unique_agent'),
        ]

    def clean(self):
        from identity.models import Membership
        if sum(bool(value) for value in (self.user_id, self.installation_id, self.agent_id)) != 1:
            raise ValidationError('Choose exactly one user, application installation, or agent account.')
        organization = self.conversation.organization
        if self.user_id and not Membership.objects.filter(organization=organization, user_id=self.user_id, active=True, user__is_active=True).exists():
            raise ValidationError('Choose an active member of this organization.')
        if self.installation_id and self.installation.organization_id != organization.pk:
            raise ValidationError('Choose an installation from this organization.')
        if self.agent_id:
            from platform_services.models import AgentAccount
            if not AgentAccount.objects.filter(pk=self.agent_id, organization=organization, active=True).exists():
                raise ValidationError('Choose an active agent account from this organization.')
        if self.federated_issuer:
            from .federation import validate_issuer
            validate_issuer(self.federated_issuer)
            if not self.installation_id:
                raise ValidationError('Only a service participant can attest a federated sender.')


class Message(TaggedResource):
    id = models.BigAutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT, related_name='messages')
    request_id = models.CharField(max_length=160)
    sender_key = models.CharField(max_length=160)
    actor_kind = models.CharField(max_length=20)
    actor_id = models.CharField(max_length=160)
    origin_issuer = models.URLField()
    via_installation_id = models.UUIDField(null=True, blank=True)
    agent_name = models.CharField(max_length=160, blank=True)
    text = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['conversation', 'sender_key', 'request_id'], name='chat_idempotent_message')]
        ordering = ['id']
