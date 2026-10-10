"""Bounded, provider-neutral AI planning contract and reproducible synthetic generator."""
from datetime import datetime, timedelta, timezone
import json
import random
import uuid

from django.core.exceptions import ValidationError
from jsonschema import Draft202012Validator


from training_contract import PLAN_SCHEMA


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_plan(plan):
    try:
        encoded = canonical(plan)
        if len(encoded.encode()) > 65536 or not Draft202012Validator(PLAN_SCHEMA).is_valid(plan):
            raise ValueError
        if sum(case['count'] for case in plan['cases']) > 200 or len({case['name'] for case in plan['cases']}) != len(plan['cases']):
            raise ValueError
        for case in plan['cases']:
            for generator in case['fields'].values():
                if generator['kind'] in {'integer', 'number'} and generator['min'] > generator['max']:
                    raise ValueError
                if generator['kind'] == 'constant' and len(canonical(generator['value']).encode()) > 1024:
                    raise ValueError
                if generator['kind'] == 'timestamp':
                    start = datetime.fromisoformat(generator['start'].replace('Z', '+00:00'))
                    if not start.tzinfo:
                        raise ValueError
                    start + timedelta(seconds=generator.get('step_seconds', 1) * 200)
            for assertion in case.get('assertions', []):
                if assertion['operator'] in {'equals', 'type'} and 'value' not in assertion:
                    raise ValueError
                if assertion['operator'] == 'type' and assertion['value'] not in ['string', 'integer', 'number', 'boolean', 'object', 'array', 'null']:
                    raise ValueError
                if assertion['operator'] == 'equals_input' and not assertion.get('input_field'):
                    raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValidationError('Use a valid bounded simulation plan: up to 20 unique cases, 200 records, and supported generators/assertions.')


def samples(plan):
    return [(case_index, index) for case_index, case in enumerate(plan['cases']) for index in range(case['count'])]


def generate(plan, index):
    case_index, sample = samples(plan)[index]
    case = plan['cases'][case_index]
    rng = random.Random(f'core-training-v1:{plan["seed"]}:{case_index}:{sample}')
    result = {}
    for field, generator in sorted(case['fields'].items()):
        kind = generator['kind']
        if kind == 'constant':
            value = json.loads(canonical(generator['value']))
        elif kind == 'choice':
            value = rng.choice(generator['values'])
        elif kind == 'integer':
            value = rng.randint(generator['min'], generator['max'])
        elif kind == 'number':
            value = round(rng.uniform(generator['min'], generator['max']), 6)
        elif kind == 'boolean':
            value = rng.random() < generator.get('probability', .5)
        elif kind == 'sequence':
            value = generator.get('prefix', 'sim-') + str(generator.get('start', 0) + sample)
        elif kind == 'email':
            value = f'sim-{case_index}-{sample}@example.test'
        elif kind == 'uuid':
            value = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        else:
            start = datetime.fromisoformat(generator['start'].replace('Z', '+00:00'))
            value = (start + timedelta(seconds=generator.get('step_seconds', 1) * sample)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        result[field] = value
    return case, result


MISSING = object()


def at_path(value, path):
    for component in path.split('.'):
        if not isinstance(value, dict) or component not in value:
            return MISSING
        value = value[component]
    return value


def evaluate(case, data, output, accepted, workflow_succeeded=True):
    checks = [{'check': 'ingestion', 'passed': accepted == (case['expected_ingest'] == 'accept')}]
    if accepted:
        checks.append({'check': 'workflow', 'passed': workflow_succeeded})
        for assertion in case.get('assertions', []):
            actual = at_path(output, assertion['field'])
            operator = assertion['operator']
            passed = actual is not MISSING
            if passed and operator == 'equals':
                passed = canonical(actual) == canonical(assertion['value'])
            elif passed and operator == 'equals_input':
                expected = at_path(data, assertion['input_field'])
                passed = expected is not MISSING and canonical(actual) == canonical(expected)
            elif passed and operator == 'type':
                name = {str: 'string', bool: 'boolean', int: 'integer', float: 'number', dict: 'object', list: 'array', type(None): 'null'}[type(actual)]
                passed = name == assertion['value'] or assertion['value'] == 'number' and name == 'integer'
            checks.append({'check': operator, 'field': assertion['field'], 'passed': bool(passed)})
    return {'passed': all(check['passed'] for check in checks), 'checks': checks}
