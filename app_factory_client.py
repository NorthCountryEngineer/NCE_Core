"""AI function tools: the host fixes Core, tenant, identity, and development placement."""
from copy import deepcopy
from uuid import UUID


class AppFactoryTools:
    def __init__(self, core, environment):
        if environment not in {'sandbox', 'development', 'staging', 'uat'} or core.environment != environment:
            raise ValueError('Use the authenticated host application development environment.')
        self.core, self.environment = core, environment

    @staticmethod
    def tools():
        resource = {'type': 'object', 'additionalProperties': False, 'required': ['slug', 'name', 'fields'],
            'properties': {'slug': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{0,29}$'},
                'name': {'type': 'string', 'maxLength': 100},
                'fields': {'type': 'object', 'minProperties': 1, 'maxProperties': 20,
                    'propertyNames': {'pattern': '^[a-z][a-z0-9_]{0,29}$'},
                    'additionalProperties': {'enum': ['string', 'integer', 'boolean']}}}}
        parameters = {'type': 'object', 'additionalProperties': False, 'required': ['name', 'description', 'specification'],
            'properties': {'name': {'type': 'string', 'maxLength': 160}, 'description': {'type': 'string', 'maxLength': 12000},
                'specification': {'type': 'object', 'additionalProperties': False, 'required': ['resources'],
                    'properties': {'resources': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': resource}}}}}
        return [{'type': 'function', 'function': {'name': 'core_app_create',
            'description': 'Convert a description into a bounded data-app draft requiring human review. Custom business logic needs a reviewed extension.',
            'parameters': parameters}},
            {'type': 'function', 'function': {'name': 'core_app_status', 'description': 'Read an authorized application development request.',
                'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['project_id'],
                    'properties': {'project_id': {'type': 'string', 'format': 'uuid'}}}}}]

    def execute(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object.')
        if name == 'core_app_create' and set(arguments) == {'name', 'description', 'specification'}:
            return self.core.request('POST', '/app-factory/projects/', json={**deepcopy(arguments), 'environment': self.environment})
        if name == 'core_app_status' and set(arguments) == {'project_id'}:
            return self.core.request('GET', f"/app-factory/projects/{UUID(str(arguments['project_id']))}/")
        raise ValueError('Unknown tool or unsupported arguments. Approval, credentials, execution, and deployment are host/admin controls.')
