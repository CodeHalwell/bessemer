"""Apply Linux affinity before the reference server re-execs its workers."""

import argparse
import inspect
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpus", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--loops", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--event-loop", required=True)
    args = parser.parse_args()
    if hasattr(os, "sched_getaffinity"):
        available = sorted(os.sched_getaffinity(0))
        if len(available) < args.cpus:
            raise RuntimeError("requested CPUs exceed the launcher's affinity")
        os.sched_setaffinity(0, available[: args.cpus])
    if os.environ["MULTICORE_FRAMEWORK"] == "fastapi":
        import uvicorn

        uvicorn.run(
            "app:app",
            host="127.0.0.1",
            port=args.port,
            workers=args.workers,
            access_log=False,
            log_level="warning",
        )
        return 0
    from bessemer import App
    from bessemer.cli import main as serve

    command = [
        "run",
        "app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--workers",
        str(args.workers),
        "--loops",
        str(args.loops),
        "--event-loop",
        args.event_loop,
        "--max-in-flight",
        "16384",
        "--max-connections",
        "16384",
    ]
    if "sync_workers" in inspect.signature(App.run).parameters:
        command += ["--sync-workers", str(max(1, args.cpus // args.workers))]
    return serve(command)


if __name__ == "__main__":
    raise SystemExit(main())
