CORE_CATALOG = [
    ("core-ai", "Core AI", "Permission-aware unified application queries and evidence-first conversations; no provider key required.",
     ["core-data", "core-fabric"], ["ai.query", "ai.manage"]),
    ('core-app-factory', 'Core App Factory', 'Description-led application development with scoped agent accounts and inherited platform security.',
     ['core-agents', 'core-workflows'], ['appfactory.create', 'appfactory.read', 'appfactory.build', 'appfactory.release']),
    ('core-fabric', 'Core Data Fabric', 'Bilateral administrator-approved data federation between Core deployments.',
     ['core-data'], ['fabric.read', 'fabric.write', 'fabric.manage', 'fabric.peer']),
    ("core-platform", "Core Platform", "Identity, organization administration, marketplace, documentation, AI rules, shared usage budgets, and instance management.", [], ['docs.read', 'docs.admin', 'docs.manage', 'rules.read', 'rules.manage', 'usage.reserve', 'usage.report', 'usage.read']),
    ("core-data", "Core Data", "Schema-validated, idempotent ingestion and cursor-based data consumption.", [], ["data.read", "data.write"]),
    ("core-agents", "Core Agents", "Managed agents with an explicit, bounded tool registry.", ["core-data"], ["agents.run", "data.read", "data.write"]),
    ("core-workflows", "Core Workflows", "Durable agents and composable development objects, data fusions, and local ML experiments.", ["core-agents", "core-data"], ["workflows.run", "workflows.read", "data.read", "data.write", 'development.read', 'development.write', 'development.run']),
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
