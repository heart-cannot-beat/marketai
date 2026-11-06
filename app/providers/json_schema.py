"""Validate a model's JSON output against the schema the caller asked for.

``llm_chat`` promises its callers *schema-valid* JSON, and a provider's
structured-output mode is a request, not a guarantee (a truncated answer, a
refusal, an OpenAI-compatible gateway that ignores ``response_format``). So
the output is validated here, locally, before it is ever returned.

This is the subset of JSON Schema (draft 2020-12) that OpenAI strict
structured outputs accept -- ``type`` (including type lists), ``properties``,
``required``, ``additionalProperties``, ``items``, ``enum``, ``const``,
``anyOf``, local ``$ref`` into ``$defs``/``definitions``, and the numeric,
string-length and array-length bounds -- implemented without a third-party
dependency. Unknown keywords are ignored, as JSON Schema itself does.
"""
from __future__ import annotations

from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}

#: Errors beyond this many are dropped; the first few explain the failure.
_MAX_ERRORS = 20


class SchemaDefinitionError(ValueError):
    """The schema itself is unusable (bad ``$ref``, unknown type)."""


def check_schema(schema: Any) -> None:
    """Reject a schema this validator cannot apply, before any money is spent."""
    if not isinstance(schema, dict):
        raise SchemaDefinitionError("a JSON schema must be an object")
    if schema.get("type") != "object":
        raise SchemaDefinitionError("the top-level JSON schema must have type 'object'")
    _walk_types(schema, schema)


def validate(instance: Any, schema: dict) -> list[str]:
    """Every way ``instance`` violates ``schema`` (empty when it conforms)."""
    errors: list[str] = []
    _validate(instance, schema, schema, "$", errors)
    return errors[:_MAX_ERRORS]


#: Keywords whose value maps *names* to sub-schemas: the map itself is not a
#: schema (a property may well be called ``type`` or ``$ref``).
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
#: Keywords whose value is instance data, not a schema.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})


def _walk_types(node: Any, root: dict) -> None:
    if isinstance(node, dict):
        declared = node.get("type")
        for name in declared if isinstance(declared, list) else [declared]:
            if name is not None and (not isinstance(name, str) or name not in _TYPE_CHECKS):
                raise SchemaDefinitionError(f"unsupported JSON schema type {name!r}")
        if "$ref" in node:
            _resolve_ref(node["$ref"], root)
        for key, value in node.items():
            if key in _DATA_KEYWORDS:
                continue
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                for sub_schema in value.values():
                    _walk_types(sub_schema, root)
            else:
                _walk_types(value, root)
    elif isinstance(node, list):
        for value in node:
            _walk_types(value, root)


def _resolve_ref(ref: Any, root: dict) -> dict:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SchemaDefinitionError(f"only local $ref values are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaDefinitionError(f"unresolvable $ref {ref!r}")
        node = node[part]
    if not isinstance(node, dict):
        raise SchemaDefinitionError(f"$ref {ref!r} does not point at a schema")
    return node


def _validate(instance: Any, schema: Any, root: dict, path: str, errors: list[str]) -> None:
    if len(errors) >= _MAX_ERRORS or schema is True or not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _validate(instance, _resolve_ref(schema["$ref"], root), root, path, errors)
        return

    if "anyOf" in schema:
        branches = schema["anyOf"] or []
        if not any(_conforms(instance, branch, root) for branch in branches):
            errors.append(f"{path}: matches none of the anyOf alternatives")
            return

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_type_name(instance)}")
            return

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, dict):
        _validate_object(instance, schema, root, path, errors)
    elif isinstance(instance, list):
        _validate_array(instance, schema, root, path, errors)
    elif isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    elif _TYPE_CHECKS["number"](instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: not below {schema['exclusiveMaximum']}")


def _validate_object(
    instance: dict, schema: dict, root: dict, path: str, errors: list[str],
) -> None:
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in instance:
            errors.append(f"{path}: missing required property {name!r}")
    additional = schema.get("additionalProperties", True)
    for name, value in instance.items():
        child = f"{path}.{name}"
        if name in properties:
            _validate(value, properties[name], root, child, errors)
        elif additional is False:
            errors.append(f"{path}: unexpected property {name!r}")
        elif isinstance(additional, dict):
            _validate(value, additional, root, child, errors)


def _validate_array(
    instance: list, schema: dict, root: dict, path: str, errors: list[str],
) -> None:
    if "minItems" in schema and len(instance) < schema["minItems"]:
        errors.append(f"{path}: fewer than {schema['minItems']} items")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        errors.append(f"{path}: more than {schema['maxItems']} items")
    items = schema.get("items")
    if isinstance(items, dict):
        for index, value in enumerate(instance):
            _validate(value, items, root, f"{path}[{index}]", errors)


def _conforms(instance: Any, schema: Any, root: dict) -> bool:
    probe: list[str] = []
    _validate(instance, schema, root, "$", probe)
    return not probe


def _type_name(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](value):
            return name
    return type(value).__name__
