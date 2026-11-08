"""Immutable callable plans built before the first request is accepted."""

from __future__ import annotations

import enum
import functools
import inspect
import json
import math
import re
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from .params import UNSET, Body, Depends, File, Header, Parameter, Path, Query

_MISSING = inspect.Parameter.empty
_SCALARS = {str: "str", int: "int", float: "float", bool: "bool"}
_PATH_PARAMETER = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(?::[^{}/]*)?\}")


@dataclass(frozen=True)
class Binding:
    name: str
    kind: str
    key: str | None = None
    dependency: int | None = None
    use_cache: bool = True
    scope: str = "request"
    # The accumulated Security() scopes, for a ``security_scopes`` binding.
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CallablePlan:
    id: int
    call: Callable[..., Any]
    kind: str
    bindings: tuple[Binding, ...]
    scope: str = "request"


@dataclass(frozen=True)
class CompiledInputs:
    plan: CallablePlan
    dependencies: tuple[CallablePlan, ...]
    before: tuple[Binding, ...]
    parameters: tuple[dict[str, Any], ...]
    body_models: dict[str, Any]
    body_containers: dict[str, type]
    nullable_body: frozenset[str]
    input_validators: dict[str, Callable[[Any], Any]]
    needs_request: bool
    needs_principal: bool = False
    security: tuple[Any, ...] = ()
    # Several or embedded body parameters, read as one generated model:
    # (argument key, model, field aliases, ((attribute, key), ...)).
    embedded_body: tuple | None = None
    openapi_parameters: tuple[dict[str, Any], ...] = ()


# The argument carrying the JSON object that embedded body parameters share.
EMBEDDED_BODY_KEY = "__bessemer_body__"


@dataclass(frozen=True)
class _BodyInput:
    """A JSON body input, compiled once every body input is known."""

    arguments: tuple[Any, ...]
    name: str
    key: str
    annotation: Any
    marker: Parameter | None
    default: Any
    owner: int
    alias: str
    position: int


def _merged(scopes: tuple[str, ...], more: tuple[str, ...]) -> tuple[str, ...]:
    """``scopes`` followed by those of ``more`` it lacks, in order."""
    return scopes + tuple(dict.fromkeys(s for s in more if s not in scopes))


def optional(annotation: Any) -> tuple[Any, bool]:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        arguments = typing.get_args(annotation)
        non_none = [argument for argument in arguments if argument is not type(None)]
        if len(non_none) == 1 and len(arguments) == 2:
            return non_none[0], True
    return annotation, False


def _is_field_info(item: Any) -> bool:
    return type(item).__name__ == "FieldInfo" and getattr(
        type(item), "__module__", ""
    ).startswith("pydantic")


def _field_to_parameter(
    field_info: Any, name: str, annotation: Any, path_names: set[str]
) -> Parameter:
    for attr in ("validate_default", "frozen", "exclude"):
        if getattr(field_info, attr, None) is not None:
            raise ValueError(f"unsupported Field option: {attr}")
    if field_info.alias_priority is not None and (
        field_info.alias_priority != 2
        or (
            field_info.alias is None
            and field_info.validation_alias is None
            and field_info.serialization_alias is None
        )
    ):
        raise ValueError("unsupported Field option: alias_priority")
    kwargs: dict[str, Any] = {}
    if (
        type(field_info.default).__name__ != "PydanticUndefinedType"
        and field_info.default is not Ellipsis
    ):
        kwargs["default"] = field_info.default
    if field_info.default_factory is not None:
        kwargs["default_factory"] = field_info.default_factory
    for attr in (
        "alias",
        "validation_alias",
        "serialization_alias",
        "title",
        "description",
        "examples",
        "json_schema_extra",
        "discriminator",
    ):
        value = getattr(field_info, attr, None)
        if value is not None:
            kwargs[attr] = value
    if field_info.deprecated:
        kwargs["deprecated"] = bool(field_info.deprecated)
    for m in getattr(field_info, "metadata", ()):
        m_type = type(m).__name__
        if m_type == "Gt":
            kwargs["gt"] = m.gt
        elif m_type == "Ge":
            kwargs["ge"] = m.ge
        elif m_type == "Lt":
            kwargs["lt"] = m.lt
        elif m_type == "Le":
            kwargs["le"] = m.le
        elif m_type == "MultipleOf":
            kwargs["multiple_of"] = m.multiple_of
        elif m_type == "MinLen":
            kwargs["min_length"] = m.min_length
        elif m_type == "MaxLen":
            kwargs["max_length"] = m.max_length
        elif m_type == "Strict":
            kwargs["strict"] = m.strict
        elif m_type == "_PydanticGeneralMetadata":
            for k, v in getattr(m, "__dict__", {}).items():
                if v is None:
                    continue
                if k == "pattern":
                    kwargs["pattern"] = v
                elif k == "strict":
                    kwargs["strict"] = v
                else:
                    raise ValueError(f"unsupported Field option: {k}")
        else:
            raise ValueError(f"unsupported Field metadata: {m!r}")
    effective_alias = (
        kwargs.get("validation_alias")
        if isinstance(kwargs.get("validation_alias"), str)
        else kwargs.get("alias") or name
    )
    base = optional(annotation)[0]
    if name in path_names or effective_alias in path_names:
        return Path(**kwargs)
    if _scalar(base)[0] is not None:
        return Query(**kwargs)
    return Body(**kwargs)


def _is_model_type(tp: Any) -> bool:
    return isinstance(tp, type) and callable(getattr(tp, "model_validate", None))


def _is_model_union(annotation: Any) -> bool:
    if typing.get_origin(annotation) not in (typing.Union, types.UnionType):
        return False
    args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
    return bool(args) and all(
        _is_model_type(
            typing.get_args(arg)[0]
            if typing.get_origin(arg) is typing.Annotated
            else arg
        )
        for arg in args
    )


def _is_sequence_annotation(annotation: Any) -> bool:
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    annotation, _ = optional(annotation)
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation) or annotation
    return origin in (list, set, tuple, frozenset, Sequence)


class _ModelAdapter:
    """Adapter wrapping a Pydantic model or discriminated union for body validation."""

    def __init__(
        self,
        annotation: Any,
        *,
        discriminator: str | None = None,
        strict: bool | None = None,
    ) -> None:
        from pydantic import Field, TypeAdapter

        field_kwargs: dict[str, Any] = {}
        if discriminator is not None:
            field_kwargs["discriminator"] = discriminator
        if strict is not None:
            field_kwargs["strict"] = strict
        target = (
            typing.Annotated[annotation, Field(**field_kwargs)]
            if field_kwargs
            else annotation
        )
        self._adapter = TypeAdapter(target)

    def model_validate(self, value: Any) -> Any:
        return self._adapter.validate_python(value)

    def json_schema(self) -> dict[str, Any]:
        return self._adapter.json_schema(ref_template="#/components/schemas/{model}")


