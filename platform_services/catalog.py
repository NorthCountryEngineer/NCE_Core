CORE_CATALOG = [
    ("core-platform", "Core Platform", "Identity, organization administration, marketplace, and instance management.", [], []),
    ("core-data", "Core Data", "Schema-validated, idempotent ingestion and cursor-based data consumption.", [], ["data.read", "data.write"]),
    ("core-agents", "Core Agents", "Managed agents with an explicit, bounded tool registry.", ["core-data"], ["agents.run", "data.read", "data.write"]),
    ("core-workflows", "Core Workflows", "Durable agent workflows with resumable steps and centralized policy checks.", ["core-agents", "core-data"], ["workflows.run", "workflows.read", "data.read", "data.write"]),
    ("core-chat", "Core Chat", "Shared conversations, application messaging, and permission-checked AI tools across trusted Core instances.", [],
     ["chat.read", "chat.send", "chat.manage", "chat.ai", "chat.federate"]),
    ('core-training', 'Core Training', 'AI-led simulation scenarios, synthetic pipeline data, agent workflows, and reproducible evaluations.',
     ['core-data', 'core-agents', 'core-workflows'], ['training.run', 'training.read', 'training.manage', 'training.evaluate']),
]


def seed_catalog(apps, schema_editor):
    Package = apps.get_model("platform_services", "MarketplacePackage")
    for slug, name, description, dependencies, capabilities in CORE_CATALOG:
        Package.objects.get_or_create(slug=slug, defaults={"name": name, "description": description,
            "kind": "core", "version": "0.1.0", "manifest": {"dependencies": dependencies, "capabilities": capabilities}})
