"""Validated command-line, environment and project settings."""

from __future__ import annotations

import argparse
import math
import os
import tomllib
from pathlib import Path

SERVER_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8000,
    "max_body_size": 1_048_576,
    "max_request_bytes_in_flight": 33_554_432,
    "max_in_flight": 128,
    "request_timeout": 30.0,
    "graceful_timeout": 30.0,
    "max_requests": 0,
    "max_response_size": 4_194_304,
    "max_response_bytes_in_flight": 33_554_432,
    "max_connections": 1024,
    "header_timeout": 10.0,
    "max_headers": 64,
    "max_header_size": 32768,
    "max_request_target_size": 8192,
    "tls_cert": None,
    "tls_key": None,
    "tls_min": "1.3",
    "tls_client_ca": None,
    "tls_client_crl": None,
    "tls_ocsp_response": None,
    "tls_client_auth": "required",
    "tls_reload_interval": 60.0,
    "http": "auto",
    "websocket_compression": "none",
    "max_upload_size": 0,
    "max_upload_bytes_in_flight": 4_294_967_296,
    "upload_dir": None,
    "loops": 1,
    "event_loop": "asyncio",
    "io_backend": "epoll",
}
# Settings whose default is None take text from the environment.
TLS_VERSIONS = ("1.2", "1.3")
HTTP_VERSIONS = ("auto", "1.1")
WEBSOCKET_COMPRESSIONS = ("none", "deflate")
EVENT_LOOPS = ("asyncio", "bessemer")
IO_BACKENDS = ("epoll", "io_uring")
CLIENT_CERTIFICATE_MODES = ("required", "optional")
# Settings that take one of a fixed set of values.
CHOICES = {
    "tls_min": TLS_VERSIONS,
    "tls_client_auth": CLIENT_CERTIFICATE_MODES,
    "http": HTTP_VERSIONS,
    "websocket_compression": WEBSOCKET_COMPRESSIONS,
    "event_loop": EVENT_LOOPS,
    "io_backend": IO_BACKENDS,
}
MAX_LOOPS = 256
_OPTIONAL_TEXT = frozenset(
    {
        "tls_cert",
        "tls_key",
        "tls_client_ca",
        "tls_client_crl",
        "tls_ocsp_response",
        "upload_dir",
    }
)
SUPERVISOR_DEFAULTS = {
    "workers": 0,
    "startup_timeout": 30.0,
    "worker_timeout": 30.0,
    "max_lifetime": 0.0,
    "max_rss": 0,
    "restart_limit": 5,
    "restart_backoff": 0.1,
}
DEFAULTS = SERVER_DEFAULTS | SUPERVISOR_DEFAULTS

SERVER_INTEGER_LIMITS = {
    "max_body_size": (1, 1_073_741_824),
    "max_request_bytes_in_flight": (1, 1_073_741_824),
    "max_in_flight": (1, 1_000_000),
    "max_response_size": (1, 67_108_864),
    "max_response_bytes_in_flight": (1, 1_073_741_824),
    "max_connections": (1, 100_000),
    "max_headers": (1, 1024),
    "max_header_size": (8192, 1_048_576),
    "max_request_target_size": (1, 1_048_576),
    "max_upload_size": (0, 68_719_476_736),
    "max_upload_bytes_in_flight": (1, 1_099_511_627_776),
}


def validate_choice(name: str, value) -> None:
    """Reject a value outside the fixed set ``CHOICES[name]``."""
    allowed = CHOICES[name]
    if value not in allowed:
        raise ValueError(f"{name} must be {' or '.join(map(repr, allowed))}")


def validate_choices(values: dict) -> None:
    """Check every fixed-choice setting present in ``values``."""
    for name in CHOICES:
        if name in values:
            validate_choice(name, values[name])


def validate_loops(loops) -> None:
    if type(loops) is not int or not 1 <= loops <= MAX_LOOPS:
        raise ValueError(f"loops must be an integer between 1 and {MAX_LOOPS}")