def _annotated(annotation: Any) -> tuple[Any, Any]:
    metadata = None
    if typing.get_origin(annotation) is typing.Annotated:
        annotation, *extras = typing.get_args(annotation)
        markers = [
            item
            for item in extras
            if isinstance(item, (Parameter, Depends)) or _is_field_info(item)
        ]
        if len(markers) > 1:
            raise ValueError(
                "an input may declare only one parameter or dependency marker"
            )
        metadata = markers[0] if markers else None
        if any(
            not isinstance(item, (Parameter, Depends)) and not _is_field_info(item)
            for item in extras
        ):
            raise ValueError(
                "unsupported Annotated metadata; use Bessemer parameter markers"
            )
    return annotation, metadata


def _callable_kind(call: Callable[..., Any]) -> str:
    # Callable instances require inspecting __call__ rather than their class.
    target = call
    if (
        not inspect.isfunction(call)
        and not inspect.ismethod(call)
        and not inspect.isclass(call)
        and not isinstance(call, functools.partial)
    ):
        target = call.__call__
    if inspect.isasyncgenfunction(target):
        return "async_generator"
    if inspect.isgeneratorfunction(target):
        return "generator"
    return "async" if inspect.iscoroutinefunction(target) else "sync"


def _callable_hints(call: Callable[..., Any]) -> dict[str, Any]:
    target = call
    while isinstance(target, functools.partial):
        target = target.func
    if inspect.isclass(target):
        target = target.__init__
    elif not inspect.isfunction(target) and not inspect.ismethod(target):
        target = target.__call__
    return typing.get_type_hints(target, include_extras=True)


def _scalar(
    annotation: Any,
) -> tuple[str | None, Callable[[Any], Any] | None, dict[str, Any]]:
    try:
        primitive = _SCALARS.get(annotation)
    except TypeError:
        primitive = None
    if primitive is not None:
        return (
            primitive,
            None,
            {
                "type": {
                    "str": "string",
                    "int": "integer",
                    "float": "number",
                    "bool": "boolean",
                }[primitive]
            },
        )
    if annotation in (UUID, datetime, date):
        constructor = UUID if annotation is UUID else annotation.fromisoformat
        format_name = (
            "uuid"
            if annotation is UUID
            else "date-time"
            if annotation is datetime
            else "date"
        )
        return (
            "str",
            lambda value: (
                value if isinstance(value, annotation) else constructor(value)
            ),
            {"type": "string", "format": format_name},
        )
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        members = list(annotation)
        if not members:
            raise ValueError("Enum inputs must have at least one value")
        value_type = type(members[0].value)
        if value_type not in _SCALARS or any(
            type(item.value) is not value_type for item in members
        ):
            raise ValueError("Enum input values must have one supported scalar type")
        primitive, _, schema = _scalar(value_type)
        return (
            primitive,
            annotation,
            dict(schema, enum=[item.value for item in members]),
        )
    if typing.get_origin(annotation) is typing.Literal:
        values = typing.get_args(annotation)
        if (
            not values
            or type(values[0]) not in _SCALARS
            or any(type(value) is not type(values[0]) for value in values)
        ):
            raise ValueError(
                "Literal inputs must have values of one supported scalar type"
            )
        primitive, _, schema = _scalar(type(values[0]))

        def validate_literal(value: Any) -> Any:
            if value not in values or type(value) is not type(values[0]):
                raise ValueError("Input should be one of the declared literal values")
            return value

        return primitive, validate_literal, dict(schema, enum=list(values))
    return None, None, {}


def _json_value(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    elif isinstance(value, UUID):
        value = str(value)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "defaults and schema metadata must be JSON-compatible"
        ) from error


