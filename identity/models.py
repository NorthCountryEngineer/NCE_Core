import hashlib
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from oauth2_provider.models import AbstractApplication


class TaggedResource(models.Model):
    permission_tags = models.ManyToManyField("identity.PermissionTag", blank=True,
        related_name="%(app_label)s_%(class)s_resources")

    class Meta:
        abstract = True


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Platform administrators must have staff and superuser flags.")
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)
    is_developer = models.BooleanField(default=False, help_text="Operator-approved access to organization onboarding")
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def clean(self):
        super().clean()
        self.email = self.email.strip().lower()


class Organization(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    identity_name = models.CharField(max_length=160, blank=True, help_text="Custom name for Core Identity")
    unified_header_enabled = models.BooleanField(default=True, help_text="Default shared header for this organization's applications")
    active = models.BooleanField(default=True)

    @property
    def brand(self):
        return self.identity_name or settings.PRODUCT_NAME

    def __str__(self):
        return self.name


class Membership(TaggedResource):
    class Kind(models.TextChoices):
        ADMINISTRATOR = "administrator", "Organization administrator"
        MEMBER = "member", "Member"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MEMBER)
    active = models.BooleanField(default=True)
    roles = models.ManyToManyField("Role", blank=True, related_name="memberships")
    access_tags = models.ManyToManyField("PermissionTag", blank=True, related_name="member_grants")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="unique_membership")]


class Service(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="services")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    @property
    def audience(self):
        return f"urn:core:service:{self.pk}"

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_org_service")]

    def __str__(self):
        return self.name


class ServicePermission(TaggedResource):
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="permissions")
    codename = models.SlugField(help_text="Examples: read, reports-edit. The admin permission is reserved.")
    name = models.CharField(max_length=160)

    def clean(self):
        if self.codename == "admin":
            raise ValidationError({"codename": "admin is reserved for organization administrators."})

    class Meta:
        constraints = [models.UniqueConstraint(fields=["service", "codename"], name="unique_service_permission")]

    def __str__(self):
        return f"{self.service.slug}: {self.codename}"


class Role(TaggedResource):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    permissions = models.ManyToManyField(ServicePermission, blank=True, related_name="roles")
    access_tags = models.ManyToManyField("PermissionTag", blank=True, related_name="role_grants")

    def clean(self):
        if self.slug in {"administrator", "member"}:
            raise ValidationError({"slug": "This name is reserved for a built-in role."})

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_org_role")]

    def __str__(self):
        return self.name


class Application(AbstractApplication, TaggedResource):
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="applications")
    active = models.BooleanField(default=True)

    def is_usable(self, request):
        return self.active and self.service.active and self.service.organization.active

    def clean(self):
        super().clean()
        if self.authorization_grant_type != self.GRANT_AUTHORIZATION_CODE:
            raise ValidationError("Only the authorization code flow is supported.")
        if self.algorithm != self.RS256_ALGORITHM or not self.hash_client_secret or self.skip_authorization:
            raise ValidationError("Clients require RS256, hashed secrets, and explicit consent.")
        if not self.redirect_uris.strip():
            raise ValidationError({"redirect_uris": "Register at least one exact redirect URL."})
        for uri in self.redirect_uris.split():
            parsed = urlparse(uri)
            if (not parsed.netloc or parsed.username or parsed.fragment or "*" in uri or
                (parsed.scheme != "https" and not (
                    settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                ))):
                raise ValidationError({"redirect_uris": "Use exact HTTPS URLs; HTTP loopback is allowed in development."})


class Invitation(TaggedResource):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    kind = models.CharField(max_length=20, choices=Membership.Kind.choices, default=Membership.Kind.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True)
    expires = models.DateTimeField()
    accepted = models.BooleanField(default=False)

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def usable(self):
        return not self.accepted and self.expires > timezone.now() and self.organization.active


class ProtectedResource(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="protected_resources")
    resource_type = models.SlugField(max_length=80)
    external_id = models.CharField(max_length=255, help_text="Opaque application identifier; do not use patient names or record contents.")
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["service", "resource_type", "external_id"], name="unique_protected_resource")]


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit events are append-only through the application.")

    def delete(self):
        raise ValidationError("Audit events require an operator retention procedure.")


class AuditEvent(models.Model):
    objects = AuditQuerySet.as_manager()
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="audit_events")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)
    request_id = models.UUIDField(null=True, blank=True)
    actor_subject = models.UUIDField(null=True, blank=True)
    principal_id = models.UUIDField(null=True, blank=True)
    principal_kind = models.CharField(max_length=20, default="user")
    outcome = models.CharField(max_length=12, default="allowed", choices=[("allowed", "Allowed"), ("denied", "Denied"), ("error", "Error")])
    reason_code = models.CharField(max_length=60, blank=True)
    purpose_code = models.SlugField(max_length=80, blank=True)
    tags = models.JSONField(default=list, blank=True)

    def save(self, *args, **kwargs):
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit events are append-only through the application.")
        if self.actor_id and not self.actor_subject:
            self.actor_subject = self.actor_id
        if self.actor_id and not self.principal_id:
            self.principal_id = self.actor_id
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events require an operator retention procedure.")

    class Meta:
        ordering = ["-created", "-pk"]


