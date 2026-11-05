#!/usr/bin/env python3
"""Run a subset of CPython's asyncio test suite against an event loop.

    python3 run_compat.py --loop bessemer --json out.json --failures-out fails.txt

Each test module runs in its own worker subprocess (``python -I``) with the
chosen loop installed as the event loop policy, so ``asyncio.run``,
``asyncio.Runner``, ``IsolatedAsyncioTestCase`` and ``asyncio.new_event_loop``
all use it. Test classes that CPython repeats once per selector (they define
``create_event_loop`` or ``new_loop``) are collapsed into one class whose hook
returns the chosen loop.

A per-test watchdog (``faulthandler.dump_traceback_later``) kills a hung or
crashed worker; the runner records that test as ``crash`` and restarts the
module with the finished tests and the culprit excluded.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LOOPS = ("bessemer", "asyncio", "uvloop")
LOOP_ENV = "BESSEMER_COMPAT_LOOP"
PACKAGE = "test.test_asyncio"

# The subset, in report order. Modules that do not exist in the running
# CPython version are reported as missing and skipped.
MODULES = (
    # Loop surface: these rewire create_event_loop/new_loop to the chosen loop.
    "test_events",
    "test_sock_lowlevel",
    "test_sendfile",
    "test_buffered_proto",
    "test_server",
    # Streams, TLS and subprocesses on a real loop via the policy.
    "test_streams",
    "test_ssl",
    "test_subprocess",
    # Higher-level APIs; many of these run under asyncio.run, Runner or
    # IsolatedAsyncioTestCase, some on the fake TestLoop (see README).
    "test_tasks",
    "test_eager_task_factory",
    "test_futures",
    "test_futures2",
    "test_locks",
    "test_queues",
    "test_taskgroups",
    "test_timeouts",
    "test_waitfor",
    "test_staggered",
    "test_threads",
    "test_runners",
    "test_context",
    "test_pep492",
)

# Test-class methods that build the loop a test runs on.
LOOP_HOOKS = ("create_event_loop", "new_loop")

OUTCOMES = ("pass", "fail", "error", "skip", "crash")
MAX_RESTARTS = 25
WORKER_SETUP_FAILED = 3


# --------------------------------------------------------------------------
# Worker: runs one module inside a fresh interpreter.
# --------------------------------------------------------------------------


class LoopCounter:
    """Counts chosen-loop instantiations so tests that never touch it show."""

    def __init__(self) -> None:
        self.count = 0


def load_loop(name: str):
    """Return (loop factory, policy base class, expected loop class)."""
    import asyncio

    if name == "asyncio":
        return (
            asyncio.SelectorEventLoop,
            asyncio.DefaultEventLoopPolicy,
            asyncio.SelectorEventLoop,
        )
    if name == "uvloop":
        import uvloop

        return uvloop.new_event_loop, uvloop.EventLoopPolicy, uvloop.Loop
    if name == "bessemer":
        from bessemer import _loop as bessemer_loop

        return (
            bessemer_loop.new_event_loop,
            bessemer_loop.EventLoopPolicy,
            bessemer_loop.Loop,
        )
    raise ValueError(f"unknown loop {name!r}")


def install_loop(name: str, counter: LoopCounter):
    """Make the chosen loop the default everywhere; return a counting factory."""
    import asyncio
    from asyncio import events

    factory, policy_base, loop_class = load_loop(name)

    def make_loop():
        counter.count += 1
        return factory()

    class CompatPolicy(policy_base):
        def new_event_loop(self):
            counter.count += 1
            return super().new_event_loop()

    # Test modules reset the policy with set_event_loop_policy(None) in
    # tearDown/tearDownModule; map that back to a fresh chosen-loop policy.
    for owner in (asyncio, events):
        for attr in ("set_event_loop_policy", "_set_event_loop_policy"):
            original = getattr(owner, attr, None)
            if original is None:
                continue

            def patched(policy, _original=original):
                _original(CompatPolicy() if policy is None else policy)

            setattr(owner, attr, patched)

    # Python 3.13 tests also name the platform loop class directly, e.g.
    # IsolatedAsyncioTestCase.loop_factory = asyncio.EventLoop in
    # test_taskgroups. It must stay a class (it is used as a class attribute).
    if hasattr(asyncio, "EventLoop"):

        class EventLoop(loop_class):
            def __init__(self, *args, **kwargs):
                counter.count += 1
                super().__init__(*args, **kwargs)

        EventLoop.__module__ = loop_class.__module__
        asyncio.EventLoop = EventLoop

    asyncio.set_event_loop_policy(None)
    probe = asyncio.new_event_loop()
    try:
        probe_class = f"{type(probe).__module__}.{type(probe).__qualname__}"
        ok = isinstance(probe, loop_class)
    finally:
        probe.close()
    counter.count = 0
    return make_loop, probe_class, ok


def rewire_loop_classes(module, make_loop) -> list[str]:
    """Collapse per-selector test classes into one class on the chosen loop.

    CPython defines e.g. SelectEventLoopTests, PollEventLoopTests and
    EPollEventLoopTests with identical bases, differing only in the
    create_event_loop they define. Each group of two or more such classes is
    replaced by one subclass (of a non-skipped member) whose hook returns the
    chosen loop. Single classes defining a hook are left alone: there it is
    usually a deliberate fake (test_runners) or already uses the policy.
    """
    groups: dict[tuple, list[type]] = {}
    for name, obj in list(vars(module).items()):
        if (
            isinstance(obj, type)
            and issubclass(obj, unittest.TestCase)
            and obj.__module__ == module.__name__
            and any(hook in vars(obj) for hook in LOOP_HOOKS)
        ):
            groups.setdefault(obj.__bases__, []).append(obj)

    notes = []
    for bases, classes in groups.items():
        if len(classes) < 2:
            continue
        runnable = [c for c in classes if not getattr(c, "__unittest_skip__", False)]
        if not runnable:
            continue
        # Prefer the member that overrides least beyond its hook.
        template = min(
            runnable,
            key=lambda c: (
                len([k for k in vars(c) if not k.startswith("__")]),
                c.__name__,
            ),
        )
        hooks = {
            hook: (lambda self: make_loop())
            for hook in LOOP_HOOKS
            if hook in vars(template)
        }
        name = bases[0].__name__.removesuffix("Mixin") + "OnChosenLoop"
        generated = type(
            name,
            (template,),
            {**hooks, "__module__": module.__name__, "__qualname__": name},
        )
        for cls in classes:
            delattr(module, cls.__name__)
        setattr(module, name, generated)
        notes.append(
            f"{name} replaces {', '.join(sorted(c.__name__ for c in classes))}"
        )
    return notes


def one_line(err) -> str:
    exc_type, value, _ = err
    lines = traceback.format_exception_only(exc_type, value)
    text = " ".join(lines[-1].split()) if lines else exc_type.__name__
    return text[:300]


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


class EventResult(unittest.TestResult):
    """Writes one JSON line per event; re-arms the watchdog around each test."""

    def __init__(self, out, counter: LoopCounter, timeout: float) -> None:
        super().__init__()
        self.out = out
        self.counter = counter
        self.timeout = timeout
        self.current: dict | None = None

    def emit(self, **event) -> None:
        self.out.write(json.dumps(event) + "\n")
        self.out.flush()

    def arm(self) -> None:
        faulthandler.dump_traceback_later(self.timeout, exit=True)

    def startTest(self, test) -> None:
        super().startTest(test)
        self.arm()
        self.counter.count = 0
        self.current = {
            "id": test.id(),
            "outcome": "pass",
            "reason": "",
            "start": time.perf_counter(),
        }
        self.emit(event="start", id=test.id())

    def stopTest(self, test) -> None:
        super().stopTest(test)
        record, self.current = self.current, None
        if record is not None:
            self.emit(
                event="result",
                id=record["id"],
                outcome=record["outcome"],
                reason=record["reason"],
                loops=self.counter.count,
                seconds=round(time.perf_counter() - record["start"], 3),
            )
        # Keep the watchdog armed across class/module fixtures too.
        self.arm()

    def mark(self, test, outcome: str, reason: str) -> None:
        if self.current is not None and self.current["id"] == test.id():
            # Keep the first failure reason; errors outrank failures.
            if self.current["outcome"] in ("pass", "skip") or (
                outcome == "error" and self.current["outcome"] == "fail"
            ):
                self.current["outcome"] = outcome
                self.current["reason"] = reason
        else:
            # Class/module fixture errors arrive as _ErrorHolder pseudo-tests.
            self.emit(event="fixture", id=test.id(), outcome=outcome, reason=reason)

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self.mark(test, "error", one_line(err))

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.mark(test, "fail", one_line(err))

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self.mark(test, "skip", " ".join(str(reason).split())[:300])

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self.mark(test, "fail", "unexpected success")

    def addSubTest(self, test, subtest, err) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self.mark(
                test,
                "fail" if failed else "error",
                f"{subtest._subDescription()}: {one_line(err)}",
            )

    def escaped(self, test, err) -> None:
        """Record an exception raised by TestCase.run itself, after stopTest.

        IsolatedAsyncioTestCase closes its runner outside the result protocol,
        so a loop that fails there would otherwise kill the worker.
        """
        self.emit(
            event="result",
            id=test.id(),
            outcome="error",
            reason=f"raised outside the test: {one_line(err)}",
            loops=self.counter.count,
            seconds=None,
        )


def guard_runs(tests, result: EventResult) -> None:
    """Wrap each test class's run() so an escaping exception is a result."""
    for cls in {type(test) for test in tests}:
        original = cls.run

        def run(self, result_=None, _original=original):
            try:
                return _original(self, result_)
            except Exception:  # noqa: BLE001 - any escape is a result
                result.escaped(self, sys.exc_info())
                return None

        cls.run = run


