"""Typed, bounded, deterministic operators. No scripts, SQL, or remote model calls."""
from copy import deepcopy
import math
import random
import re
from django.core.exceptions import ValidationError
from .documentation import checksum

MAX_ROWS = 200
OPERATORS = {
    'dataset': ('connection', {}, 'rows', {'dataset_id', 'schema_digest', 'limit'}),
    'integration': ('connection', {}, 'connection', {'integration_id'}),
    'transform': ('pipeline', {'rows': 'rows'}, 'rows', {'fields', 'rename', 'where'}),
    'pipeline': ('pipeline', {'rows': 'rows'}, 'rows', {'pipeline_id', 'transforms_digest'}),
    'union': ('fusion', {'left': 'rows', 'right': 'rows'}, 'rows', set()),
    'join': ('fusion', {'left': 'rows', 'right': 'rows'}, 'rows', {'left_key', 'right_key', 'right_prefix'}),
    'linear_regression': ('model', {'rows': 'rows'}, 'model', {'features', 'target', 'seed', 'epochs', 'ridge'}),
    'predict': ('prediction', {'rows': 'rows', 'model': 'model'}, 'rows', {'field'}),
    'agent': ('agent', {'rows': 'rows'}, 'rows', {'agent_id', 'binding_id', 'definition_digest'}),
}
TYPES = {'rows', 'model', 'connection'}


def fail(message):
    raise ValidationError(message)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]{0,63}', value):
        fail('Use a field or port name of at most 64 letters, digits, and underscores.')
    return value


def scalar(value):
    if value is None or type(value) is bool:
        return True
    if isinstance(value, str):
        return len(value) <= 4096
    return type(value) in (int, float) and abs(value) <= 1e12 and math.isfinite(value)