def _constraints(
    marker: Parameter | None, primitive: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], Callable[[Any], Any] | None]:
    if marker is None:
        return {}, None
    constraints = {}
    for name in ("ge", "gt", "le", "lt"):
        value = getattr(marker, name)
        if value is not None:
            if (
                primitive not in {"int", "float"}
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} constraints require finite numeric inputs")
            constraints[name] = value
            schema[
                {
                    "ge": "minimum",
                    "gt": "exclusiveMinimum",
                    "le": "maximum",
                    "lt": "exclusiveMaximum",
                }[name]
            ] = value
    multiple_of_validator = None
    if marker.multiple_of is not None:
        value = marker.multiple_of
        if (
            primitive not in {"int", "float"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                "multiple_of constraints require positive finite numeric inputs"
            )
        constraints["multiple_of"] = value
        schema["multipleOf"] = value
        try:
            from pydantic_core import SchemaValidator

            multiple_of_validator = SchemaValidator(
                {
                    "type": "float" if primitive == "float" else "int",
                    "multiple_of": value,
                }
            )
        except ImportError:
            pass
    for name in ("min_length", "max_length"):
        value = getattr(marker, name)
        if value is not None:
            if primitive not in {"str", "json"} or type(value) is not int or value < 0:
                raise ValueError(
                    f"{name} constraints require nonnegative lengths on strings or JSON bodies"
                )
            constraints[name] = value
            if primitive == "str":
                schema["minLength" if name == "min_length" else "maxLength"] = value
            elif schema.get("type") == "array":
                schema["minItems" if name == "min_length" else "maxItems"] = value
            elif schema.get("type") == "object":
                schema["minProperties" if name == "min_length" else "maxProperties"] = (
                    value
                )
    lower = [
        (constraints[name], name == "gt")
        for name in ("ge", "gt")
        if name in constraints
    ]
    upper = [
        (constraints[name], name == "lt")
        for name in ("le", "lt")
        if name in constraints
    ]
    if lower and upper:
        minimum = max(value for value, _ in lower)
        maximum = min(value for value, _ in upper)
        exclusive = any(strict and value == minimum for value, strict in lower) or any(
            strict and value == maximum for value, strict in upper
        )
        if minimum > maximum or minimum == maximum and exclusive:
            raise ValueError("numeric constraints describe an empty range")
    if (
        "min_length" in constraints
        and "max_length" in constraints
        and constraints["min_length"] > constraints["max_length"]
    ):
        raise ValueError("minimum length cannot exceed maximum length")
    pattern = None
    if marker.pattern is not None:
        if primitive != "str" or not isinstance(marker.pattern, str):
            raise ValueError("pattern requires a string input and regular expression")
        try:
            from pydantic_core import SchemaValidator
        except ImportError as error:
            raise ValueError(
                "pattern constraints require the optional Pydantic v2 dependency"
            ) from error
        try:
            pattern = SchemaValidator({"type": "str", "pattern": marker.pattern})
        except Exception as error:
            raise ValueError(
                "invalid or unsupported input regular expression"
            ) from error
        schema["pattern"] = marker.pattern
        # pydantic-core's Rust regex engine avoids backtracking expressions
        # allowing untrusted request input to block the Python event loop.
    if not constraints and pattern is None:
        return {}, None

    def validate(value: Any) -> Any:
        if value is None:
            return value
        for name, limit in constraints.items():
            if name == "ge" and value < limit or name == "gt" and value <= limit:
                raise ValueError("Input is below its allowed minimum")
            if name == "le" and value > limit or name == "lt" and value >= limit:
                raise ValueError("Input exceeds its allowed maximum")
            if name == "multiple_of":
                if multiple_of_validator is not None:
                    multiple_of_validator.validate_python(value)
                elif value % limit != 0:
                    raise ValueError(f"Input should be a multiple of {limit}")
            if name == "min_length" and len(value) < limit:
                raise ValueError("Input is shorter than its minimum length")
            if name == "max_length" and len(value) > limit:
                raise ValueError("Input exceeds its maximum length")
        if pattern is not None:
            return pattern.validate_python(value)
        return value

    return constraints, validate


# Sources whose scalar values the native invoker passes straight to handlers.
_NATIVE_SCALAR_SOURCES = frozenset({"path", "query", "header", "cookie"})


def _merge_validators(
    first: Callable | None, second: Callable | None, nullable: bool
) -> Callable | None:
    if first is None and second is None:
        return None

    def validate(value: Any) -> Any:
        if nullable and value is None:
            return None
        if first is not None:
            value = first(value)
        return second(value) if second is not None else value

    return validate


class _Compiler:
    def __init__(
        self,
        path: str,
        *,
        websocket: bool = False,
        overrides: Mapping[Callable, Callable] | None = None,
    ) -> None:
        self.path = path
        self.websocket = websocket
        self.overrides = overrides or {}
        self.path_names = set(_PATH_PARAMETER.findall(path))
        self.found_path_names: set[str] = set()
        # Path names an overridden dependency consumed, which its fake need not.
        self.overridden_path_names: set[str] = set()
        self.dependencies: list[CallablePlan] = []
        self.dependency_ids: dict[tuple[int, str, tuple[str, ...]], int] = {}
        self.active: set[int] = set()
        self.parameters: list[dict[str, Any]] = []
        self.openapi_parameters: list[dict[str, Any]] = []
        self.body_models: dict[str, Any] = {}
        self.body_containers: dict[str, type] = {}
        self.nullable_body: set[str] = set()
        self.input_validators: dict[str, Callable] = {}
        self.body_owner: tuple[int, str] | None = None
        self.body_inputs: list[_BodyInput] = []
        self.body_compiled = False
        self.form_used = False
        self.needs_request = False
        self.needs_principal = False
        # Security scheme dependencies, as declared (before any override), so
        # the OpenAPI document keeps describing them while tests replace them,
        # each with the Security() scopes required where it was reached.
        self.security: list[tuple[Any, tuple[str, ...]]] = []
        # The Security() scopes of the dependency being compiled.
        self.scopes: tuple[str, ...] = ()

    def _require(self, scheme: Any, scopes: tuple[str, ...]) -> None:
        for index, (seen, required) in enumerate(self.security):
            if seen is scheme:
                self.security[index] = (seen, _merged(required, scopes))
                return
        self.security.append((scheme, scopes))

    def _hints(self, call: Callable) -> dict[str, Any]:
        try:
            return _callable_hints(call)
        except (NameError, TypeError) as error:
            raise ValueError(
                f"cannot resolve type annotations for {self.path}: {error}"
            ) from error

    def _dependency(self, dependency: Depends, annotation: Any = _MISSING) -> int:
        call = dependency.dependency or annotation
        if call is _MISSING or not callable(call):
            raise ValueError(
                f"{self.path}: Depends requires an explicit callable dependency"
            )
        from .params import Security
        from .security import SecurityBase

        # Application dependencies are shared by every route, so no route's
        # scopes reach them.
        scopes = (
            ()
            if dependency.scope == "app"
            else _merged(self.scopes, dependency.scopes)
            if isinstance(dependency, Security)
            else self.scopes
        )
        if isinstance(call, SecurityBase):
            self._require(call, scopes)
        # An override replaces the dependency and contributes its own inputs.
        if self.overrides:
            original = call
            try:
                call = self.overrides.get(call, call)
            except TypeError:
                pass  # An unhashable callable cannot be an override key.
            if call is not original:
                self._excuse_path_names(original, dependency.scope, scopes)
        if not callable(call):
            raise TypeError(f"{self.path}: dependency overrides must be callable")
        # One call under different scopes is a different dependency, as its
        # SecurityScopes differ; each is cached on its own (as in FastAPI).
        identity = (id(call), dependency.scope, scopes)
        if id(call) in self.active:
            raise ValueError(f"{self.path}: dependency cycle detected")
        if identity in self.dependency_ids:
            return self.dependency_ids[identity]
        outer, self.scopes = self.scopes, scopes
        try:
            plan = self.callable(call, dependency.scope)
        finally:
            self.scopes = outer
        self.dependencies.append(plan)
        self.dependency_ids[identity] = plan.id
        return plan.id

    def _excuse_path_names(
        self, original: Callable, scope: str, scopes: tuple[str, ...]
    ) -> None:
        shadow = _Compiler(self.path, websocket=self.websocket)
        shadow.scopes = scopes
        try:
            shadow.callable(original, scope)
        except Exception:  # noqa: BLE001 -- The original need not compile.
            return
        self.overridden_path_names |= shadow.found_path_names
        # Schemes the original reached stay documented while it is replaced.
        for scheme, required in shadow.security:
            self._require(scheme, required)

    def _query_model(
        self, name: str, annotation: Any, marker: Query, default: Any
    ) -> int:
        from .request import Request

        model, nullable = optional(annotation)
        if (
            default is not _MISSING
            or nullable
            or marker.default not in (UNSET, Ellipsis)
            or marker.default_factory is not None
        ):
            raise ValueError(
                f"{self.path}: declare defaults on the query model's fields, not on {name!r}"
            )
        disallowed = [
            opt
            for opt in (
                "alias",
                "validation_alias",
                "serialization_alias",
                "description",
                "ge",
                "gt",
                "le",
                "lt",
                "multiple_of",
                "min_length",
                "max_length",
                "pattern",
                "strict",
                "discriminator",
                "title",
                "examples",
                "openapi_examples",
                "json_schema_extra",
            )
            if getattr(marker, opt) is not None
        ]
        if disallowed or marker.deprecated:
            raise ValueError(
                f"{self.path}: Query() on a Pydantic model only supports include_in_schema; "
                "declare constraints and metadata on the model's fields"
            )
        field_specs: list[tuple[str, bool]] = []
        for field_name, field_info in model.model_fields.items():
            query_key = (
                field_info.validation_alias
                if isinstance(field_info.validation_alias, str)
                else field_info.alias
                if field_info.alias is not None
                else field_name
            )
            if query_key in self.path_names:
                raise ValueError(
                    f"{self.path}: query model field {query_key!r} conflicts with a path parameter"
                )
            field_specs.append(
                (query_key, _is_sequence_annotation(field_info.annotation))
            )
        model_schema = model.model_json_schema(
            ref_template="#/components/schemas/{model}", mode="validation"
        )
        defs = model_schema.get("$defs")
        required_fields = set(model_schema.get("required", ()))
        for prop_name, prop_schema in model_schema.get("properties", {}).items():
            schema_copy = dict(prop_schema)
            if defs:
                schema_copy["$defs"] = defs
            openapi_meta: dict[str, Any] = {}
            if not marker.include_in_schema:
                openapi_meta["include_in_schema"] = False
            if "default" not in prop_schema:
                openapi_meta["default_factory"] = True
            param_entry: dict[str, Any] = {
                "name": prop_name,
                "alias": prop_name,
                "source": "query",
                "required": prop_name in required_fields,
                "default": prop_schema.get("default"),
                "schema": schema_copy,
            }
            if "description" in prop_schema:
                param_entry["description"] = prop_schema["description"]
            if prop_schema.get("deprecated"):
                param_entry["deprecated"] = True
            if openapi_meta:
                param_entry["openapi"] = openapi_meta
            self.openapi_parameters.append(param_entry)
        self.needs_request = True
        identity = (id(model), "query_model", ())
        if identity in self.dependency_ids:
            return self.dependency_ids[identity]
        specs = tuple(field_specs)
        known_keys = frozenset(k for k, _ in specs)

        async def extract(request: Request) -> Any:
            qparams = request.query_params
            data: dict[str, Any] = {}
            for key, is_seq in specs:
                if key in qparams:
                    data[key] = qparams.getlist(key) if is_seq else qparams[key]
            for key in qparams:
                if key not in known_keys:
                    data[key] = qparams[key]
            try:
                return model.model_validate(data)
            except ValueError as error:
                from .request import RequestValidationError
                from .worker import Worker

                raise RequestValidationError(
                    Worker._details(error, ["query"], "Invalid query parameters")
                ) from error

        plan_id = len(self.dependencies)
        plan = CallablePlan(
            plan_id,
            extract,
            "async",
            (Binding("request", "request"),),
            "request",
        )
        self.dependencies.append(plan)
        self.dependency_ids[identity] = plan_id
        return plan_id

    def callable(
        self, call: Callable, scope: str = "request", *, handler: bool = False
    ) -> CallablePlan:
        from .auth import Principal
        from .request import BackgroundTasks, Request, State
        from .security import SecurityScopes
        from .websockets import WebSocket

        self.active.add(id(call))
        try:
            signature = inspect.signature(call)
            hints = self._hints(call)
            prepared = []
            for parameter in signature.parameters.values():
                if parameter.name in (
                    "__bessemer_request__",
                    EMBEDDED_BODY_KEY,
                ) or parameter.name.startswith("__dependency_"):
                    raise ValueError(
                        f"{self.path}: parameter name {parameter.name!r} uses a reserved framework namespace"
                    )
                if parameter.kind not in (
                    parameter.POSITIONAL_OR_KEYWORD,
                    parameter.KEYWORD_ONLY,
                ):
                    raise ValueError(
                        f"{self.path}: positional-only and variadic parameters are unsupported"
                    )
                annotation, marker = _annotated(hints.get(parameter.name, _MISSING))
                default = parameter.default
                if isinstance(default, (Parameter, Depends)) or _is_field_info(default):
                    if marker is not None:
                        raise ValueError(
                            f"{self.path}: declare input metadata only once"
                        )
                    marker = default
                    default = _MISSING
                if _is_field_info(marker):
                    marker = _field_to_parameter(
                        marker, parameter.name, annotation, self.path_names
                    )
                if isinstance(marker, Depends):
                    if default is not _MISSING:
                        raise ValueError(
                            f"{self.path}: a dependency cannot also declare a default"
                        )
                    if scope == "app" and marker.scope != "app":
                        raise ValueError(
                            f"{self.path}: an application dependency cannot depend on a request-scoped dependency"
                        )
                    dependency_id = self._dependency(marker, annotation)
                    prepared.append(
                        (parameter, annotation, marker, default, dependency_id)
                    )
                elif isinstance(marker, Query) and _is_model_type(
                    optional(annotation)[0]
                ):
                    if scope == "app":
                        raise ValueError(
                            f"{self.path}: application dependencies cannot extract request inputs"
                        )
                    if self.websocket:
                        raise ValueError(
                            f"{self.path}: WebSocket handlers read path, query, header and cookie "
                            f"values from the WebSocket object; {parameter.name!r} is not injectable"
                        )
                    dependency_id = self._query_model(
                        parameter.name, annotation, marker, default
                    )
                    prepared.append(
                        (parameter, annotation, Depends(), _MISSING, dependency_id)
                    )
                else:
                    prepared.append((parameter, annotation, marker, default, None))
            node_id = -1 if handler else len(self.dependencies)
            bindings = []
            for parameter, annotation, marker, default, dependency_id in prepared:
                if isinstance(marker, Depends):
                    bindings.append(
                        Binding(
                            parameter.name,
                            "dependency",
                            dependency=dependency_id,
                            use_cache=marker.use_cache,
                            scope=marker.scope,
                        )
                    )
                    continue
                if annotation is WebSocket:
                    if (
                        not self.websocket
                        or marker is not None
                        or default is not _MISSING
                    ):
                        raise ValueError(
                            f"{self.path}: WebSocket parameters are only injected into WebSocket routes"
                        )
                    if scope == "app":
                        raise ValueError(
                            f"{self.path}: application dependencies cannot access request-scoped values"
                        )
                    bindings.append(Binding(parameter.name, "websocket"))
                    continue
                if annotation is SecurityScopes:
                    if marker is not None or default is not _MISSING:
                        raise ValueError(
                            f"{self.path}: injected framework values cannot declare metadata or defaults"
                        )
                    if scope == "app":
                        raise ValueError(
                            f"{self.path}: application dependencies cannot access request-scoped values"
                        )
                    bindings.append(
                        Binding(parameter.name, "security_scopes", scopes=self.scopes)
                    )
                    continue
                if annotation in (Request, State, BackgroundTasks, Principal):
                    if marker is not None or default is not _MISSING:
                        raise ValueError(
                            f"{self.path}: injected framework values cannot declare metadata or defaults"
                        )
                    if scope == "app" and annotation is not State:
                        raise ValueError(
                            f"{self.path}: application dependencies cannot access request-scoped values"
                        )
                    kind = (
                        "request"
                        if annotation is Request
                        else "state"
                        if annotation is State
                        else "principal"
                        if annotation is Principal
                        else "background_tasks"
                    )
                    self.needs_request |= annotation is Request
                    self.needs_principal |= annotation is Principal
                    bindings.append(Binding(parameter.name, kind))
                    continue
                if scope == "app":
                    raise ValueError(
                        f"{self.path}: application dependencies cannot extract request inputs"
                    )
                key = (
                    parameter.name
                    if handler
                    else f"__dependency_{node_id}_{parameter.name}"
                )
                if self.websocket:
                    raise ValueError(
                        f"{self.path}: WebSocket handlers read path, query, header and cookie "
                        f"values from the WebSocket object; {parameter.name!r} is not injectable"
                    )
                self.input(parameter.name, key, annotation, marker, default, id(call))
                bindings.append(Binding(parameter.name, "input", key=key))
            kind = _callable_kind(call)
            if handler and kind in {"generator", "async_generator"}:
                raise ValueError(
                    f"{self.path}: route handlers cannot yield; use a generator dependency"
                )
            return CallablePlan(node_id, call, kind, tuple(bindings), scope)
        finally:
            self.active.discard(id(call))

    def file_input(
        self,
        name: str,
        key: str,
        inner: Any,
        many: bool,
        nullable: bool,
        marker: Parameter | None,
        default: Any,
    ) -> None:
        if marker is not None and not isinstance(marker, File):
            raise ValueError(
                f"{self.path}: UploadFile parameters must use File() or no marker"
            )
        if marker is not None:
            constrained = [
                field
                for field in (
                    "ge",
                    "gt",
                    "le",
                    "lt",
                    "multiple_of",
                    "min_length",
                    "max_length",
                    "pattern",
                    "strict",
                    "discriminator",
                    "default_factory",
                )
                if getattr(marker, field) is not None
            ]  # fmt: skip
            if constrained:
                raise ValueError(
                    f"{self.path}: File() does not support {', '.join(constrained)}; "
                    "bound upload sizes with max_body_size"
                )
        if self.body_owner is not None:
            raise ValueError(
                f"{self.path}: a route cannot read both a JSON body and form fields"
            )
        self.form_used = True
        alias = (
            marker.validation_alias
            if marker is not None and marker.validation_alias is not None
            else marker.alias
            if marker is not None and marker.alias is not None
            else name
        )
        if (
            not isinstance(alias, str)
            or not alias
            or any(character in alias for character in "\r\n\x00")
        ):
            raise ValueError(
                f"{self.path}: parameter aliases must be nonempty strings without control characters"
            )
        if marker is not None and marker.default not in (UNSET, Ellipsis):
            if default is not _MISSING:
                raise ValueError(f"{self.path}: input default was declared twice")
            default = marker.default
        if default is Ellipsis:
            default = _MISSING
        required = default is _MISSING
        if not required and (default is not None or not nullable):
            raise ValueError(
                f"{self.path}: optional file parameters need an Optional annotation and a None default"
            )
        raw = inner is bytes
        self.input_validators[key] = _file_validator(raw, many)
        parameter: dict[str, Any] = {
            "name": key,
            "alias": alias,
            "source": "file",
            "type": "bytes" if raw else "upload",
            "many": many,
            "required": required,
            "default": None,
            "schema": _file_schema(many, nullable),
        }
        if marker is not None:
            if marker.description is not None:
                if not isinstance(marker.description, str):
                    raise ValueError("parameter descriptions must be strings")
                parameter["description"] = marker.description
            if type(marker.deprecated) is not bool:
                raise TypeError("parameter deprecated must be a boolean")
            if marker.deprecated:
                parameter["deprecated"] = True
            _openapi_metadata(marker, parameter)
        self.parameters.append(parameter)

    def input(
        self,
        name: str,
        key: str,
        annotation: Any,
        marker: Parameter | None,
        default: Any,
        owner: int,
    ) -> None:
        from .request import UploadFile

        arguments = (name, key, annotation, marker, default, owner)
        annotation, nullable = optional(annotation)
        inner, many = annotation, False
        if (
            typing.get_origin(annotation) is list
            and len(typing.get_args(annotation)) == 1
        ):
            inner, many = typing.get_args(annotation)[0], True
        if inner is UploadFile or (isinstance(marker, File) and inner is bytes):
            self.file_input(name, key, inner, many, nullable, marker, default)
            return
        if isinstance(marker, File):
            raise ValueError(  # noqa: TRY004 -- Declaration errors are ValueErrors.
                f"{self.path}: File parameters must be UploadFile, bytes or a list of either"
            )
        primitive, converter, schema = _scalar(annotation)
        is_model = _is_model_type(annotation)
        is_model_union = _is_model_union(annotation)
        is_json = annotation in (dict, list) or typing.get_origin(annotation) in (
            dict,
            list,
        )
        if is_json and typing.get_args(annotation):
            allowed = (str, Any) if typing.get_origin(annotation) is dict else (Any,)
            if typing.get_args(annotation) != allowed:
                raise ValueError(
                    f"{self.path}: typed JSON container contents require a Pydantic model; use dict/list for raw JSON"
                )
        if primitive is None and not is_model and not is_model_union and not is_json:
            raise ValueError(
                f"{self.path}: parameter {name!r} needs a supported scalar, dict, list or Pydantic model annotation"
            )
        if marker is not None and not isinstance(marker, Parameter):
            raise ValueError(f"{self.path}: unsupported parameter metadata")
        source = (
            marker.source
            if marker is not None
            else "path"
            if name in self.path_names
            else "query"
            if primitive is not None
            else "body"
        )
        constraint_primitive = primitive
        alias = (
            marker.validation_alias
            if marker is not None and marker.validation_alias is not None
            else marker.alias
            if marker is not None and marker.alias is not None
            else name
        )
        if (
            not isinstance(alias, str)
            or not alias
            or any(character in alias for character in "\r\n\x00")
        ):
            raise ValueError(
                f"{self.path}: parameter aliases must be nonempty strings without control characters"
            )
        if (
            isinstance(marker, Header)
            and marker.alias is None
            and marker.validation_alias is None
            and marker.convert_underscores
        ):
            alias = alias.replace("_", "-")
        if source == "header":
            alias = alias.lower()
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", alias) is None:
                raise ValueError(
                    f"{self.path}: Header aliases must be valid ASCII HTTP field names"
                )
        if (
            source == "cookie"
            and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", alias) is None
        ):
            raise ValueError(
                f"{self.path}: Cookie aliases must be valid ASCII cookie names"
            )
        if marker is not None and type(marker.deprecated) is not bool:
            raise TypeError("parameter deprecated must be a boolean")
        if isinstance(marker, Header) and type(marker.convert_underscores) is not bool:
            raise TypeError("Header convert_underscores must be a boolean")
        if source == "path":
            if alias not in self.path_names or primitive is None or nullable:
                raise ValueError(
                    f"{self.path}: path parameters must name a declared non-null scalar placeholder"
                )
            self.found_path_names.add(alias)
        elif name in self.path_names and marker is not None:
            raise ValueError(
                f"{self.path}: a path placeholder cannot be extracted from another source"
            )
        if (
            marker is not None
            and marker.default is not UNSET
            and marker.default is not Ellipsis
        ):
            if default is not _MISSING:
                raise ValueError(f"{self.path}: input default was declared twice")
            default = marker.default
        if default is Ellipsis:
            default = _MISSING
        has_default_factory = marker is not None and marker.default_factory is not None
        if has_default_factory and default is not _MISSING:
            raise ValueError(f"{self.path}: input default was declared twice")
        required = default is _MISSING and not has_default_factory
        if source == "path" and not required:
            raise ValueError(
                f"{self.path}: path parameters must be required non-null scalars"
            )
        if source != "body" and primitive is None:
            raise ValueError(
                f"{self.path}: models and containers must be JSON body inputs"
            )
        if (
            marker is not None
            and marker.discriminator is not None
            and (source != "body" or not (is_model or is_model_union))
        ):
            raise ValueError(
                f"{self.path}: discriminator requires a JSON body model or union"
            )
        if source == "form":
            if self.body_owner is not None:
                raise ValueError(
                    f"{self.path}: a route cannot read both a JSON body and form fields"
                )
            self.form_used = True
        if source == "body":
            if self.form_used:
                raise ValueError(
                    f"{self.path}: a route cannot read both a JSON body and form fields"
                )
            if not self.body_compiled:
                # Whether this body is embedded depends on the others.
                self.body_owner = self.body_owner or (owner, name)
                self.body_inputs.append(
                    _BodyInput(
                        arguments,
                        name,
                        key,
                        arguments[2],
                        marker,
                        default,
                        owner,
                        alias,
                        len(self.parameters),
                    )
                )
                return
            self.body_owner = (owner, name)
            primitive = "json"
            if is_model_union or (
                is_model
                and marker is not None
                and (marker.discriminator is not None or marker.strict is not None)
            ):
                try:
                    adapter = _ModelAdapter(
                        annotation,
                        discriminator=(
                            marker.discriminator if marker is not None else None
                        ),
                        strict=marker.strict if marker is not None else None,
                    )
                    schema = adapter.json_schema()
                except Exception as error:
                    raise ValueError(
                        f"{self.path}: invalid body model or union: {error}"
                    ) from error
                self.body_models[key] = adapter
            elif is_model:
                self.body_models[key] = annotation
                schema = annotation_schema(annotation)
            elif is_json:
                self.body_containers[key] = typing.get_origin(annotation) or annotation
                schema = annotation_schema(annotation)
            else:
                scalar_type = {"str": str, "int": int, "float": float, "bool": bool}[
                    constraint_primitive
                ]
                if (
                    marker is not None
                    and marker.strict
                    and annotation in (UUID, date, datetime)
                ):
                    from pydantic import Field, TypeAdapter

                    converter = TypeAdapter(
                        typing.Annotated[annotation, Field(strict=True)]
                    ).validate_python
                self.input_validators[key] = _primitive_body_validator(scalar_type)
                schema = annotation_schema(annotation)
            if nullable:
                self.nullable_body.add(key)
        if not required and not has_default_factory:
            if default is None and not nullable:
                raise ValueError(
                    f"{self.path}: a None default requires an optional annotation"
                )
            if default is not None:
                if source == "body":
                    raise ValueError(f"{self.path}: JSON body defaults must be None")
                if converter is not None:
                    try:
                        converter(default)
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            f"{self.path}: default for {name!r} does not match its annotation"
                        ) from error
                elif (
                    primitive in {"str", "int", "bool"}
                    and type(default) is not annotation
                ):
                    raise ValueError(
                        f"{self.path}: default for {name!r} does not match its annotation"
                    )
                elif primitive == "float" and (
                    isinstance(default, bool)
                    or not isinstance(default, (int, float))
                    or not math.isfinite(default)
                ):
                    raise ValueError(
                        f"{self.path}: float defaults must be finite numbers"
                    )
        constraints, validator = _constraints(
            marker, constraint_primitive or primitive, schema
        )
        if source == "body":
            validator = _merge_validators(
                self.input_validators.get(key), validator, nullable
            )
        validator = _merge_validators(validator, converter, nullable)
        strict_non_str = (
            source != "body"
            and marker is not None
            and bool(marker.strict)
            and (constraint_primitive != "str" or annotation in (UUID, date, datetime))
        )
        if strict_non_str:
            try:
                from pydantic import Field, TypeAdapter
            except ImportError as error:
                raise ValueError(
                    "strict parameter validation requires the optional Pydantic v2 dependency"
                ) from error
            strict_adapter = TypeAdapter(
                typing.Annotated[annotation, Field(strict=True)]
            )
            inner_validator = validator
            factory = marker.default_factory if has_default_factory else None
            fallback_default = None if required else default

            def validate_strict(value: Any) -> Any:
                if value is None:
                    resolved = factory() if factory is not None else fallback_default
                    return (
                        inner_validator(resolved)
                        if inner_validator is not None
                        else resolved
                    )
                checked = strict_adapter.validate_python(value)
                return (
                    inner_validator(checked) if inner_validator is not None else checked
                )

            validator = validate_strict
        elif has_default_factory:
            inner_validator = validator
            factory = marker.default_factory

            def validate_factory(value: Any) -> Any:
                if value is None:
                    value = factory()
                return inner_validator(value) if inner_validator is not None else value

            validator = validate_factory
        if (
            validator is not None
            and constraints
            and "multiple_of" not in constraints
            and not strict_non_str
            and not has_default_factory
            and converter is None
            and marker.pattern is None
            and source in _NATIVE_SCALAR_SOURCES
            and primitive in {"int", "float", "str"}
        ):
            # The validator only checks these bounds, so the native invoker may
            # check them itself and skip calling it when they pass.
            validator.native_constraints = dict(constraints)
        if (
            not required
            and not has_default_factory
            and default is not None
            and validator is not None
        ):
            try:
                if strict_non_str:
                    validator(None)
                else:
                    validator(_json_value(default))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"{self.path}: default for {name!r} violates its declared constraints"
                ) from error
        if validator is not None:
            self.input_validators[key] = validator
        if nullable:
            schema = {"anyOf": [schema, {"type": "null"}]}
        json_default = None if required or has_default_factory else _json_value(default)
        parameter = {
            "name": key,
            "alias": alias,
            "source": source,
            "type": "str" if strict_non_str else primitive,
            "required": required,
            "default": None if strict_non_str else json_default,
            "schema": schema,
        }
        if constraints:
            parameter["constraints"] = constraints
        if marker is not None:
            if marker.description is not None:
                if not isinstance(marker.description, str):
                    raise ValueError("parameter descriptions must be strings")
                parameter["description"] = marker.description
            if marker.deprecated:
                parameter["deprecated"] = True
            _openapi_metadata(
                marker,
                parameter,
                openapi_default=json_default
                if strict_non_str and not required and not has_default_factory
                else UNSET,
            )
        self.parameters.append(parameter)

    def compile_body(self, name: str) -> tuple | None:
        """Compile the body inputs, once all of them are known.

        One body input (bound once, or by the same parameter more than once)
        keeps the plain body path. Several, or an embedded one, are each read
        from their own key of one JSON object, as in FastAPI.
        """
        inputs, self.body_inputs = self.body_inputs, []
        self.body_compiled = True
        if not inputs:
            return None
        if len({(item.owner, item.name) for item in inputs}) == 1 and not any(
            isinstance(item.marker, Body) and item.marker.embed for item in inputs
        ):
            for index, item in enumerate(inputs):
                self.input(*item.arguments)
                # Keep the declaration order the native runtime checks in.
                self.parameters.insert(item.position + index, self.parameters.pop())
            return None
        return self._embedded_body(inputs, name)

    def _embedded_body(self, inputs: list[_BodyInput], name: str) -> tuple:
        try:
            from pydantic import BaseModel, Field, create_model
        except ImportError as error:
            raise ValueError(
                f"{self.path}: embedded or several body parameters require the"
                " optional Pydantic v2 dependency"
            ) from error
        fields: dict[str, Any] = {}
        # The first input of each alias, by alias, with its model attribute.
        declared: dict[str, tuple[_BodyInput, str]] = {}
        keys = []
        required = False
        media_type = None
        for item in inputs:
            marker = item.marker
            if item.alias in declared:
                first, attribute = declared[item.alias]
                if (first.annotation, first.marker, first.default) != (
                    item.annotation,
                    marker,
                    item.default,
                ):
                    raise ValueError(
                        f"{self.path}: body parameters named {item.alias!r} conflict"
                    )
                keys.append((attribute, item.key))
                continue
            attribute = item.name
            options: dict[str, Any] = {}
            if (
                not attribute.isidentifier()
                or attribute.startswith(("_", "model_"))
                or hasattr(BaseModel, attribute)
                or attribute in fields
            ):
                number = len(fields)
                while f"field_{number}" in fields:
                    number += 1
                attribute = f"field_{number}"
                options["title"] = item.name.replace("_", " ").title()
            if item.alias != attribute:
                options["alias"] = item.alias
            if marker is not None:
                if marker.openapi_examples is not None or not marker.include_in_schema:
                    raise ValueError(
                        f"{self.path}: embedded body parameters do not support"
                        " openapi_examples or include_in_schema=False"
                    )
                constraints = (
                    "ge",
                    "gt",
                    "le",
                    "lt",
                    "multiple_of",
                    "min_length",
                    "max_length",
                    "pattern",
                )
                # Pydantic applies these after conversion, so only types it
                # compares or measures as given (not models, containers,
                # UUIDs or dates) can take them, and each only where it fits,
                # as for any other parameter.
                base = optional(item.annotation)[0]
                if any(getattr(marker, option) is not None for option in constraints):
                    primitive = {int: "int", float: "float", str: "str"}.get(base)
                    if primitive is None:
                        raise ValueError(
                            f"{self.path}: constraints on embedded body parameter"
                            f" {item.alias!r} apply to int, float and str only;"
                            " validate other types in a model"
                        )
                    try:
                        _constraints(marker, primitive, {})
                    except ValueError as error:
                        raise ValueError(
                            f"{self.path}: embedded body parameter {item.alias!r}: {error}"
                        ) from error
                for option in (
                    "ge", "gt", "le", "lt", "multiple_of", "min_length", "max_length",
                    "pattern", "strict", "discriminator", "validation_alias",
                    "serialization_alias", "title", "description", "examples",
                    "json_schema_extra",
                ):  # fmt: skip
                    value = getattr(marker, option)
                    if value is not None:
                        options[option] = value
                if marker.deprecated:
                    options["deprecated"] = True
                if media_type is None and marker.media_type != Body.media_type:
                    media_type = marker.media_type
            has_factory = marker is not None and marker.default_factory is not None
            required |= item.default is _MISSING and not has_factory
            if has_factory:
                fields[attribute] = (
                    item.annotation,
                    Field(default_factory=marker.default_factory, **options),
                )
            else:
                default = ... if item.default is _MISSING else item.default
                fields[attribute] = (item.annotation, Field(default, **options))
            declared[item.alias] = (item, attribute)
            keys.append((attribute, item.key))
        title = "Body_" + (re.sub(r"[^a-zA-Z0-9_]", "_", name) or "body")
        try:
            model = create_model(title, **fields)
            schema = annotation_schema(model)
        except Exception as error:
            raise ValueError(
                f"{self.path}: invalid body parameters: {error}"
            ) from error
        parameter: dict[str, Any] = {
            "name": EMBEDDED_BODY_KEY,
            "alias": EMBEDDED_BODY_KEY,
            "source": "body",
            "type": "json",
            # Missing fields are reported one by one, under their own names.
            "required": False,
            "default": None,
            "schema": schema,
            "embedded": {"required": required},
        }
        if media_type is not None:
            parameter["openapi"] = {"media_type": media_type}
        self.parameters.insert(inputs[0].position, parameter)
        return EMBEDDED_BODY_KEY, model, tuple(declared), tuple(keys)