def run_worker(args) -> int:
    # The fetched CPython tree must shadow any other "test" package.
    sys.path[:0] = [args.tests_root, *json.loads(args.sys_path)]
    counter = LoopCounter()
    timeout = args.timeout
    with open(args.events, "a", encoding="utf-8") as out:
        result = EventResult(out, counter, timeout)
        result.arm()
        try:
            make_loop, loop_class, ok = install_loop(args.loop, counter)
            result.emit(event="meta", loop_class=loop_class, loop_ok=ok)
            module = __import__(f"{PACKAGE}.{args.worker}", fromlist=["*"])
            notes = rewire_loop_classes(module, make_loop)
            result.emit(event="meta", rewired=notes)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        except Exception:  # noqa: BLE001 - any import failure is a result
            result.emit(
                event="fixture",
                id=args.worker,
                outcome="error",
                reason=one_line(sys.exc_info()),
            )
            return WORKER_SETUP_FAILED

        excluded = (
            set(json.loads(Path(args.exclude).read_text())) if args.exclude else set()
        )
        tests = list(iter_tests(suite))
        result.emit(event="plan", ids=[t.id() for t in tests])
        guard_runs(tests, result)
        unittest.TestSuite([t for t in tests if t.id() not in excluded]).run(result)
        faulthandler.cancel_dump_traceback_later()
    return 0


