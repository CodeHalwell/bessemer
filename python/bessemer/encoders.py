"""``jsonable_encoder``: turn Python values into JSON-compatible ones.

Handlers do not need it: Bessemer encodes return values natively. It is
here for code that builds JSON-compatible data itself, as FastAPI code
often does before storing or comparing values.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import deque
from collections.abc import Callable, Mapping
from decimal import Decimal
from enum import Enum
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import PurePath
from re import Pattern
from types import GeneratorType
from typing import Any
from uuid import UUID


def _decimal(value: Decimal) -> int | float:
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= 0:
        return int(value)
    return float(value)


_ENCODERS: dict[type, Callable[[Any], Any]] = {
    bytes: lambda value: value.decode(),
    dt.date: lambda value: value.isoformat(),
    dt.datetime: lambda value: value.isoformat(),
    dt.time: lambda value: value.isoformat(),
    dt.timedelta: lambda value: value.total_seconds(),
    Decimal: _decimal,
    Enum: lambda value: value.value,
    frozenset: list,
    deque: list,
    GeneratorType: list,
    IPv4Address: str,
    IPv4Interface: str,
    IPv4Network: str,
    IPv6Address: str,
    IPv6Interface: str,
    IPv6Network: str,
    PurePath: str,
    Pattern: lambda value: value.pattern,
    set: list,
    UUID: str,
}


def jsonable_encoder(
    obj: Any,
    include: set | dict | None = None,
    exclude: set | dict | None = None,
    by_alias: bool = True,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    custom_encoder: dict[Any, Callable[[Any], Any]] | None = None,
    sqlalchemy_safe: bool = True,
) -> Any:
    """Convert ``obj`` to values ``json.dumps`` accepts, as FastAPI does.

    Pydantic models are dumped in JSON mode with the given options;
    dataclasses, mappings, sequences, enums, dates, UUIDs, decimals and
    paths are converted recursively. ``custom_encoder`` maps types to
    functions and takes precedence.
    """
    custom_encoder = custom_encoder or {}
    if custom_encoder:
        if type(obj) in custom_encoder:
            return custom_encoder[type(obj)](obj)
        for encoder_type, encoder in custom_encoder.items():
            if isinstance(obj, encoder_type):
                return encoder(obj)
    if include is not None and not isinstance(include, (set, dict)):
        include = set(include)
    if exclude is not None and not isinstance(exclude, (set, dict)):
        exclude = set(exclude)
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump) and hasattr(type(obj), "model_fields"):
        data = model_dump(
            mode="json",
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
        return jsonable_encoder(
            data,
            exclude_none=exclude_none,
            custom_encoder=custom_encoder,
            sqlalchemy_safe=sqlalchemy_safe,
        )
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable_encoder(
            dataclasses.asdict(obj),
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            custom_encoder=custom_encoder,
            sqlalchemy_safe=sqlalchemy_safe,
        )
    if isinstance(obj, Enum):
        return jsonable_encoder(obj.value, custom_encoder=custom_encoder)
    if isinstance(obj, PurePath):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float)):
        return obj
    options = {
        "by_alias": by_alias,
        "exclude_unset": exclude_unset,
        "exclude_defaults": exclude_defaults,
        "exclude_none": exclude_none,
        "custom_encoder": custom_encoder,
        "sqlalchemy_safe": sqlalchemy_safe,
    }
    if isinstance(obj, Mapping):
        keys = set(obj.keys())
        if include is not None:
            keys &= set(include)
        if exclude is not None:
            keys -= set(exclude)
        return {
            jsonable_encoder(key, **options): jsonable_encoder(value, **options)
            for key, value in obj.items()
            if key in keys
            and (not sqlalchemy_safe or not str(key).startswith("_sa"))
            and (value is not None or not exclude_none)
        }
    if isinstance(obj, (list, set, frozenset, GeneratorType, tuple, deque)):
        return [jsonable_encoder(item, **options) for item in obj]
    for base in type(obj).__mro__:
        if base in _ENCODERS:
            return _ENCODERS[base](obj)
    try:
        data = dict(obj)
    except (TypeError, ValueError):
        try:
            data = vars(obj)
        except TypeError as error:
            raise ValueError(
                f"cannot convert {type(obj).__name__} to a JSON-compatible value"
            ) from error
    return jsonable_encoder(
        data,
        include=include,
        exclude=exclude,
        **options,
    )


__all__ = ["jsonable_encoder"]