class PermissionTag(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="permission_tag_definitions")
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    classification = models.CharField(max_length=20, default="internal", choices=[
        ("public", "Public"), ("internal", "Internal"), ("confidential", "Confidential"), ("restricted", "Restricted")])
    requires_mfa = models.BooleanField(default=False)
    max_auth_age = models.PositiveIntegerField(null=True, blank=True, help_text="Maximum seconds since verified human authentication; machines cannot satisfy this.")
    allowed_purposes = models.JSONField(default=list, blank=True, help_text='Empty permits any purpose; otherwise exact codes, e.g. ["treatment", "operations"].')
    allow_workloads = models.BooleanField(default=False, help_text="Explicitly permit machine access with this tag grant.")
    active = models.BooleanField(default=True)

    def clean(self):
        import re
        if self.max_auth_age is not None and not 60 <= self.max_auth_age <= 86400:
            raise ValidationError({"max_auth_age": "Use 60–86400 seconds."})
        if (not isinstance(self.allowed_purposes, list) or len(self.allowed_purposes) > 30 or
            any(not isinstance(p, str) or not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", p) for p in self.allowed_purposes)):
            raise ValidationError({"allowed_purposes": "Use up to 30 purpose codes."})

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_permission_tag")]

    def __str__(self):
        return self.name


class AuthenticationPolicy(models.Model):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="authentication_policy")
    require_federated = models.BooleanField(default=False)
    require_mfa = models.BooleanField(default=False)
    max_session_age = models.PositiveIntegerField(default=28800)

    def clean(self):
        if not 60 <= self.max_session_age <= 86400:
            raise ValidationError({"max_session_age": "Use 60–86400 seconds."})


class IdentityProvider(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="identity_providers")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, default="oidc", choices=[("oidc", "Generic OIDC"), ("keycloak", "Keycloak"),
        ("entra", "Microsoft Entra ID"), ("cognito", "Amazon Cognito"), ("google", "Google"), ("core", "Another Core Platform")])
    issuer = models.URLField(max_length=500)
    client_id = models.CharField(max_length=255)
    client_secret_file = models.CharField(max_length=500, help_text="Operator-mounted path under CORE_IDP_SECRET_ROOT; no secret value here.")
    token_auth_method = models.CharField(max_length=24, default="client_secret_basic", choices=[
        ("client_secret_basic", "Client secret Basic"), ("client_secret_post", "Client secret POST")])
    mfa_amr = models.JSONField(default=list, blank=True, help_text='Explicit trusted MFA indicators, e.g. ["mfa", "otp"].')
    mfa_acr = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=False)
    configuration_version = models.PositiveIntegerField(default=1, editable=False)

    def clean(self):
        from .federation import validate_provider
        validate_provider(self)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = IdentityProvider.objects.filter(pk=self.pk).first()
            fields = ["issuer", "client_id", "client_secret_file", "token_auth_method", "mfa_amr", "mfa_acr", "active"]
            if previous and (previous.issuer != self.issuer or previous.client_id != self.client_id) and previous.identities.exists():
                raise ValidationError("Create a new provider when changing an issuer or client ID with linked identities.")
            if previous and any(getattr(previous, f) != getattr(self, f) for f in fields):
                self.configuration_version = previous.configuration_version + 1
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = {*kwargs["update_fields"], "configuration_version"}
        return super().save(*args, **kwargs)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_org_identity_provider")]

    def __str__(self):
        return self.name


class FederatedIdentity(models.Model):
    provider = models.ForeignKey(IdentityProvider, on_delete=models.PROTECT, related_name="identities")
    subject = models.CharField(max_length=255)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="federated_identities")
    active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = FederatedIdentity.objects.filter(pk=self.pk).first()
            if previous and any(getattr(previous, f) != getattr(self, f) for f in ["provider_id", "subject", "user_id", "active"]):
                AuthenticationEvidence.objects.filter(identity=self).update(revoked=True)
        return super().save(*args, **kwargs)

    def clean(self):
        if self.provider_id and self.user_id and not Membership.objects.filter(
                organization_id=self.provider.organization_id, user_id=self.user_id, active=True).exists():
            raise ValidationError("Link an existing active member of this provider's organization.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "subject"], name="unique_provider_subject")]


class AuthenticationEvidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    identity = models.ForeignKey(FederatedIdentity, on_delete=models.PROTECT, null=True, blank=True)
    provider_version = models.PositiveIntegerField(default=0)
    authenticated_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(default=timezone.now)
    mfa = models.BooleanField(default=False)
    amr = models.JSONField(default=list)
    acr = models.CharField(max_length=255, blank=True)
    revoked = models.BooleanField(default=False)


class OAuthEvidence(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=12)
    evidence = models.ForeignKey(AuthenticationEvidence, on_delete=models.CASCADE)
    purpose_code = models.SlugField(max_length=80, blank=True)
    created = models.DateTimeField(default=timezone.now)
