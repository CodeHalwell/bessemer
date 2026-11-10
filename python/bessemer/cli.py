"""Application commands and the isolated worker entry point."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from pathlib import Path

from .application import App
from .config import parse_max_in_flight
from .config import settings as _settings


def _configure_child(parent_pid: int) -> None:
    if parent_pid < 1:
        raise ValueError("supervised worker requires a valid parent pid")
    if sys.platform == "linux":
        from ._native import _set_parent_death_signal

        _set_parent_death_signal(parent_pid)
    # A Windows virtual environment's python.exe is a launcher that starts
    # the real interpreter as its child, so the parent there is the launcher
    # and never the supervisor. Windows workers are tied to the supervisor by
    # their job object and by the start gate on stdin instead.
    if sys.platform != "win32" and os.getppid() != parent_pid:
        raise RuntimeError("supervisor exited before worker startup")


def load_app(target: str) -> App:
    module_name, attribute = _target_parts(target)
    directory = str(Path.cwd())
    if directory not in sys.path:
        sys.path.insert(0, directory)
    # Set by app.run(workers=...) for a script: its directory comes first, so a
    # same-named module in the working directory cannot shadow it.
    script_directory = os.environ.get("BESSEMER_IMPORT_PATH")
    if script_directory:
        sys.path.insert(0, script_directory)
    module = importlib.import_module(module_name)
    app = getattr(module, attribute)
    if not isinstance(app, App):
        raise TypeError(f"{target} is not a bessemer.App")
    app.build()
    return app


def _target_parts(target: str) -> tuple[str, str]:
    module_name, separator, attribute = target.partition(":")
    if (
        not separator
        or not attribute.isidentifier()
        or not all(part.isidentifier() for part in module_name.split("."))
    ):
        raise ValueError("application must have the form module:app")
    return module_name, attribute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bessemer", description="Bessemer application runtime"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="serve an application using supervised worker processes"
    )
    run.add_argument("app")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument(
        "--workers", type=int, help="worker processes; 0 selects the cgroup CPU quota"
    )
    run.add_argument(
        "--sync-workers",
        type=int,
        help="sync handler threads per process; 0 selects 8–16 adaptive threads "
        "on Linux/macOS GIL builds with a process CPU clock, 8 on other GIL "
        "builds, or the process CPU share on free-threaded builds (default 0)",
    )
    run.add_argument(
        "--loops",
        type=int,
        help="asyncio event loops per worker process (default 1); on free-threaded "
        "Python they run handlers in parallel",
    )
    run.add_argument(
        "--event-loop",
        choices=("auto", "asyncio", "bessemer"),
        help="the workers' event loop: 'auto' (default) runs 'bessemer', a loop "
        "written in Rust that serves connections on its own thread, on Linux and "
        "macOS with --loops 1, and asyncio otherwise; 'asyncio' always runs "
        "asyncio; 'bessemer' asks for the Rust loop (falls back to asyncio with a "
        "warning where it is unavailable)",
    )
    run.add_argument(
        "--io-backend",
        choices=("epoll", "io_uring"),
        help="socket I/O for a worker serving connections on the 'bessemer' loop "
        "(including 'auto' resolving to it) with one loop: 'epoll' (default) or 'io_uring' (Linux 6.1+; falls back "
        "to epoll with a warning when unavailable)",
    )
    run.add_argument("--max-body-size", type=int)
    run.add_argument("--max-request-bytes-in-flight", type=int)
    run.add_argument(
        "--max-upload-size",
        type=int,
        help="spool multipart bodies up to this size to disk for File() routes "
        "(default 0, off)",
    )
    run.add_argument(
        "--max-upload-bytes-in-flight",
        type=int,
        help="disk bytes all spooled uploads may hold at once (default 4 GiB)",
    )
    run.add_argument(
        "--upload-dir",
        help="directory for upload spool files (default: the system temp directory)",
    )
    run.add_argument(
        "--max-in-flight",
        "--backpressure",
        dest="max_in_flight",
        type=parse_max_in_flight,
        help="auto (default) or a fixed maximum of admitted requests per worker",
    )
    run.add_argument("--max-response-size", type=int)
    run.add_argument("--max-response-bytes-in-flight", type=int)
    run.add_argument("--max-connections", type=int)
    run.add_argument("--max-headers", type=int)
    run.add_argument("--max-header-size", type=int)
    run.add_argument("--max-request-target-size", type=int)
    run.add_argument("--request-timeout", type=float)
    run.add_argument("--header-timeout", type=float)
    run.add_argument("--graceful-timeout", type=float)
    run.add_argument("--startup-timeout", type=float)
    run.add_argument(
        "--worker-timeout",
        type=float,
        help="replace workers whose event loop heartbeat stalls; 0 disables",
    )
    run.add_argument("--tls-cert", help="PEM certificate chain for native TLS")
    run.add_argument("--tls-key", help="PEM private key for --tls-cert")
    run.add_argument(
        "--tls-client-ca",
        help="PEM CA bundle that client certificates must chain to (mutual TLS)",
    )
    run.add_argument(
        "--tls-client-crl",
        help="PEM certificate revocation lists for --tls-client-ca; revoked "
        "client certificates, and ones whose issuer has no current CRL, are refused",
    )
    run.add_argument(
        "--tls-ocsp-response",
        help="DER OCSP response for --tls-cert, stapled to every handshake and "
        "reloaded with the other TLS files",
    )
    run.add_argument(
        "--tls-client-auth",
        choices=("required", "optional"),
        help="With --tls-client-ca: refuse clients without a certificate "
        "(required, default) or verify one only when presented (optional)",
    )
    run.add_argument(
        "--tls-reload-interval",
        type=float,
        help="Seconds between checks of the TLS files for changes (default 60; 0 disables)",
    )
    run.add_argument(
        "--tls-min",
        choices=("1.2", "1.3"),
        help="Lowest TLS version accepted (default 1.3)",
    )
    run.add_argument(
        "--http",
        choices=("auto", "1.1"),
        help="auto serves HTTP/1.1 and HTTP/2 (default); 1.1 serves HTTP/1.1 only",
    )
    run.add_argument(
        "--websocket-compression",
        choices=("none", "deflate"),
        help="deflate accepts permessage-deflate from WebSocket clients that offer it "
        "(default none)",
    )
    run.add_argument("--max-requests", type=int)
    run.add_argument("--max-lifetime", type=float)
    run.add_argument(
        "--max-rss", type=int, help="recycle workers above this RSS in MiB; 0 disables"
    )
    run.add_argument("--restart-limit", type=int)
    run.add_argument("--restart-backoff", type=float)
    routes = commands.add_parser(
        "routes", help="show compiled routes and execution tiers"
    )
    routes.add_argument("app")
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("app")
    worker.add_argument("--listener-fd", type=int, required=True)
    worker.add_argument("--ready-fd", type=int, required=True)
    worker.add_argument("--worker-count", type=int, required=True)
    worker.add_argument("--parent-pid", type=int, required=True)
    worker.add_argument("--settings", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            _target_parts(args.app)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
            from .supervisor import run as supervise

            return supervise(args.app, _settings(args))
        if args.command == "_worker":
            if sys.platform == "win32" and sys.stdin.buffer.read(1) != b"1":
                # The supervisor sends this once the worker is in its job
                # object; end of file means it died first.
                return 1
            _configure_child(args.parent_pid)
            if sys.platform == "win32":
                import msvcrt

                # The supervisor passes OS handles: the listening SOCKET,
                # which the native server takes as it is, and the control
                # pipe's HANDLE, which the worker writes through a C runtime
                # descriptor.
                os.set_handle_inheritable(args.listener_fd, False)
                os.set_handle_inheritable(args.ready_fd, False)
                args.ready_fd = msvcrt.open_osfhandle(args.ready_fd, os.O_WRONLY)
            else:
                os.set_inheritable(args.listener_fd, False)
                os.set_inheritable(args.ready_fd, False)
        app = load_app(args.app)
        if args.command == "routes":
            for route in app._export_routes():
                print(
                    f"{route['method']:7} {route['path']:32} Tier {route['tier']}: {route['reason']}"
                )
        else:
            settings = json.loads(args.settings)
            if not isinstance(settings, dict):
                raise ValueError("worker settings must be an object")
            if args.worker_count > 1 and app._export_settings().get("metrics_path"):
                raise ValueError(
                    "process-local metrics require --workers 1 until multi-process aggregation is supported"
                )
            app._worker_processes = args.worker_count
            app.run(**settings, listener_fd=args.listener_fd, ready_fd=args.ready_fd)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- CLI reports application failures.
        if isinstance(error, KeyboardInterrupt):
            return 130
        print(f"bessemer: {error}", file=sys.stderr)
        if args.command == "_worker" and isinstance(
            error, (ValueError, TypeError, ImportError, AttributeError)
        ):
            return 78  # EX_CONFIG: retrying the same invalid application will not help.
        return 1
    return 0
