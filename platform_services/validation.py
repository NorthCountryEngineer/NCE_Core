import re
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from jsonschema import Draft202012Validator, SchemaError

CAPABILITIES = {"appfactory.create", "appfactory.read", "appfactory.build", "appfactory.release", "data.read", "data.write", "agents.run", "workflows.run", "workflows.read",
                "chat.read", "chat.send", "chat.manage", "chat.ai", "chat.federate",
                'training.run', 'training.read', 'training.manage', 'training.evaluate',
                'fabric.read', 'fabric.write', 'fabric.manage', 'fabric.peer'}
CORE_PACKAGES = {"core-app-factory", "core-platform", "core-agents", "core-workflows", "core-data", "core-chat", 'core-training', 'core-fabric'}


def validate_schema(schema):
    if not isinstance(schema, dict):
        raise ValidationError("A schema must be a JSON object.")
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValidationError("Only local schema references are supported.")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValidationError("Invalid JSON Schema.") from error


def validate_endpoint(url):
    parsed = urlparse(url)
    if not parsed.netloc or parsed.username is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValidationError("Use an origin URL without a path, query, or credentials.")
    if parsed.scheme != "https" and not (settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValidationError("Use HTTPS (HTTP loopback is available only in development).")


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) - {"image", "port", "capabilities", "dependencies", "callback_path", "health_path", 'existing_workload'}:
        raise ValidationError("Unsupported manifest fields.")
    if 'existing_workload' in manifest and manifest['existing_workload'] is not True:
        raise ValidationError('Existing workloads must explicitly use adoption mode.')
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(c, str) or c not in CAPABILITIES for c in capabilities):
        raise ValidationError("Unknown core capability.")
    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(d, str) or d not in CORE_PACKAGES for d in dependencies):
        raise ValidationError("Applications can depend on the published core packages.")
    if "image" in manifest and (not isinstance(manifest["image"], str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", manifest["image"])):
        raise ValidationError("Container images must be pinned to a SHA-256 digest.")
    if type(manifest.get("port", 8080)) is not int or not 1024 <= manifest.get("port", 8080) <= 65535:
        raise ValidationError("Use a nonprivileged container port.")
    for field in ["callback_path", "health_path"]:
        path = manifest.get(field, "/oauth/callback" if field == "callback_path" else "/health")
        if not isinstance(path, str) or not re.fullmatch(r"/[a-zA-Z0-9/_-]*", path):
            raise ValidationError("Callbacks and health checks require a simple absolute path.")


def validate_transforms(transforms):
    if not isinstance(transforms, list) or len(transforms) > 20:
        raise ValidationError("Use a list of at most 20 transformations.")
    for step in transforms:
        if not isinstance(step, dict) or step.get("operation") not in {"select", "rename"}:
            raise ValidationError("Supported transformations are select and rename.")
        fields = step.get("fields")
        if step["operation"] == "select":
            if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
                raise ValidationError("select requires a list of field names.")
        elif not isinstance(fields, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in fields.items()):
            raise ValidationError("rename requires a mapping of field names.")
