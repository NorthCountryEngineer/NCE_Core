"""Bounded nonsecret configuration contracts; submitted values never enter errors."""
import json
import re
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from jsonschema import Draft202012Validator


def validate_configuration_schema(schema):
    if not isinstance(schema, dict) or set(schema) - {'type', 'properties', 'required', 'additionalProperties'}:
        raise ValidationError('Use an object schema with properties, required, and additionalProperties=false.')
    if schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
        raise ValidationError('Configuration schemas must reject unknown fields.')
    properties = schema.get('properties', {})
    if not isinstance(properties, dict) or len(properties) > 30:
        raise ValidationError('Use at most 30 scalar configuration fields.')
    for name, field in properties.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,49}', name) or re.search(r'password|secret|token|credential|api_?key|authorization', name):
            raise ValidationError('Use nonsecret field names. Authentication belongs in the encrypted credential field.')
        if (not isinstance(field, dict) or set(field) - {'type', 'description', 'enum', 'minimum', 'maximum', 'maxLength', 'format'} or
                field.get('type') not in {'string', 'integer', 'boolean'} or field.get('format') not in {None, 'uri'}):
            raise ValidationError('Use scalar string, integer, or boolean fields; URI format is supported.')
        if field.get('format') == 'uri' and field.get('type') != 'string':
            raise ValidationError('URI configuration fields must be strings.')
    try:
        if len(json.dumps(schema, allow_nan=False)) > 16384:
            raise ValueError
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise ValidationError('Invalid bounded configuration schema.') from error


def validate_configuration(configuration, schema):
    validate_configuration_schema(schema)
    try:
        if not isinstance(configuration, dict) or len(json.dumps(configuration, allow_nan=False)) > 32768:
            raise ValueError
        Draft202012Validator(schema).validate(configuration)
        for name, value in configuration.items():
            if isinstance(value, str) and (len(value) > 2000 or any(ord(c) < 32 for c in value)):
                raise ValueError
            if schema.get('properties', {}).get(name, {}).get('format') == 'uri':
                parsed = urlparse(value)
                if (not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment or
                        parsed.scheme != 'https' and not (settings.DEBUG and parsed.scheme == 'http' and
                        parsed.hostname in {'localhost', '127.0.0.1', '::1'})):
                    raise ValueError
    except Exception as error:
        raise ValidationError('Configuration does not match its published nonsecret schema or trusted URL rules.') from error
