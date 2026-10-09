from django.core.exceptions import ValidationError


def validate_configuration(organization_id, kind, configuration):
    from .models import Dataset
    if not isinstance(configuration, dict):
        raise ValidationError("Agent configuration must be an object.")
    if kind == "select" and (not isinstance(configuration.get("fields"), list) or
        any(not isinstance(f, str) for f in configuration["fields"])):
        raise ValidationError("The select tool requires a list of fields.")
    if kind == "publish" and not Dataset.objects.filter(organization_id=organization_id,
        slug=configuration.get("dataset"), active=True).exists():
        raise ValidationError("The publish tool requires an active dataset in this organization.")
    if kind == "rules":
        rules = configuration.get("rules")
        default = configuration.get("default")
        if not isinstance(rules, list) or len(rules) > 10 or not isinstance(default, dict):
            raise ValidationError("A rule agent requires at most ten rules and a default tool.")
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("field"), str) or "equals" not in rule:
                raise ValidationError("Each rule requires field, equals, tool, and configuration.")
            if isinstance(rule["equals"], (dict, list)):
                raise ValidationError("Rule comparisons use a scalar JSON value.")
        for tool in [*rules, default]:
            if tool.get("tool") not in {"inspect", "select", "publish"}:
                raise ValidationError("Rule agents can select only inspect, select, or publish.")
            validate_configuration(organization_id, tool["tool"], tool.get("configuration", {}))


def possible_tools(step):
    if step["kind"] != "rules":
        return {step["kind"]}
    config = step["configuration"]
    return {tool["tool"] for tool in [*config["rules"], config["default"]]}


def choose_tool(step, data):
    if step["kind"] != "rules":
        return step
    config = step["configuration"]
    selected = next((rule for rule in config["rules"] if rule["field"] in data and
        type(data[rule["field"]]) is type(rule["equals"]) and data[rule["field"]] == rule["equals"]), config["default"])
    return {"kind": selected["tool"], "configuration": selected.get("configuration", {})}