def rows(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS or any(not isinstance(row, dict) or len(row) > 64 or
            any(not isinstance(key, str) or len(key) > 160 or not scalar(item) for key, item in row.items()) for row in value):
        fail('Use at most 200 flat records, 64 fields each, and bounded finite scalar values.')
    return value


def shape(spec, kind):
    if not isinstance(spec, dict) or set(spec) != {'operator', 'inputs', 'config'} or not isinstance(spec['config'], dict):
        fail('A recipe has exactly operator, inputs (port/type map), and config.')
    ports = spec['inputs']
    if not isinstance(ports, dict) or len(ports) > 8 or any(identifier(key) != key or value not in TYPES for key, value in ports.items() if isinstance(value, str)) or any(not isinstance(v, str) for v in ports.values()):
        fail('Use at most eight typed input ports.')
    op, config = spec['operator'], spec['config']
    if not isinstance(op, str):
        fail('Use a registered operator.')
    if op == 'graph':
        if kind != 'workflow' or set(config) != {'nodes', 'output'} or not isinstance(config['nodes'], list) or not 1 <= len(config['nodes']) <= 24:
            fail('A workflow graph requires 1–24 nodes and an output node name.')
        return None
    if op not in OPERATORS or OPERATORS[op][0] != kind:
        fail('The operator must match the object kind.')
    _, required, output, keys = OPERATORS[op]
    if ports != required or set(config) != keys:
        fail('Use the operator’s exact typed inputs and configuration keys.')
    for key in ['dataset_id', 'integration_id', 'binding_id', 'pipeline_id']:
        if key in config:
            from uuid import UUID
            try:
                if key == 'pipeline_id':
                    if type(config[key]) is not int or config[key] < 1:
                        fail('Pipeline identifiers are positive integers.')
                else:
                    UUID(config[key])
            except (ValueError, TypeError, AttributeError):
                fail('Resource references are UUIDs.')
    for key in ['schema_digest', 'transforms_digest', 'definition_digest']:
        if key in config and (not isinstance(config[key], str) or not re.fullmatch('[a-f0-9]{64}', config[key])):
            fail('Resource definitions must be pinned by SHA-256 digest.')
    if op == 'dataset' and (type(config['limit']) is not int or not 1 <= config['limit'] <= MAX_ROWS):
        fail('A connection reads at most 200 records per experiment.')
    if op == 'transform':
        fields, rename, where = config['fields'], config['rename'], config['where']
        if not isinstance(fields, list) or not 1 <= len(fields) <= 64 or any(identifier(f) != f for f in fields) or len(set(fields)) != len(fields):
            fail('Choose 1–64 distinct output fields.')
        if not isinstance(rename, dict) or set(rename) - set(fields) or any(identifier(k) != k or identifier(v) != v for k, v in rename.items()):
            fail('Rename only selected fields.')
        if len(set(rename.get(f, f) for f in fields)) != len(fields):
            fail('Renaming cannot merge fields.')
        if not isinstance(where, dict) or len(where) > 8 or any(identifier(k) != k or not scalar(v) for k, v in where.items()):
            fail('Filters contain at most eight scalar equality conditions.')
    if op == 'join':
        for name in config.values():
            identifier(name)
    if op == 'linear_regression':
        features = config['features']
        if not isinstance(features, list) or not 1 <= len(features) <= 8 or any(identifier(f) != f for f in features) or len(set(features)) != len(features):
            fail('Choose 1–8 distinct numeric features.')
        identifier(config['target'])
        if config['target'] in features or type(config['seed']) is not int or not 0 <= config['seed'] <= 2**31 - 1 or type(config['epochs']) is not int or not 20 <= config['epochs'] <= 500:
            fail('Keep target separate from features; use a bounded seed and 20–500 epochs.')
        if type(config['ridge']) not in (int, float) or not 0 <= config['ridge'] <= 1 or not math.isfinite(config['ridge']):
            fail('Ridge regularization is between zero and one.')
    if op == 'predict':
        identifier(config['field'])
    if op == 'agent' and (type(config['agent_id']) is not int or config['agent_id'] < 1):
        fail('Agent identifiers are positive integers.')
    return output


def order(spec, revisions):
    """Resolve forward edges, reject cycles/dangling ports, and check every edge type."""
    config, external = spec['config'], spec['inputs']
    pending, completed, ordered = {}, {}, []
    for node in config['nodes']:
        if not isinstance(node, dict) or set(node) != {'name', 'revision_id', 'digest', 'inputs'}:
            fail('Graph nodes require name, revision_id, digest, and inputs.')
        name = identifier(node['name'])
        if name in pending or not isinstance(node['inputs'], dict) or not isinstance(node['revision_id'], str):
            fail('Graph nodes have unique names and a port/source map.')
        child = revisions.get(node['revision_id'])
        if not child or child.digest != node['digest'] or set(node['inputs']) != set(child.spec['inputs']):
            fail('Pin an existing revision and digest, with every declared input.')
        if any(not isinstance(value, str) for value in node['inputs'].values()):
            fail('Edges name a node or an external port prefixed with $.')
        pending[name] = node
    while pending:
        progressed = False
        for name, node in list(pending.items()):
            if any(not source.startswith('$') and source not in completed for source in node['inputs'].values()):
                continue
            child = revisions[node['revision_id']]
            for port, source in node['inputs'].items():
                actual = external.get(source[1:]) if source.startswith('$') else completed.get(source)
                if actual != child.spec['inputs'][port]:
                    fail('Every graph edge must have the destination port’s type.')
            completed[name] = revisions[node['revision_id']]._output_type
            ordered.append(node)
            del pending[name]
            progressed = True
        if not progressed:
            fail('The graph contains a cycle or missing node.')
    if not isinstance(config['output'], str) or config['output'] not in completed:
        fail('The output must name a graph node.')
    # Do not execute disconnected branches or conceal extra source accesses.
    required = {config['output']}
    for node in reversed(ordered):
        if node['name'] in required:
            required.update(s for s in node['inputs'].values() if not s.startswith('$'))
    if len(required) != len(ordered):
        fail('Every node must contribute to the selected output.')
    return ordered, completed[config['output']]


def fit(records, config):
    records = rows(records)
    features, target = config['features'], config['target']
    if len(records) < 10 or any(type(row.get(f)) not in (int, float) or not scalar(row[f]) for row in records for f in [*features, target]):
        fail('Training requires at least ten complete finite numeric records.')
    indices = list(range(len(records)))
    random.Random(config['seed']).shuffle(indices)
    ntest = max(2, len(indices) // 5)
    test, train = [records[i] for i in indices[:ntest]], [records[i] for i in indices[ntest:]]
    means = [sum(r[f] for r in train) / len(train) for f in features]
    scales = [math.sqrt(sum((r[f] - means[i])**2 for r in train) / len(train)) or 1.0 for i, f in enumerate(features)]
    center = sum(r[target] for r in train) / len(train)
    weights = [0.0] * len(features)
    vectors = [[(r[f] - means[i]) / scales[i] for i, f in enumerate(features)] for r in train]
    for _ in range(config['epochs']):
        errors = [center + sum(w*x for w, x in zip(weights, vector)) - r[target] for r, vector in zip(train, vectors)]
        # Step size bounds convergence even for highly correlated features.
        rate = 0.2 / len(features)
        center -= rate * sum(errors) / len(train)
        weights = [w - rate * (sum(e * vector[i] for e, vector in zip(errors, vectors)) / len(train) + config['ridge']*w) for i, w in enumerate(weights)]
    coefficients = [w/scales[i] for i, w in enumerate(weights)]
    intercept = center - sum(c*m for c, m in zip(coefficients, means))
    predictions = [intercept + sum(c*r[f] for c, f in zip(coefficients, features)) for r in test]
    mse = sum((p-r[target])**2 for p, r in zip(predictions, test))/len(test)
    avg = sum(r[target] for r in test)/len(test)
    variance = sum((r[target]-avg)**2 for r in test)/len(test)
    baseline = sum((r[target] - sum(t[target] for t in train)/len(train))**2 for r in test)/len(test)
    artifact = {'format': 'core-linear-model-v1', 'features': features, 'target': target,
        'coefficients': coefficients, 'intercept': intercept,
        'training_digest': checksum(records), 'config_digest': checksum(config),
        'metrics': {'mse': mse, 'mae': sum(abs(p-r[target]) for p, r in zip(predictions, test))/len(test),
            'r2': 1-mse/variance if variance else None, 'baseline_mse': baseline, 'training_rows': len(train), 'test_rows': len(test)},
        'evaluation': 'deterministic_80_20_holdout; fit_scaling_on_training_only', 'engine': 'core-local-ml-v1'}
    if any(not math.isfinite(value) for value in [*coefficients, intercept, mse, baseline]):
        fail('The fit did not produce finite results.')
    return artifact


def predict(records, artifact, field):
    records = rows(records)
    if not isinstance(artifact, dict) or artifact.get('format') != 'core-linear-model-v1':
        fail('Use a Core linear model artifact.')
    features, weights, intercept = artifact.get('features'), artifact.get('coefficients'), artifact.get('intercept')
    if not isinstance(features, list) or not 1 <= len(features) <= 8 or any(identifier(f) != f for f in features) or not isinstance(weights, list) or len(weights) != len(features) or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e18 for v in [*weights, intercept]):
        fail('Invalid numeric model artifact.')
    result = []
    for record in records:
        if field in record or any(type(record.get(f)) not in (int, float) or not scalar(record[f]) for f in features):
            fail('Predict on complete numeric features and a new output field.')
        result.append({**record, field: intercept + sum(w*record[f] for w, f in zip(weights, features))})
    return rows(result)


def operate(op, config, inputs):
    if op == 'linear_regression':
        return fit(inputs['rows'], config)
    if op == 'predict':
        return predict(inputs['rows'], inputs['model'], config['field'])
    if op == 'transform':
        return rows([{config['rename'].get(f, f): row[f] for f in config['fields'] if f in row} for row in rows(inputs['rows'])
            if all(key in row and type(row[key]) is type(value) and row[key] == value for key, value in config['where'].items())])
    if op == 'union':
        return rows(deepcopy(rows(inputs['left']) + rows(inputs['right'])))
    if op == 'join':
        left, right = rows(inputs['left']), rows(inputs['right'])
        result, index = [], {}
        for row in right:
            value = row.get(config['right_key'])
            if value is not None:
                index.setdefault((type(value).__name__, value), []).append(row)
        for row in left:
            value = row.get(config['left_key'])
            for match in index.get((type(value).__name__, value), []) if value is not None else []:
                extra = {config['right_prefix'] + '_' + key: value for key, value in match.items()}
                if set(extra) & set(row):
                    fail('Fusion field names collide; choose a different right prefix.')
                result.append({**row, **extra})
                if len(result) > MAX_ROWS:
                    fail('Fusion exceeds the 200-record bound; narrow the inputs.')
        return rows(result)
    fail('The operator needs an authorized resource adapter.')
