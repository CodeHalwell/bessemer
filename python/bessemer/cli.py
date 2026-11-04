"""Command-line entry point for the initial single-worker runtime."""

from __future__ import annotations

import argparse
import importlib
import math
import os
import sys
import tomllib
from pathlib import Path

from .application import App

_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8000,
    "max_body_size": 1_048_576,
    "max_in_flight": 128,
    "request_timeout": 30.0,
    "graceful_timeout": 30.0,
}


def _settings(arguments: argparse.Namespace) -> dict:
    config_path = Path("pyproject.toml")
    values = {}
    if config_path.is_file():
        with config_path.open("rb") as source:
            values = tomllib.load(source).get("tool", {}).get("bessemer", {})
        unknown = set(values) - set(_DEFAULTS)
        if unknown:
            raise ValueError(
                f"unsupported Bessemer settings: {', '.join(sorted(unknown))}"
            )
    settings = dict(_DEFAULTS)
    settings.update(values)
    for key, default in _DEFAULTS.items():
        environment = os.environ.get("BESSEMER_" + key.upper())
        if environment is not None:
            settings[key] = type(default)(environment)
        override = getattr(arguments, key, None)
        if override is not None:
            settings[key] = override
    if not isinstance(settings["host"], str) or not settings["host"]:
        raise ValueError("host must be a nonempty string")
    for key in ("port", "max_body_size", "max_in_flight"):
        if type(settings[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    if not 0 <= settings["port"] <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if settings["max_body_size"] < 1 or settings["max_in_flight"] < 1:
        raise ValueError("body size and in-flight limits must be positive")
    for key in ("request_timeout", "graceful_timeout"):
        value = settings[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} must be a positive finite number")
        settings[key] = float(value)
    return settings


def load_app(target: str) -> App:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute or ":" in attribute:
        raise ValueError("application must have the form module:app")
    # Python console scripts put their installation directory first; the app
    # belongs to the explicit working directory, just as with python -m.
    directory = str(Path.cwd())
    if directory not in sys.path:
        sys.path.insert(0, directory)
    module = importlib.import_module(module_name)
    app = getattr(module, attribute)
    if not isinstance(app, App):
        raise TypeError(f"{target} is not a bessemer.App")
    app.build()
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bessemer", description="Bessemer initial single-worker runtime"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="serve a module:app using one worker")
    run.add_argument("app")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--max-body-size", type=int)
    run.add_argument(
        "--max-in-flight", "--backpressure", dest="max_in_flight", type=int
    )
    run.add_argument("--request-timeout", type=float)
    run.add_argument("--graceful-timeout", type=float)
    routes = commands.add_parser(
        "routes", help="show compiled routes and current execution tiers"
    )
    routes.add_argument("app")
    args = parser.parse_args(argv)
    try:
        app = load_app(args.app)
        if args.command == "routes":
            for route in app._export_routes():
                print(
                    f"{route['method']:7} {route['path']:32} Tier {route['tier']}: {route['reason']}"
                )
        else:
            app.run(**_settings(args))
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- CLI boundary reports imported application failures.
        if isinstance(error, KeyboardInterrupt):
            return 130
        print(f"bessemer: {error}", file=sys.stderr)
        return 1
    return 0
