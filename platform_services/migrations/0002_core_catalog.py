from django.db import migrations


def seed(apps, schema_editor):
    Package = apps.get_model("platform_services", "MarketplacePackage")
    entries = [
        ("core-platform", "Core Platform", "Identity, administration, marketplace, and instance management.", [], []),
        ("core-data", "Core Data", "Schema-validated ingestion and cursor-based data consumption.", [], ["data.read", "data.write"]),
        ("core-agents", "Core Agents", "Managed agents with an explicit bounded tool registry.", ["core-data"], ["agents.run", "data.read", "data.write"]),
        ("core-workflows", "Core Workflows", "Durable workflows with resumable agent steps.", ["core-agents", "core-data"], ["workflows.run", "workflows.read", "data.read", "data.write"]),
    ]
    for slug, name, description, dependencies, capabilities in entries:
        Package.objects.get_or_create(slug=slug, defaults={"name": name, "description": description, "version": "0.1.0", "kind": "core",
            "manifest": {"dependencies": dependencies, "capabilities": capabilities}})


class Migration(migrations.Migration):
    dependencies = [("platform_services", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
