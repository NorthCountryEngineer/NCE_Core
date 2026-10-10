"""Immutable publisher revisions and policy-tagged organization documentation."""
import uuid
from django.core.exceptions import ValidationError
from django.db import models
from identity.models import Organization, TaggedResource


CATEGORIES = [(value, value.replace('-', ' ').title()) for value in
    ['getting-started', 'user-guide', 'administration', 'api', 'operations', 'security', 'architecture', 'troubleshooting', 'release-notes']]


class DocumentationSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey('MarketplacePackage', on_delete=models.PROTECT, related_name='documentation_sources')
    version = models.CharField(max_length=40)
    slug = models.SlugField(max_length=80)
    title = models.CharField(max_length=160)
    summary = models.CharField(max_length=500)
    category = models.CharField(max_length=30, choices=CATEGORIES)
    audience = models.CharField(max_length=20, choices=[('users', 'Users'), ('administrators', 'Administrators and approved technical readers')], default='users')
    required_permissions = models.JSONField(default=list, blank=True)
    markdown = models.TextField()
    revision = models.PositiveIntegerField(default=1)
    digest = models.CharField(max_length=64, editable=False, blank=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['package', 'version', 'slug', 'revision'], name='unique_documentation_source_revision')]

    def clean(self):
        from .documentation import validate_markdown, validate_permissions, source_digest
        validate_markdown(self.markdown)
        validate_permissions(self.required_permissions)
        if self.revision < 1:
            raise ValidationError('Use a positive documentation revision.')
        old = type(self).objects.filter(pk=self.pk).first()
        fields = ['package_id', 'version', 'slug', 'title', 'summary', 'category', 'audience', 'required_permissions', 'markdown', 'revision']
        if old and any(getattr(old, field) != getattr(self, field) for field in fields):
            raise ValidationError('Published documentation is immutable. Publish another revision; existing organizations review it before inheriting.')
        self.digest = source_digest(self)

    def __str__(self):
        return f'{self.package.name}: {self.title} ({self.version}, revision {self.revision})'


class DocumentationPage(TaggedResource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='documentation_pages')
    installation = models.ForeignKey('Installation', on_delete=models.PROTECT, null=True, blank=True, related_name='documentation_pages')
    environment = models.ForeignKey('DeploymentEnvironment', on_delete=models.PROTECT, null=True, blank=True)
    deployment = models.ForeignKey('Deployment', on_delete=models.PROTECT, null=True, blank=True)
    source = models.ForeignKey(DocumentationSource, on_delete=models.PROTECT, null=True, blank=True)
    scope_key = models.CharField(max_length=160, editable=False, blank=True)
    slug = models.SlugField(max_length=80)
    title = models.CharField(max_length=160)
    summary = models.CharField(max_length=500, blank=True)
    category = models.CharField(max_length=30, choices=CATEGORIES, default='user-guide')
    audience = models.CharField(max_length=20, choices=DocumentationSource._meta.get_field('audience').choices, default='users')
    required_permissions = models.JSONField(default=list, blank=True)
    mode = models.CharField(max_length=20, choices=[('inherit', 'Inherit application documentation'), ('extend', 'Add organization guidance'), ('replace', 'Replace with organization guidance')], default='inherit')
    markdown = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'scope_key', 'slug'], name='unique_organization_documentation_scope')]

    def clean(self):
        from .documentation import validate_markdown, validate_permissions
        from identity.access import resource_organization
        validate_permissions(self.required_permissions)
        if self.markdown:
            validate_markdown(self.markdown)
        if self.mode == 'inherit' and not self.source_id or self.mode != 'inherit' and not self.markdown:
            raise ValidationError('Inherited pages need a published source; organization extensions/replacements need Markdown.')
        for resource in [self.installation, self.environment, self.deployment]:
            if resource and resource_organization(resource).pk != self.organization_id:
                raise ValidationError('Documentation placement must belong to this organization.')
        if self.deployment_id and (self.deployment.installation_id != self.installation_id or self.deployment.environment_id != self.environment_id):
            raise ValidationError('Documentation deployment, installation, and environment must agree.')
        if self.source_id and (not self.installation_id or self.source.package_id != self.installation.package_id or self.active and self.source.version != self.installation.version):
            raise ValidationError('Choose published documentation for the installed application version.')
        self.scope_key = ':'.join(str(value or '-') for value in [self.installation_id, self.environment_id, self.deployment_id])
        old = type(self).objects.filter(pk=self.pk).first()
        if old and any(getattr(old, field) != getattr(self, field) for field in ['organization_id', 'installation_id', 'environment_id', 'deployment_id', 'slug']):
            raise ValidationError('Documentation placement is immutable. Create a separate scoped page.')
        if old and old.source_id and (not self.source_id or self.source.slug != old.source.slug):
            raise ValidationError('Retain the inherited guide’s source identity; choose a reviewed revision of that guide or create a separate organization page.')

    def __str__(self):
        return self.title


class DocumentationRevision(models.Model):
    """Reviewable page history; historical content retains the live page policy."""
    page = models.ForeignKey(DocumentationPage, on_delete=models.PROTECT, related_name='revisions')
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    digest = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['page', 'revision'], name='unique_documentation_page_revision')]