def _openapi_metadata(
    marker: Parameter,
    parameter: dict[str, Any],
    *,
    openapi_default: Any = UNSET,
) -> None:
    """Record a marker's documentation-only options for the OpenAPI builder.

    The native runtime reads only the keys it needs, so these never reach a
    request path.
    """
    metadata: dict[str, Any] = {}
    for option in (
        "title",
        "examples",
        "openapi_examples",
        "json_schema_extra",
    ):
        value = getattr(marker, option)
        if value is not None:
            metadata[option] = value
    if not marker.include_in_schema:
        metadata["include_in_schema"] = False
    if marker.default_factory is not None:
        metadata["default_factory"] = True
    if openapi_default is not UNSET:
        metadata["openapi_default"] = openapi_default
    media_type = getattr(marker, "media_type", None)
    if media_type is not None and type(marker).media_type != media_type:
        metadata["media_type"] = media_type
    if metadata:
        parameter["openapi"] = metadata


def _file_schema(many: bool, nullable: bool) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "string",
        "contentMediaType": "application/octet-stream",
    }
    if many:
        schema = {"type": "array", "items": schema}
    if nullable:
        schema = {"anyOf": [schema, {"type": "null"}]}
    return schema


def _file_validator(raw: bool, many: bool) -> Callable[[Any], Any]:
    from .request import _upload

    convert = (lambda value: value) if raw else _upload

    def validate(value: Any) -> Any:
        if value is None:
            return None
        return [convert(item) for item in value] if many else convert(value)

    return validate


