from copy import deepcopy
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from .models import DataRecord
from .validation import validate_schema, validate_transforms


class IdempotencyConflict(Exception):
    pass


def transform(data, transforms):
    validate_transforms(transforms)
    value = deepcopy(data)
    if not isinstance(value, dict):
        raise ValidationError("Pipeline input must be a JSON object.")
    for step in transforms:
        if step["operation"] == "select":
            value = {key: value[key] for key in step["fields"] if key in value}
        else:
            value = {step["fields"].get(key, key): item for key, item in value.items()}
    return value


def publish(dataset, event_id, data, *, training_run_id=None):
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 160 or not isinstance(data, dict):
        raise ValidationError("An event ID of at most 160 characters and JSON object data are required.")
    if not dataset.active or not dataset.organization.active:
        raise ValidationError("Dataset is not active.")
    if training_run_id and dataset.data_class != 'synthetic':
        raise ValidationError('Training records can only be published into synthetic datasets.')
    validate_schema(dataset.schema)
    try:
        Draft202012Validator(dataset.schema).validate(data)
    except SchemaValidationError as error:
        raise ValidationError("Record does not match the dataset schema.") from error
    try:
        with transaction.atomic():
            record, created = DataRecord.objects.get_or_create(dataset=dataset, event_id=event_id, defaults={"data": data,
                'synthetic': dataset.data_class == 'synthetic', 'training_run_id': training_run_id})
    except IntegrityError:
        record, created = DataRecord.objects.get(dataset=dataset, event_id=event_id), False
    if record.data != data or record.training_run_id != training_run_id:
        raise IdempotencyConflict("This event ID has already been used for different data.")
    return record, created
