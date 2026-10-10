"""Provider-neutral JSON tool schemas; no Django or model-provider dependency."""

def spec(kind, properties=None, required=()):
    return {'type': 'object', 'additionalProperties': False, 'required': ['kind', *required],
        'properties': {'kind': {'const': kind}, **(properties or {})}}


SCALAR = {'type': ['string', 'number', 'boolean', 'null'], 'maxLength': 256}
GENERATORS = {'oneOf': [
    spec('constant', {'value': {}}, ['value']),
    spec('choice', {'values': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': SCALAR}}, ['values']),
    spec('integer', {'min': {'type': 'integer', 'minimum': -1000000000, 'maximum': 1000000000},
        'max': {'type': 'integer', 'minimum': -1000000000, 'maximum': 1000000000}}, ['min', 'max']),
    spec('number', {'min': {'type': 'number', 'minimum': -1000000, 'maximum': 1000000},
        'max': {'type': 'number', 'minimum': -1000000, 'maximum': 1000000}}, ['min', 'max']),
    spec('boolean', {'probability': {'type': 'number', 'minimum': 0, 'maximum': 1}}),
    spec('sequence', {'prefix': {'type': 'string', 'maxLength': 200}, 'start': {'type': 'integer', 'minimum': -1000000, 'maximum': 1000000}}),
    spec('uuid'), spec('email'),
    spec('timestamp', {'start': {'type': 'string', 'maxLength': 40}, 'step_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 86400}}, ['start']),
]}
ASSERTION = {'type': 'object', 'additionalProperties': False, 'required': ['field', 'operator'], 'properties': {
    'field': {'type': 'string', 'pattern': r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$', 'maxLength': 160},
    'operator': {'enum': ['exists', 'equals', 'equals_input', 'type']}, 'value': {},
    'input_field': {'type': 'string', 'pattern': r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$', 'maxLength': 160}}}
PLAN_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['seed', 'cases'], 'properties': {
    'seed': {'type': 'integer', 'minimum': 0, 'maximum': 4294967295},
    'cases': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {'type': 'object', 'additionalProperties': False,
        'required': ['name', 'count', 'fields', 'expected_ingest'], 'properties': {
            'name': {'type': 'string', 'pattern': r'^[-A-Za-z0-9_]{1,80}$'},
            'count': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            'fields': {'type': 'object', 'minProperties': 1, 'maxProperties': 40,
                'propertyNames': {'pattern': r'^[A-Za-z_][A-Za-z0-9_]{0,79}$'}, 'additionalProperties': GENERATORS},
            'expected_ingest': {'enum': ['accept', 'reject']},
            'assertions': {'type': 'array', 'maxItems': 20, 'items': ASSERTION}}}}}}