def compile_inputs(
    call: Callable,
    path: str,
    dependencies: tuple[Depends, ...] = (),
    *,
    websocket: bool = False,
    overrides: Mapping[Callable, Callable] | None = None,
) -> CompiledInputs:
    compiler = _Compiler(path, websocket=websocket, overrides=overrides)
    before = []
    for dependency in dependencies:
        if not isinstance(dependency, Depends):
            raise TypeError("route dependencies must be Depends instances")
        before.append(
            Binding(
                "",
                "dependency",
                dependency=compiler._dependency(dependency),
                use_cache=dependency.use_cache,
                scope=dependency.scope,
            )
        )
    plan = compiler.callable(call, handler=True)
    embedded_body = compiler.compile_body(getattr(call, "__name__", "body"))
    missing_names = (
        compiler.path_names - compiler.found_path_names - compiler.overridden_path_names
    )
    if not websocket and missing_names:
        missing = ", ".join(sorted(missing_names))
        raise ValueError(
            f"{path}: path parameters missing from handler or dependencies: {missing}"
        )
    return CompiledInputs(
        plan,
        tuple(compiler.dependencies),
        tuple(before),
        tuple(compiler.parameters),
        compiler.body_models,
        compiler.body_containers,
        frozenset(compiler.nullable_body),
        compiler.input_validators,
        compiler.needs_request,
        compiler.needs_principal,
        tuple(compiler.security),
        embedded_body,
        tuple(compiler.openapi_parameters),
    )


