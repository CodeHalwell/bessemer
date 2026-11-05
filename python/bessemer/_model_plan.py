"""Compile plain Pydantic models for the native body and response fast path.

A model qualifies when validating a JSON body reduces to exact type checks and
serializing an instance reduces to writing its fields: fields typed ``int``,
``float``, ``str``, ``bool``, ``list[...]``, ``... | None`` or another such
model, optionally with an immutable default, and no validators, constraints,
custom serializers, computed fields, private attributes or configuration that
changes either. Anything else returns ``None`` and keeps the Pydantic path.

The decision reads pydantic-core's schema for the model, accepting only the
keys listed here, so an unrecognised option (including one added by a future
Pydantic release) disables the fast path for that model rather than being
ignored. Even for a qualifying model the native code only builds instances
from values that already have their field's exact JSON type, and hands any
other body to Pydantic, so coercions and error reports are Pydantic's own.
"""

from __future__ import annotations

from typing import Any

_LEAVES = {"int", "float", "str", "bool"}
_NODE_KEYS = {"type", "metadata"}
_MODEL_KEYS = {
    "type",
    "cls",
    "config",
    "custom_init",
    "root_model",
    "metadata",
    "ref",
    "schema",
    "generic_origin",
}
_FIELDS_KEYS = {"type", "fields", "computed_fields", "model_name", "metadata"}
_FIELD_KEYS = {
    "type",
    "schema",
    "metadata",
    "validation_alias",
    "serialization_alias",
    "frozen",
}
_DEFAULT_KEYS = {"type", "schema", "default", "metadata"}
# Config entries that cannot change the result for exact JSON types. Other
# entries, such as str_strip_whitespace or strict, disable the fast path.
_CONFIG = {
    "title": None,
    "extra_fields_behavior": {"ignore", "forbid"},
    "revalidate_instances": {"never"},
    "validate_by_alias": {True},
    "validate_by_name": {False},
    "from_attributes": None,
    "hide_input_in_errors": None,
    "validation_error_cause": None,
    "loc_by_alias": None,
    "cache_strings": None,
    "serialize_by_alias": None,
    "ser_json_timedelta": None,
    "ser_json_temporal": None,
    "ser_json_bytes": None,
    "ser_json_inf_nan": None,
    "allow_inf_nan": None,
    "coerce_numbers_to_str": None,
}
_IMMUTABLE_DEFAULTS = (type(None), bool, int, float, str)


class _Unsupported(Exception):
    pass


def model_plan_spec(model: Any) -> tuple | None:
    """Describe ``model`` for ``bessemer._native.ModelPlan``, or return None."""
    try:
        from pydantic import BaseModel
    except ImportError:
        return None
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    try:
        schema = model.__pydantic_core_schema__
        if not isinstance(schema, dict):
            return None
        definitions: dict[str, dict] = {}
        if schema.get("type") == "definitions":
            for definition in schema["definitions"]:
                definitions[definition["ref"]] = definition
            schema = schema["schema"]
        spec = _node(schema, definitions, ())
    except (_Unsupported, KeyError, TypeError, AttributeError):
        return None
    except Exception:  # noqa: BLE001 -- Building a schema can run user code.
        return None
    if spec[0] != "model" or spec[1] is not model:
        return None
    return spec


def model_plan(model: Any) -> Any:
    """Return a compiled native plan for ``model``, or None."""
    spec = model_plan_spec(model)
    if spec is None:
        return None
    try:
        from ._native import ModelPlan
    except ImportError:
        return None
    return ModelPlan(spec)


def _only(schema: dict, allowed: set[str]) -> None:
    if not isinstance(schema, dict) or not set(schema) <= allowed:
        raise _Unsupported


def _node(schema: dict, definitions: dict[str, dict], active: tuple) -> tuple:
    kind = schema.get("type")
    if kind == "definition-ref":
        _only(schema, {"type", "schema_ref", "metadata"})
        return _node(definitions[schema["schema_ref"]], definitions, active)
    if kind in _LEAVES:
        _only(schema, _NODE_KEYS | {"ref"})
        return (kind,)
    if kind == "list":
        _only(schema, _NODE_KEYS | {"items_schema"})
        if "items_schema" not in schema:
            raise _Unsupported
        return ("list", _node(schema["items_schema"], definitions, active))
    if kind == "nullable":
        _only(schema, _NODE_KEYS | {"schema"})
        return ("nullable", _node(schema["schema"], definitions, active))
    if kind == "model":
        return _model(schema, definitions, active)
    raise _Unsupported


def _model(schema: dict, definitions: dict[str, dict], active: tuple) -> tuple:
    _only(schema, _MODEL_KEYS)
    cls = schema["cls"]
    if cls in active or schema.get("custom_init") or schema.get("root_model"):
        # Recursive models keep the Pydantic path.
        raise _Unsupported
    for key, value in schema.get("config", {}).items():
        if key not in _CONFIG:
            raise _Unsupported
        allowed = _CONFIG[key]
        if allowed is not None and value not in allowed:
            raise _Unsupported
    forbid = schema.get("config", {}).get("extra_fields_behavior") == "forbid"
    fields_schema = schema["schema"]
    _only(fields_schema, _FIELDS_KEYS)
    if fields_schema.get("type") != "model-fields" or fields_schema.get(
        "computed_fields"
    ):
        raise _Unsupported
    active = (*active, cls)
    fields = []
    for name, field in fields_schema["fields"].items():
        _only(field, _FIELD_KEYS)
        if field.get("type") != "model-field":
            raise _Unsupported
        key = field.get("validation_alias", name)
        output = field.get("serialization_alias", name)
        if not isinstance(key, str) or not isinstance(output, str):
            # AliasPath and AliasChoices keep the Pydantic path.
            raise _Unsupported
        inner = field["schema"]
        has_default = False
        default = None
        if inner.get("type") == "default":
            _only(inner, _DEFAULT_KEYS)
            if "default" not in inner:
                raise _Unsupported
            default = inner["default"]
            if type(default) not in _IMMUTABLE_DEFAULTS:
                raise _Unsupported
            has_default = True
            inner = inner["schema"]
        fields.append(
            (name, key, output, _node(inner, definitions, active), has_default, default)
        )
    keys = [field[1] for field in fields]
    outputs = [field[2] for field in fields]
    if len(set(keys)) != len(keys) or len(set(outputs)) != len(outputs):
        raise _Unsupported
    return ("model", cls, forbid, tuple(fields))