# --------------------------------------------------------------------------
# Parent: schedules workers, restarts after crashes, reports.
# --------------------------------------------------------------------------


def short_id(test_id: str) -> str:
    return test_id.removeprefix(PACKAGE + ".")


def run_module(module: str, args, tests_root: Path, workdir: Path) -> dict:
    """Run one module to completion, restarting the worker after crashes."""
    results: dict[str, dict] = {}
    fixtures: list[dict] = []
    meta: dict = {}
    plan: list[str] = []
    excluded: set[str] = set()
    parent_path = json.dumps([p for p in sys.path if p])

    for attempt in range(MAX_RESTARTS + 1):
        events_path = workdir / f"{module}.{attempt}.jsonl"
        log_path = workdir / f"{module}.{attempt}.log"
        exclude_path = workdir / f"{module}.{attempt}.exclude.json"
        exclude_path.write_text(json.dumps(sorted(excluded)))
        cwd = Path(tempfile.mkdtemp(prefix=f"{module}-", dir=workdir))
        cmd = [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--worker",
            module,
            "--loop",
            args.loop,
            "--tests-root",
            str(tests_root),
            "--sys-path",
            parent_path,
            "--events",
            str(events_path),
            "--exclude",
            str(exclude_path),
            "--timeout",
            str(args.timeout),
        ]
        with open(log_path, "w", encoding="utf-8") as log:
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    timeout=args.module_timeout,
                )
                returncode, wall_timeout = proc.returncode, False
            except subprocess.TimeoutExpired:
                returncode, wall_timeout = None, True

        running = None
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn final line from a killed worker
                kind = event.pop("event")
                if kind == "start":
                    running = event["id"]
                elif kind == "result":
                    results[event["id"]] = event
                    running = None
                elif kind == "fixture":
                    fixtures.append(event)
                elif kind == "plan":
                    plan = event["ids"]
                elif kind == "meta":
                    meta.update(event)

        if running is None:
            # WORKER_SETUP_FAILED means the worker already recorded why.
            if returncode not in (0, WORKER_SETUP_FAILED):
                reason = (
                    "module wall-clock timeout"
                    if wall_timeout
                    else f"worker exited with {returncode}"
                )
                fixtures.append({"id": module, "outcome": "error", "reason": reason})
            break

        if wall_timeout:
            reason = f"module wall-clock timeout ({args.module_timeout}s)"
        elif "Timeout (" in log_path.read_text(encoding="utf-8", errors="replace"):
            reason = f"timed out after {args.timeout}s"
        else:
            reason = f"worker died (exit {returncode})"
        results[running] = {
            "id": running,
            "outcome": "crash",
            "reason": reason,
            "loops": None,
            "seconds": None,
        }
        excluded = set(results)
    else:
        fixtures.append(
            {
                "id": module,
                "outcome": "error",
                "reason": f"gave up after {MAX_RESTARTS} restarts",
            }
        )

    fixture_reason = fixtures[-1]["reason"] if fixtures else "worker stopped"
    for test_id in plan:
        if test_id not in results:
            results[test_id] = {
                "id": test_id,
                "outcome": "error",
                "reason": f"not run: {fixture_reason}",
                "loops": None,
                "seconds": None,
            }
    return {
        "module": module,
        "meta": meta,
        "tests": list(results.values()),
        "fixtures": fixtures,
    }