def _primitive_body_validator(annotation: Any) -> Callable[[Any], Any]:
    def validate(value: Any) -> Any:
        if annotation is float:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Expected a finite JSON number")
            return float(value)
        if type(value) is not annotation:
            raise ValueError(f"Expected a JSON {annotation.__name__}")
        return value

    return validate


def annotation_schema(annotation: Any) -> dict[str, Any]:
    annotation, nullable = optional(annotation)
    primitive, _, schema = _scalar(annotation)
    if primitive is not None:
        result = schema
    elif annotation is type(None):
        result = {"type": "null"}
    elif annotation is Any or annotation is _MISSING or annotation is None:
        result = {}
    elif isinstance(annotation, type) and callable(
        getattr(annotation, "model_json_schema", None)
    ):
        result = annotation.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
    elif annotation is dict or typing.get_origin(annotation) is dict:
        result = {"type": "object"}
    elif annotation is list or typing.get_origin(annotation) is list:
        arguments = typing.get_args(annotation)
        result = {
            "type": "array",
            "items": annotation_schema(arguments[0]) if arguments else {},
        }
    else:
        try:
            from pydantic import TypeAdapter
        except ImportError as error:
            raise ValueError(
                "this schema requires the optional Pydantic v2 dependency"
            ) from error
        try:
            result = TypeAdapter(annotation).json_schema(
                ref_template="#/components/schemas/{model}"
            )
        except Exception as error:
            raise ValueError(
                f"unsupported schema annotation: {annotation!r}"
            ) from error
    return {"anyOf": [result, {"type": "null"}]} if nullable else result


