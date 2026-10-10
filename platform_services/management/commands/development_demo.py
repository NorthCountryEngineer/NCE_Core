"""Create a permission-preserving, local synthetic composable ML example."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from identity.access import Principal, require_access
from identity.models import Organization, User
from platform_services.data import publish
from platform_services.development import administrator, create_object, publish_revision, revision_document
from platform_services.documentation import checksum
from platform_services.models import Dataset, DevelopmentObject, Installation


class Command(BaseCommand):
    help = 'Create reviewed synthetic data/model/workflow demo recipes in sandbox or development. Does not execute experiments.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True)
        parser.add_argument('--administrator', required=True)
        parser.add_argument('--environment', default='sandbox', choices=['sandbox', 'development'])

    @transaction.atomic
    def handle(self, *args, **options):
        from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
        try:
            org = Organization.objects.get(slug=options['organization'])
            user = User.objects.get(email=options['administrator'], is_active=True)
            principal = Principal(user=user)
            administrator(principal, org)
            environment = org.environments.get(name=options['environment'], active=True)
            owner = Installation.objects.get(organization=org, package__slug='core-workflows', enabled=True)
            require_access(principal, environment, 'development.demo')
            schema = {'type': 'object', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}}, 'required': ['x', 'y'], 'additionalProperties': False}
            dataset, _ = Dataset.objects.get_or_create(organization=org, slug=f'development-demo-{environment.name}',
                defaults={'name': 'Development demo synthetic regression', 'data_class': 'synthetic', 'schema': schema})
            require_access(principal, dataset, 'development.demo_data')
            if dataset.data_class != 'synthetic' or dataset.schema != schema or not dataset.active:
                raise CommandError('Existing demo dataset differs; preserve it and choose another environment.')
            boundaries = set(org.environments.filter(boundary_tag__in=dataset.permission_tags.all()).values_list('pk', flat=True))
            if boundaries and boundaries != {environment.pk}:
                raise CommandError('The existing demo dataset belongs to another environment.')
            dataset.permission_tags.add(environment.boundary_tag)
            for index in range(50):
                publish(dataset, f'development-demo-{index}', {'x': index, 'y': 3*index+2})
            def recipe(slug, name, kind, spec):
                obj = DevelopmentObject.objects.filter(organization=org, environment=environment, slug=slug).first()
                if obj:
                    version = obj.versions.get(number=1)
                    if obj.kind != kind or obj.installation_id != owner.pk or version.spec != spec:
                        raise CommandError('Existing demo recipe differs; it was preserved.')
                    revision_document(version, principal)
                else:
                    obj = create_object(org, principal, {'slug': slug, 'name': name, 'kind': kind,
                        'installation_id': str(owner.pk), 'environment_id': str(environment.pk), 'spec': spec})
                    version = obj.versions.get(number=1)
                    publish_revision(version, principal)
                return version
            connection = recipe('demo-connection', 'Demo synthetic connection', 'connection', {'operator': 'dataset', 'inputs': {},
                'config': {'dataset_id': str(dataset.pk), 'schema_digest': checksum(schema), 'limit': 50}})
            model = recipe('demo-model', 'Demo linear regression', 'model', {'operator': 'linear_regression', 'inputs': {'rows': 'rows'},
                'config': {'features': ['x'], 'target': 'y', 'seed': 42, 'epochs': 200, 'ridge': 0}})
            workflow = recipe('demo-workflow', 'Demo connection → ML training', 'workflow', {'operator': 'graph', 'inputs': {},
                'config': {'nodes': [{'name': 'source', 'revision_id': str(connection.pk), 'digest': connection.digest, 'inputs': {}},
                    {'name': 'train', 'revision_id': str(model.pk), 'digest': model.digest, 'inputs': {'rows': 'source'}}], 'output': 'train'}})
            self.stdout.write(f'Created/verified synthetic recipes. Workflow revision: {workflow.pk}. Run in Development operations with inputs {{}}.')
        except (ObjectDoesNotExist, PermissionDenied, ValidationError) as error:
            raise CommandError('Use an existing native administrator, installed Core Workflows, and an active sandbox/development environment.') from error