def summarize(module_report: dict) -> dict:
    counts = dict.fromkeys(OUTCOMES, 0)
    on_loop = 0
    for test in module_report["tests"]:
        counts[test["outcome"]] += 1
        if test.get("loops"):
            on_loop += 1
    counts["total"] = len(module_report["tests"])
    counts["on_loop"] = on_loop
    counts["fixture_errors"] = len(module_report["fixtures"])
    return counts


def print_table(reports: list[dict], totals: dict) -> None:
    columns = ("total", *OUTCOMES, "on_loop", "fixture_errors")
    headers = (
        "module",
        "total",
        "pass",
        "fail",
        "error",
        "skip",
        "crash",
        "on-loop",
        "fixt-err",
    )
    rows = [(r["module"], *(r["summary"][c] for c in columns)) for r in reports]
    rows.append(("TOTAL", *(totals[c] for c in columns)))
    widths = [
        max(len(str(row[i])) for row in [headers, *rows]) for i in range(len(headers))
    ]

    def fmt(row) -> str:
        return "  ".join(
            str(cell).ljust(w) if i == 0 else str(cell).rjust(w)
            for i, (cell, w) in enumerate(zip(row, widths))
        )

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows[:-1]:
        print(fmt(row))
    print("  ".join("-" * w for w in widths))
    print(fmt(rows[-1]))
    ran = totals["total"] - totals["skip"]
    if ran:
        print(f"\npass rate (excluding skips): {totals['pass'] / ran:.1%} of {ran}")