class _JSONAdapter:
    def __init__(self, annotation: Any) -> None:
        base, _ = optional(annotation)
        origin = typing.get_origin(base)
        arguments = typing.get_args(base)
        raw_container = base in (dict, list) or (
            origin is dict
            and arguments == (str, Any)
            or origin is list
            and arguments == (Any,)
        )
        if base not in (Any, str, int, float, bool, type(None)) and not raw_container:
            raise ValueError(
                "this response model requires the optional Pydantic v2 dependency"
            )
        self.annotation = annotation

    def validate_python(self, value: Any) -> Any:
        annotation, nullable = optional(self.annotation)
        if nullable and value is None or annotation is Any:
            return value
        if annotation in (dict, list) or typing.get_origin(annotation) in (dict, list):
            kind = typing.get_origin(annotation) or annotation
            if not isinstance(value, kind):
                raise ValueError("response does not match its declared JSON container")
            return value
        return _primitive_body_validator(annotation)(value)

    def dump_python(
        self,
        value: Any,
        *,
        mode: str = "json",
        by_alias: bool = True,
        warnings: bool | str = "error",
    ) -> Any:
        # Worker applies bounded normalization. Copying containers here would
        # allocate before that limit can reject an oversized response.
        return value

    def json_schema(
        self,
        *,
        ref_template: str = "#/components/schemas/{model}",
        mode: str = "validation",
    ) -> dict[str, Any]:
        return annotation_schema(self.annotation)


def response_adapter(annotation: Any) -> Any:
    if annotation is None or annotation is _MISSING or annotation is Any:
        return None
    try:
        from .responses import Response
    except ImportError:
        Response = None
    if (
        Response is not None
        and isinstance(annotation, type)
        and issubclass(annotation, Response)
    ):
        return None
    try:
        from pydantic import TypeAdapter
    except ImportError:
        annotation_schema(annotation)
        return _JSONAdapter(annotation)
    try:
        return TypeAdapter(annotation)
    except Exception as error:
        raise ValueError(f"unsupported response model: {annotation!r}") from error
