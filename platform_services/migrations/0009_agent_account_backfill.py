import uuid

from django.db import migrations, models
import django.db.models.deletion


def initialize(apps, schema_editor):
    Account = apps.get_model('platform_services', 'AgentAccount')
    Definition = apps.get_model('platform_services', 'AgentDefinition')
    Listing = apps.get_model('platform_services', 'AgentListing')
    for definition in Definition.objects.filter(account__isnull=True).iterator():
        account = Account.objects.create(id=uuid.uuid4(), organization_id=definition.organization_id, name=definition.name)
        definition.account_id = account.pk
        definition.save(update_fields=['account'])
    # Old queued work has no approved agent identity; it cannot retain inherited human privileges.
    apps.get_model('platform_services', 'WorkflowRun').objects.filter(status__in=['queued', 'running']).update(
        status='failed', error='Agent account approval required. Submit a new run after assigning service roles.', lease_until=None)
    Listing.objects.get_or_create(slug='record-inspector', defaults={'name': 'Record Inspector', 'kind': 'inspect',
        'description': 'Inspect field names and types using a bounded core tool. Runs under its own account with approved service roles.'})
    Listing.objects.get_or_create(slug='field-selector', defaults={'name': 'Field Selector', 'kind': 'select',
        'configuration': {'fields': ['title']},
        'description': 'Select configured fields from a record. Customize the installed definition; approve each consuming application separately.'})


class Migration(migrations.Migration):
    dependencies = [('platform_services', '0008_agentlisting_runstep_agent_account_id_and_more')]
    operations = [migrations.RunPython(initialize, migrations.RunPython.noop),
        migrations.AlterField(model_name='agentdefinition', name='account', field=models.OneToOneField(
            on_delete=django.db.models.deletion.PROTECT, related_name='definition', to='platform_services.agentaccount', blank=True))]
