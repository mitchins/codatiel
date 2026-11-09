"""Fail-closed validator for the JSON-Schema subset used by result v1.

Generic subset implementation (not benchmark-aware): local `$ref` only,
`anyOf`, `type`, `enum`, `properties`/`required`/`additionalProperties`,
`items`/`minItems`, `minLength`/`pattern`, `minimum`. Unknown keywords and
external references are errors. Stdlib only.
"""
from __future__ import annotations

import math
import re
from typing import Any


def validate(value: Any, schema: dict, root: dict | None = None, path: str = "$") -> list[str]:
    if root is None:
        root = schema
    supported = {
        "$schema", "$id", "title", "$defs", "$ref", "type", "anyOf",
        "enum", "properties", "required", "additionalProperties",
        "items", "minItems", "minLength", "minimum", "pattern",
    }
    unknown = set(schema) - supported
    if unknown:
        return [f"{path}: unsupported schema keywords {sorted(unknown)}"]
    if "$ref" in schema:
        pointer = schema["$ref"]
        if not pointer.startswith("#/"):
            return [f"{path}: external schema references forbidden"]
        target = root
        for part in pointer[2:].split("/"):
            target = target[part]
        return validate(value, target, root, path)
    if "anyOf" in schema:
        choices = [validate(value, sub, root, path) for sub in schema["anyOf"]]
        if any(not errors for errors in choices):
            return []
        return [f"{path}: no allowed type/shape matched"]
    type_checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: type(v) is int,
        "number": lambda v: type(v) in (int, float) and math.isfinite(v),
        "null": lambda v: v is None,
        "boolean": lambda v: type(v) is bool,
    }
    if "type" in schema and not type_checks[schema["type"]](value):
        return [f"{path}: expected {schema['type']}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value outside enum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path}.{key}: unexpected property")
        for key in set(value) & set(properties):
            errors.extend(validate(value[key], properties[key], root, f"{path}.{key}"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], root, f"{path}[{i}]"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: pattern mismatch")
    if type(value) in (int, float) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: below minimum")
    return errors