def validate_server_limits(values: dict) -> None:
    """Reject native transport limits before starting application resources."""
    for name, (minimum, maximum) in SERVER_INTEGER_LIMITS.items():
        value = values[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(
                f"{name} must be an integer between {minimum} and {maximum}"
            )
    for name, individual in (
        ("max_request_bytes_in_flight", "max_body_size"),
        ("max_response_bytes_in_flight", "max_response_size"),
    ):
        if values[name] < values[individual]:
            raise ValueError(f"{name} must be at least {individual}")
    if values["max_upload_size"]:
        if values["max_upload_size"] < values["max_body_size"]:
            raise ValueError("max_upload_size must be 0 or at least max_body_size")
        if values["max_upload_bytes_in_flight"] < values["max_upload_size"]:
            raise ValueError(
                "max_upload_bytes_in_flight must be at least max_upload_size"
            )
    elif values.get("upload_dir") is not None:
        raise ValueError("upload_dir needs max_upload_size")


def settings(arguments: argparse.Namespace) -> dict:
    """Resolve CLI > environment > pyproject > defaults without importing apps."""
    values = {}
    config_path = Path("pyproject.toml")
    if config_path.is_file():
        with config_path.open("rb") as source:
            values = tomllib.load(source).get("tool", {}).get("bessemer", {})
        if not isinstance(values, dict):
            raise ValueError("[tool.bessemer] must be a table")
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise ValueError(
                f"unsupported Bessemer settings: {', '.join(sorted(unknown))}"
            )
    result = DEFAULTS | values
    for name, default in DEFAULTS.items():
        environment = os.environ.get("BESSEMER_" + name.upper())
        if environment is not None:
            try:
                result[name] = (
                    environment
                    if name in _OPTIONAL_TEXT
                    else type(default)(environment)
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"BESSEMER_{name.upper()} has an invalid value"
                ) from error
        override = getattr(arguments, name, None)
        if override is not None:
            result[name] = override
    if not isinstance(result["host"], str) or not result["host"]:
        raise ValueError("host must be a nonempty string")
    integers = tuple(name for name, default in DEFAULTS.items() if type(default) is int)
    for name in integers:
        if type(result[name]) is not int:
            raise ValueError(f"{name} must be an integer")
        if result[name] > 2**63 - 1:
            raise ValueError(f"{name} exceeds the supported signed 64-bit range")
    for name in _OPTIONAL_TEXT:
        if result[name] is not None and (
            not isinstance(result[name], str) or not result[name]
        ):
            raise ValueError(f"{name} must be a nonempty path")
    if (result["tls_cert"] is None) != (result["tls_key"] is None):
        raise ValueError("tls_cert and tls_key must be given together")
    if result["tls_client_ca"] is not None and result["tls_cert"] is None:
        raise ValueError("tls_client_ca needs tls_cert and tls_key")
    if result["tls_client_crl"] is not None and result["tls_client_ca"] is None:
        raise ValueError("tls_client_crl needs tls_client_ca")
    if result["tls_ocsp_response"] is not None and result["tls_cert"] is None:
        raise ValueError("tls_ocsp_response needs tls_cert and tls_key")
    if not 0 <= result["port"] <= 65535:
        raise ValueError("port must be between 0 and 65535")
    validate_server_limits(result)
    validate_loops(result["loops"])
    validate_choices(result)
    for name in ("max_requests", "workers", "max_rss", "restart_limit"):
        if result[name] < 0:
            raise ValueError(f"{name} cannot be negative")
    if result["workers"] > 256:
        raise ValueError("workers cannot exceed 256")
    for name, default in DEFAULTS.items():
        if type(default) is not float:
            continue
        value = result[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be a finite number")
        minimum = (
            0
            if name in {"max_lifetime", "worker_timeout", "tls_reload_interval"}
            else 1e-9
        )
        if value < minimum or value > 86_400:
            raise ValueError(f"{name} must be between {minimum} and 86400 seconds")
        result[name] = float(value)
    if 0 < result["worker_timeout"] < 1:
        raise ValueError("worker_timeout must be 0 or at least one second")
    return result
