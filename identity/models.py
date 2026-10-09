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


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    identity_name = models.CharField(max_length=160, blank=True, help_text="Custom name for Core Identity")
    active = models.BooleanField(default=True)

    @property
    def brand(self):
        return self.identity_name or settings.PRODUCT_NAME

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Kind(models.TextChoices):
        ADMINISTRATOR = "administrator", "Organization administrator"
        MEMBER = "member", "Member"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MEMBER)
    active = models.BooleanField(default=True)
    roles = models.ManyToManyField("Role", blank=True, related_name="memberships")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="unique_membership")]


class Service(models.Model):
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


class ServicePermission(models.Model):
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


class Role(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    slug = models.SlugField()
    name = models.CharField(max_length=160)
    permissions = models.ManyToManyField(ServicePermission, blank=True, related_name="roles")

    def clean(self):
        if self.slug in {"administrator", "member"}:
            raise ValidationError({"slug": "This name is reserved for a built-in role."})

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "slug"], name="unique_org_role")]

    def __str__(self):
        return self.name


class Application(AbstractApplication):
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


class Invitation(models.Model):
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


class AuditEvent(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="audit_events")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-pk"]