def write_failures(path: Path, reports: list[dict]) -> None:
    lines = []
    for report in reports:
        for test in sorted(report["tests"], key=lambda t: t["id"]):
            if test["outcome"] in ("fail", "error", "crash"):
                lines.append(
                    f"{short_id(test['id'])}\t{test['outcome']}\t{test['reason']}"
                )
        for fixture in report["fixtures"]:
            lines.append(
                f"{short_id(fixture['id'])}\tfixture-{fixture['outcome']}\t{fixture['reason']}"
            )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def run_parent(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_cpython_tests import DEFAULT_CACHE, ensure_tests

    modules = tuple(args.modules) if args.modules else MODULES
    tests_root = ensure_tests(args.cache or DEFAULT_CACHE, modules, quiet=False)
    available = [
        m
        for m in modules
        if (tests_root / "test" / "test_asyncio" / f"{m}.py").is_file()
    ]
    missing = [m for m in modules if m not in available]

    workdir = Path(tempfile.mkdtemp(prefix=f"compat-{args.loop}-", dir=args.workdir))
    print(
        f"loop={args.loop} python={sys.version.split()[0]} tests={tests_root} logs={workdir}",
        file=sys.stderr,
    )
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(run_module, m, args, tests_root, workdir) for m in available
        ]
        reports = []
        for future in futures:
            report = future.result()
            report["summary"] = summarize(report)
            s = report["summary"]
            print(
                f"  {report['module']}: {s['pass']}/{s['total']} passed",
                file=sys.stderr,
            )
            reports.append(report)

    totals = dict.fromkeys(("total", *OUTCOMES, "on_loop", "fixture_errors"), 0)
    for report in reports:
        for key in totals:
            totals[key] += report["summary"][key]

    loop_classes = sorted({r["meta"].get("loop_class", "?") for r in reports})
    print()
    print(
        f"loop: {args.loop} ({', '.join(loop_classes)})  python: {sys.version.split()[0]}"
    )
    if any(r["meta"].get("loop_ok") is False for r in reports):
        print("WARNING: asyncio.new_event_loop() did not return the chosen loop class")
    for report in reports:
        for note in report["meta"].get("rewired", []):
            print(f"  {report['module']}: {note}")
    if missing:
        print(f"  not in this CPython version: {', '.join(missing)}")
    print()
    print_table(reports, totals)
    elapsed = time.monotonic() - started
    print(f"elapsed: {elapsed:.0f}s")

    if args.json:
        summary = {
            "loop": args.loop,
            "loop_class": loop_classes,
            "python": sys.version.split()[0],
            "elapsed_seconds": round(elapsed, 1),
            "totals": totals,
            "modules": {r["module"]: r["summary"] for r in reports},
            "missing_modules": missing,
            "rewired": {r["module"]: r["meta"].get("rewired", []) for r in reports},
            "tests": {
                short_id(t["id"]): {
                    k: t[k] for k in ("outcome", "reason", "loops", "seconds")
                }
                for r in reports
                for t in r["tests"]
            },
            "fixture_errors": [
                {**f, "id": short_id(f["id"])} for r in reports for f in r["fixtures"]
            ],
        }
        Path(args.json).write_text(
            json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.failures_out:
        write_failures(Path(args.failures_out), reports)

    bad = totals["fail"] + totals["error"] + totals["crash"] + totals["fixture_errors"]
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--loop",
        choices=LOOPS,
        default=os.environ.get(LOOP_ENV, "bessemer"),
        help=f"event loop to test (default: ${LOOP_ENV} or bessemer)",
    )
    parser.add_argument("--json", metavar="FILE", help="write a JSON summary")
    parser.add_argument(
        "--failures-out",
        metavar="FILE",
        help="write failing test ids with a one-line reason",
    )
    parser.add_argument(
        "--modules",
        nargs="+",
        metavar="MOD",
        help="test_asyncio modules to run (default: subset)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-test timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--module-timeout",
        type=float,
        default=900.0,
        help="per-worker wall clock limit (default: 900)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="modules run in parallel",
    )
    parser.add_argument(
        "--cache", type=Path, help="CPython test cache dir (default: compat/.cpython)"
    )
    parser.add_argument(
        "--workdir",
        help="where worker logs and scratch files go (default: system temp)",
    )
    # Internal: the worker side of a single module run.
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--tests-root", help=argparse.SUPPRESS)
    parser.add_argument("--sys-path", default="[]", help=argparse.SUPPRESS)
    parser.add_argument("--events", help=argparse.SUPPRESS)
    parser.add_argument("--exclude", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return run_worker(args) if args.worker else run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
