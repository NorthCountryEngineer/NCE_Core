"""Bundled, versioned platform documentation. No scraping or paid generation."""
from pathlib import Path
from django.db import transaction
from .documentation import inherit_installation, source_digest
from .models import DocumentationSource, Installation, MarketplacePackage

ROOT = Path(__file__).resolve().parent.parent
GUIDES = [
    ('core-ai', 'central-ai', 'Query accessible application data with Core AI', 'user-guide', 'users', 'docs/central-ai.md'),
    ('core-platform', 'getting-started', 'Organization getting started', 'getting-started', 'users', 'docs/organization-guide.md'),
    ('core-platform', 'platform', 'Platform administration and onboarding', 'administration', 'administrators', 'docs/platform.md'),
    ('core-platform', 'security', 'Identity, roles, and classification controls', 'security', 'administrators', 'docs/security.md'),
    ('core-platform', 'deployment', 'Deploy, operate, and recover Core', 'operations', 'administrators', 'docs/deployment.md'),
    ('core-platform', 'modernization', 'Environment isolation and application modernization', 'architecture', 'administrators', 'docs/modernization.md'),
    ('core-platform', 'cicd', 'CI/CD configuration and release controls', 'operations', 'administrators', 'docs/cicd.md'),
    ('core-platform', 'integration', 'OIDC and application security integration', 'api', 'administrators', 'docs/integration.md'),
    ('core-platform', 'application-integrations', 'Shared and application-owned integrations', 'api', 'administrators', 'docs/application-integrations.md'),
    ('core-platform', 'unified-header', 'Shared application header, pins, and Chat', 'user-guide', 'users', 'docs/unified-header.md'),
    ('core-platform', 'user-profile', 'Your Core account profile', 'user-guide', 'users', 'docs/user-profile.md'),
    ('core-platform', 'local-testing', 'Local development and troubleshooting', 'troubleshooting', 'administrators', 'docs/local-testing.md'),
    ('core-platform', 'documentation', 'Documentation inheritance and AI consumption', 'administration', 'administrators', 'docs/organization-documentation.md'),
    ('core-platform', 'ai-rules', 'Inherited AI rules, query budgets, and shared usage', 'administration', 'administrators', 'docs/ai-rules.md'),
    ('core-data', 'data-api', 'Core data ingestion and consumption', 'api', 'administrators', 'docs/platform.md'),
    ('core-agents', 'agent-accounts', 'Agent accounts, marketplace, and task permissions', 'administration', 'administrators', 'docs/agent-accounts.md'),
    ('core-workflows', 'workflows', 'Durable agent workflows', 'operations', 'administrators', 'docs/platform.md'),
    ('core-workflows', 'development-objects', 'Composable data, ML, and agent development operations', 'operations', 'administrators', 'docs/development-objects.md'),
    ('core-chat', 'chat', 'Central and federated chat', 'user-guide', 'administrators', 'docs/chat.md'),
    ('core-training', 'training', 'Synthetic training and evaluations', 'user-guide', 'administrators', 'docs/training.md'),
    ('core-fabric', 'data-fabric', 'Bilateral data federation', 'architecture', 'administrators', 'docs/data-fabric.md'),
    ('core-app-factory', 'app-factory', 'AI-led application creation', 'user-guide', 'administrators', 'docs/app-factory.md'),
]


@transaction.atomic
def sync_bundled_documentation():
    for package_slug, slug, title, category, audience, filename in GUIDES:
        package = MarketplacePackage.objects.select_for_update().get(slug=package_slug)
        text = (ROOT / filename).read_text()
        context = ('# {{platform_name}} · {{organization_name}}\n\n'
            'Application: **{{application_name}}** · Version: **{{application_version}}**\n\n'
            'Environment: **{{environment_name}}** · Namespace: **{{namespace}}**\n\n'
            'Support: {{support_email}}\n\n---\n\n')
        previous = DocumentationSource.objects.filter(package=package, version=package.version, slug=slug).order_by('-revision').first()
        source = DocumentationSource(package=package, version=package.version, slug=slug, title=title, summary=title,
            category=category, audience=audience, markdown=context + text, revision=previous.revision if previous else 1)
        if previous and previous.digest == source_digest(source):
            continue
        source.revision = previous.revision + 1 if previous else 1
        source.full_clean()
        source.save()
    for installation in Installation.objects.select_related('organization', 'package', 'service').filter(enabled=True):
        inherit_installation(installation)
