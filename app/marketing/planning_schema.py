"""A constrained provider wire format; domain documents keep their public shape."""
import json


def strict_schema(node):
    if isinstance(node, list):
        return [strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Decimal's string alternative also permits models to try prose. Use the
    # numeric alternative on the wire; Pydantic still enforces domain precision.
    alternatives = node.get("anyOf", [])
    number = next((x for x in alternatives if x.get("type") == "number"), None)
    if number is not None and any(x.get("type") == "string" for x in alternatives):
        return strict_schema(number)
    additional = node.get("additionalProperties")
    if node.get("type") == "object" and isinstance(additional, dict):
        # OpenAI's strict objects cannot have arbitrary keys. Preserve dynamic
        # element names as pairs, rather than replacing them with a fixed list.
        return {"type": "array", "maxItems": node.get("maxProperties", 20),
            "items": {"type": "object", "additionalProperties": False,
                "properties": {"key": {"type": "string", "minLength": 1},
                    "value": strict_schema(additional)}, "required": ["key", "value"]}}
    result = {key: strict_schema(value) for key, value in node.items()
        if key not in ("default", "maxProperties", "minProperties")}
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


def openai_format(system):
    prefix, separator, schema_text = system.rpartition("Schema:\n")
    if not separator:
        raise ValueError("planning_schema_missing")
    schema = strict_schema(json.loads(schema_text))
    instruction = ("元素elements使用key/value数组，key可自由命名；服务端会还原为元素对象。"
        "数字字段只返回数字，待确认状态放在说明中。Schema:\n")
    return prefix + instruction + json.dumps(schema, ensure_ascii=False), {
        "type": "json_schema", "json_schema": {"name": "marketai_planning", "strict": True, "schema": schema}}


def domain_document(data):
    """Restore element maps, refusing ambiguous duplicate names or malformed pairs."""
    if not isinstance(data, dict):
        return data
    combinations = data.get("combinations", [])
    if not isinstance(combinations, list):
        return data  # the domain validator reports the malformed field
    for combo in combinations:
        if not isinstance(combo, dict):
            continue
        elements = combo.get("elements")
        if isinstance(elements, list):
            if len(elements) > 20 or any(not isinstance(p, dict) or set(p) != {"key", "value"}
                or not isinstance(p["key"], str) or not p["key"].strip() for p in elements):
                raise ValueError("invalid_element_pairs")
            keys = [p["key"].strip() for p in elements]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate_element_names")
            combo["elements"] = dict(zip(keys, [p["value"] for p in elements]))
    return data
