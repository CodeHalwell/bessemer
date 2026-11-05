"""OpenTelemetry tracing settings and the hand-off to the Python SDK."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SAMPLERS = {
    "always_on": 1.0,
    "parentbased_always_on": 1.0,
    "always_off": 0.0,
    "parentbased_always_off": 0.0,
}


@dataclass(frozen=True)
class OpenTelemetry:
    """Native W3C Trace Context propagation and OTLP span export.

    With an ``endpoint`` (the collector's OTLP/HTTP traces URL, such as
    ``http://collector:4318/v1/traces``) every request gets a server span,
    exported as OTLP/HTTP JSON in the background. The span joins the
    caller's trace when a valid ``traceparent`` arrives; otherwise a new trace
    starts, sampled with probability ``sample_ratio``. Sampling is parent
    based: a caller's sampling decision is always kept.

    Without an endpoint the incoming ``traceparent`` is only propagated, so
    spans you record with the OpenTelemetry SDK still join the caller's trace.

    ``headers_env`` names an environment variable holding export headers in
    the OTLP format (``key=value,key2=value2``, values percent-encoded), read
    at startup, for collector authentication. ``ca_file`` adds a private CA
    for an ``https`` endpoint.
    """

    endpoint: str | None = None
    service_name: str = "bessemer"
    sample_ratio: float = 1.0
    headers_env: str | None = None
    ca_file: str | None = None

    def __post_init__(self) -> None:
        otel_dict(self)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OpenTelemetry:
        """Settings from the standard ``OTEL_*`` environment variables.

        Reads ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (or
        ``OTEL_EXPORTER_OTLP_ENDPOINT`` plus ``/v1/traces``),
        ``OTEL_SERVICE_NAME``, ``OTEL_TRACES_SAMPLER`` and
        ``OTEL_TRACES_SAMPLER_ARG``, and uses
        ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` or ``OTEL_EXPORTER_OTLP_HEADERS``
        for headers and ``OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE`` or
        ``OTEL_EXPORTER_OTLP_CERTIFICATE`` for a CA file. Only the
        ``http/json`` protocol is exported; other protocols, samplers and
        ``OTEL_SDK_DISABLED`` are rejected rather than ignored.
        """
        env = os.environ if environ is None else environ

        def first(*names: str) -> tuple[str | None, str | None]:
            for name in names:
                value = env.get(name)
                if value:
                    return name, value
            return None, None

        if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
            raise ValueError(
                "OTEL_SDK_DISABLED is set; leave App(otel=...) unset to disable tracing"
            )
        _, protocol = first(
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_EXPORTER_OTLP_PROTOCOL"
        )
        if protocol is not None and protocol != "http/json":
            raise ValueError(
                f"OTLP protocol {protocol!r} is not supported; Bessemer exports http/json"
            )
        _, endpoint = first("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        if endpoint is None:
            _, base = first("OTEL_EXPORTER_OTLP_ENDPOINT")
            if base is not None:
                endpoint = base.rstrip("/") + "/v1/traces"
        sampler = env.get("OTEL_TRACES_SAMPLER", "parentbased_always_on")
        argument = env.get("OTEL_TRACES_SAMPLER_ARG")
        if sampler in _SAMPLERS:
            ratio = _SAMPLERS[sampler]
        elif sampler in ("traceidratio", "parentbased_traceidratio"):
            try:
                ratio = 1.0 if argument is None else float(argument)
            except ValueError as error:
                raise ValueError(
                    "OTEL_TRACES_SAMPLER_ARG must be a number from 0 to 1"
                ) from error
        else:
            raise ValueError(f"OTEL_TRACES_SAMPLER {sampler!r} is not supported")
        headers_env, _ = first(
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS"
        )
        _, ca_file = first(
            "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", "OTEL_EXPORTER_OTLP_CERTIFICATE"
        )
        return cls(
            endpoint=endpoint,
            service_name=env.get("OTEL_SERVICE_NAME") or "bessemer",
            sample_ratio=ratio,
            headers_env=headers_env if endpoint is not None else None,
            ca_file=ca_file if endpoint is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "service_name": self.service_name,
            "sample_ratio": self.sample_ratio,
            "headers_env": self.headers_env,
            "ca_file": self.ca_file,
        }


def otel_dict(value: Any) -> dict[str, Any]:
    """Validate OpenTelemetry settings (an instance or a dictionary)."""
    if isinstance(value, OpenTelemetry):
        value = value.to_dict()
    keys = {"endpoint", "service_name", "sample_ratio", "headers_env", "ca_file"}
    if not isinstance(value, dict) or set(value) - keys:
        raise ValueError(
            "otel must be OpenTelemetry or a dictionary of endpoint, service_name, "
            "sample_ratio, headers_env and ca_file"
        )
    endpoint = value.get("endpoint")
    if endpoint is not None and (
        not isinstance(endpoint, str)
        or not endpoint.startswith(("http://", "https://"))
        or len(endpoint) > 2048
    ):
        raise ValueError("otel endpoint must be an http or https URL")
    service = value.get("service_name", "bessemer")
    if not isinstance(service, str) or not 1 <= len(service) <= 256:
        raise ValueError("otel service_name must have 1 to 256 characters")
    ratio = value.get("sample_ratio", 1.0)
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(ratio)
        or not 0 <= ratio <= 1
    ):
        raise ValueError("otel sample_ratio must be a number from 0 to 1")
    headers_env = value.get("headers_env")
    if headers_env is not None and (
        not isinstance(headers_env, str) or not _ENVIRONMENT.fullmatch(headers_env)
    ):
        raise ValueError("otel headers_env must be an environment variable name")
    ca_file = value.get("ca_file")
    if ca_file is not None and (not isinstance(ca_file, str) or not ca_file):
        raise ValueError("otel ca_file must be a path")
    if endpoint is None and (headers_env is not None or ca_file is not None):
        raise ValueError("otel headers_env and ca_file need an endpoint")
    return {
        "endpoint": endpoint,
        "service_name": service,
        "sample_ratio": float(ratio),
        "headers_env": headers_env,
        "ca_file": ca_file,
    }


# The OpenTelemetry API, once imported; False when it is not installed.
_api: Any = None


def _load() -> Any:
    global _api
    if _api is None:
        try:
            from opentelemetry import context
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            _api = (context, TraceContextTextMapPropagator())
        except ImportError:
            _api = False
    return _api


def attach(trace: Mapping[str, Any]) -> Any:
    """Make the request's span the current OpenTelemetry context, if the
    OpenTelemetry API is installed. Returns a token for :func:`detach`."""
    api = _load()
    if not api:
        return None
    context, propagator = api
    carrier = {"traceparent": trace["traceparent"]}
    if trace.get("tracestate"):
        carrier["tracestate"] = trace["tracestate"]
    return context.attach(propagator.extract(carrier))


def detach(token: Any) -> None:
    if token is not None:
        _api[0].detach(token)
