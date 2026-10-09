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
    if kind == 'chat_send':
        from core_chat.models import Conversation
        from uuid import UUID
        try:
            conversation_id = UUID(str(configuration.get('conversation_id')))
        except (ValueError, TypeError):
            raise ValidationError('The chat tool requires a conversation UUID.')
        if (set(configuration) != {'conversation_id', 'text_field'} or
                not isinstance(configuration.get('text_field'), str) or not 1 <= len(configuration['text_field']) <= 160 or
                not Conversation.objects.filter(pk=conversation_id, organization_id=organization_id, active=True, ai_enabled=True).exists()):
            raise ValidationError('Choose an active, AI-enabled conversation in this organization and a text field.')
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
            if tool.get("tool") not in {"inspect", "select", "publish", "chat_send"}:
                raise ValidationError("Rule agents can select only inspect, select, publish, or chat_send.")
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
