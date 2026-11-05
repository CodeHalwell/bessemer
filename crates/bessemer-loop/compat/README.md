# asyncio compatibility harness

Runs a subset of CPython's own `Lib/test/test_asyncio` suite against an event
loop and reports pass/fail/error/skip per module.

```sh
cd crates/bessemer-loop/compat
python3 run_compat.py --loop bessemer --json bessemer.json --failures-out bessemer-failures.txt
python3 run_compat.py --loop asyncio   # the stdlib loop: checks the harness itself
python3 run_compat.py --loop uvloop    # a third-party reference point
```

`--loop` defaults to `$BESSEMER_COMPAT_LOOP`, then `bessemer`. Other options:
`--modules test_events test_tasks` to run some modules only, `--timeout`
(per test, default 60 s), `--module-timeout` (per worker, default 900 s),
`-j` (modules in parallel, default 4), `--workdir` (worker logs and scratch
files; the path is printed at start). The exit status is 1 if anything
failed, errored or crashed.

## Getting the tests

The interpreter has no `test` package, so `fetch_cpython_tests.py` downloads it
for the exact running version (tag `v{major}.{minor}.{micro}`) into
`.cpython/v{X.Y.Z}/` (gitignored). `run_compat.py` calls it automatically; run
it directly with `--force` to refetch.

It tries the codeload tarball of the tag first and falls back to
raw.githubusercontent.com file by file (some networks allow only the latter).
It keeps `Lib/test/__init__.py`, `Lib/test/support/**`,
`Lib/test/test_asyncio/**`, the `test.*` modules those import (found with
`ast`, nothing is executed), and the certificates and helper scripts the tests
name. In the raw fallback it cannot list directories, so it starts from the
module list in `SEED_MODULES`. Files are filtered by path, type and size and
written into a fresh directory that replaces the cache only once complete.

## How the chosen loop is installed

Each module runs in its own `python -I` worker that:

- sets the event loop policy to a subclass of the loop's policy class, so
  `asyncio.run`, `asyncio.Runner`, `IsolatedAsyncioTestCase` and
  `asyncio.new_event_loop()` all build the chosen loop. Tests call
  `asyncio.set_event_loop_policy(None)` in `tearDown`/`tearDownModule`; the
  worker maps that back to the chosen policy;
- replaces `asyncio.EventLoop` (3.13+) with a subclass of the chosen loop
  class, because tests such as `test_taskgroups` use it as a `loop_factory`;
- collapses the classes CPython repeats once per selector (`SelectEventLoopTests`,
  `PollEventLoopTests`, `EPollEventLoopTests`, ...: same bases, each defining
  `create_event_loop` or `new_loop`) into one class named
  `<Mixin>OnChosenLoop` whose hook returns the chosen loop. The stock variants
  are not run: they would only test the stdlib. A class that defines a hook on
  its own (e.g. `test_runners.BaseTest`, which builds a mock loop on purpose)
  is left alone. The report lists every replacement.

The loops are:

| `--loop`   | factory                        | policy base                        | expected class           |
|------------|--------------------------------|------------------------------------|--------------------------|
| `bessemer` | `bessemer._loop.new_event_loop` | `bessemer._loop.EventLoopPolicy`    | `bessemer._loop.Loop`     |
| `asyncio`  | `asyncio.SelectorEventLoop`    | `asyncio.DefaultEventLoopPolicy`   | `asyncio.SelectorEventLoop` |
| `uvloop`   | `uvloop.new_event_loop`        | `uvloop.EventLoopPolicy`           | `uvloop.Loop`            |

The worker checks that `asyncio.new_event_loop()` returns the expected class
and the report warns if it does not.

## The subset

Loop surface (rewired to the chosen loop): `test_events`, `test_sock_lowlevel`,
`test_sendfile`, `test_buffered_proto`, `test_server`.
Streams, TLS, subprocesses via the policy: `test_streams`, `test_ssl`,
`test_subprocess`.
Higher-level APIs: `test_tasks`, `test_eager_task_factory`, `test_futures`,
`test_futures2`, `test_locks`, `test_queues`, `test_taskgroups`,
`test_timeouts`, `test_waitfor`, `test_staggered`, `test_threads`,
`test_runners`, `test_context`, `test_pep492`.

Left out: `test_base_events`, `test_selector_events`, `test_unix_events`,
`test_proactor_events`, `test_windows_*` and similar, which unit-test stdlib
internals with mocks rather than a loop's behaviour.

## Reading the report

- `on-loop` counts tests that built at least one chosen loop (counted in the
  policy's `new_event_loop`, the rewired hooks and `asyncio.EventLoop`). Many
  tests never touch it: most of `test_futures`, much of `test_tasks` and
  `test_pep492` run on the fake `test_utils.TestLoop` or a bare
  `BaseEventLoop`, and `test_runners` uses mock loops. Those pass for any loop;
  judge the loop by the `on-loop` tests. The JSON has a per-test `loops` count.
- `crash` means the per-test watchdog (`faulthandler.dump_traceback_later`)
  fired or the worker died (e.g. a segfault). The test is recorded, and the
  module is restarted without the tests already done and the culprit. The
  traceback is in the worker log (`<workdir>/<module>.<attempt>.log`).
- `fixt-err` counts errors outside a test: `setUpClass`/`setUpModule`
  failures, an import failure of the module, or a worker that exited non-zero
  after its last test. Tests that never ran because of one are reported as
  `error` with reason `not run: ...`.
- Some tests check stdlib internals (`loop._selector`, `loop._ready`,
  `isinstance(h, asyncio.Handle)`, exact `repr`s). A loop with a different
  internal structure fails them; that is expected, and the failures list is for
  triage, not a target of 100 %.

`--failures-out FILE` writes one line per failing test:
`<module.Class.test>\t<fail|error|crash|fixture-error>\t<one-line reason>`.
`--json FILE` writes totals, per-module counts, the replacements made and
every test's outcome, reason, loop count and duration.

## Caveats

- Results depend on the CPython version: the suite is fetched for the running
  interpreter, so 3.12 and 3.13 runs are not directly comparable.
- `-j` runs modules in parallel. The tests bind to port 0, but if a timing
  test is flaky on a loaded machine, rerun that module alone with `--modules`.
