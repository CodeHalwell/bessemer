"""Helpers matching ``fastapi.utils``."""

from __future__ import annotations

import re
from typing import Any


def generate_unique_id(route: Any) -> str:
    """FastAPI's default OpenAPI ``operationId``: name, path and method.

    Pass ``App(generate_unique_id_function=generate_unique_id)`` to give a
    ported application the operation IDs (and generated client method
    names) it had under FastAPI.
    """
    operation_id = re.sub(r"\W", "_", f"{route.name}{route.path_format}")
    return f"{operation_id}_{min(route.methods).lower()}"


__all__ = ["generate_unique_id"]
