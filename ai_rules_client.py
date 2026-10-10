"""Provider-neutral, fail-closed budget wrapper for trusted application/agent hosts.

AI tools may read rules and usage. Hosts choose attribution, pricing, bounds,
and financial approval; the model cannot select them through tool arguments.
"""
import hashlib
import json
from uuid import UUID
from core_client import CoreServiceError


class AIRules:
    def __init__(self, core, *, user_access_token=None):
        self.core, self.user_access_token = core, user_access_token

    def request(self, method, path, **kwargs):
        value = self.core.request(method, '/ai-rules/' + path, user_access_token=self.user_access_token, **kwargs)
        if value.get('organization_id') != self.core.organization_id:
            raise CoreServiceError('Rule response belongs to a different organization.')
        return value

    def read(self):
        value = self.request('GET', '')
        expected = hashlib.sha256(json.dumps({key: item for key, item in value.items() if key != 'digest'},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if (value.get('digest') != expected or value.get('schema_version') != 1 or value.get('currency') != 'USD' or
                self.core.environment and value.get('environment') != self.core.environment):
            raise CoreServiceError('Invalid rule schema, digest, currency, or deployment environment.')
        return value

    def reserve(self, request_key, operation, estimated, *, query=None):
        return self.request('POST', 'reservations/', json={'request_key': request_key, 'operation': operation,
            'estimated': estimated, 'query': query})

    def update(self, reservation_id, action, *, actual=None):
        values = {'action': action}
        if actual is not None:
            values['actual'] = actual
        return self.request('POST', 'reservations/' + str(UUID(str(reservation_id))) + '/', json=values)

    def execute(self, request_key, operation, estimated, callback, *, query=None, financial_approval=None):
        """Callback receives enforced bounds and returns (result, actual_usage).

        It must configure the provider's output cap/query timeout before effects.
        A callback error or unknown network outcome retains the hold. Retrying
        the same key never re-executes an already started operation.
        """
        rules = self.read()
        if estimated.get('cost_micros', 0) and (not callable(financial_approval) or
                financial_approval(estimated['cost_micros'], rules['currency']) is not True):
            raise CoreServiceError('The trusted host must obtain financial approval before paid work.')
        row = self.reserve(request_key, operation, estimated, query=query)
        if row.get('state') != 'reserved':
            raise CoreServiceError('Operation has already started or completed; recover its result instead of repeating effects.')
        self.update(row['id'], 'start')
        result, actual = callback(dict(row['estimated']), dict(query) if query else None)
        outcome = self.update(row['id'], 'settle', actual=actual)
        if outcome.get('state') == 'overrun':
            raise CoreServiceError('Usage exceeded its reservation; central admission is paused for administrator review.')
        return result

    @staticmethod
    def tool_definitions():
        return [{'type': 'function', 'function': {'name': 'core_ai_rules',
            'description': 'Read inherited AI constraints, own shared quota, and reset time. This grants no operation or spending authority.',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}]

    def call_tool(self, name, arguments):
        if name != 'core_ai_rules' or arguments != {}:
            raise ValueError('Use the fixed read-only rule tool with no arguments.')
        return self.read()
