# bessemer-loop: a Rust asyncio event loop

This began as the Phase 3/4 spike from `docs/performance-design.md`. It is an
asyncio event loop with its scheduler and reactor written in Rust (pyo3). It
runs on a single-threaded epoll reactor, in the manner of uvloop. The crate is
built into the server's extension module, and `bessemer._loop.Loop` is the
loop. Workers run on it only when the internal `event_loop="bessemer"` option
reaches `App._run` (the test client's `server_options` passes it); there is no
public setting yet, because on its own it does not lower CPU per request (see
the results in `docs/performance-design.md`). The standard loop stays the
default.

## Design

`bessemer._loop.Loop` inherits from two classes. The first is the Rust base
class `LoopCore`. The second is asyncio's Unix selector loop.

`LoopCore` (Rust) takes over the parts of the loop that run on every callback:

- `call_soon`, `call_soon_threadsafe`, `call_later` and `call_at`. These
  return Rust `Handle` objects. A timer heap with lazy deletion compacts
  itself the same way asyncio's does.
- `run_forever` and `_run_once`. The ready queue is drained in one pass, and
  callbacks run in their `contextvars.Context` through `PyContext_Enter` and
  `PyContext_Exit`.
- `_add_reader`, `_add_writer`, `_remove_reader` and `_remove_writer`, which
  call epoll directly. The selector's event semantics are kept: anything other
  than `EPOLLOUT` wakes the reader, and anything other than `EPOLLIN` wakes
  the writer.
- Cross-thread wake-ups. `call_soon_threadsafe` writes to the self-pipe only
  when the loop is blocked in `epoll_wait`.
- `sock_recv` and `sock_sendall`. These try the syscall first. Only when the
  socket would block do they fall back to a future and a reader or writer.
  They are still coroutines, so `create_task(loop.sock_recv(...))` works.
- `time`, `get_debug`, `create_future`, and `create_task` when no task factory
  is set.

asyncio's own code provides everything else, and it now runs on the Rust
primitives: servers, transports, streams, subprocesses, signal handlers,
`run_in_executor`, the exception handler, async generator hooks and shutdown.
Its transports already call `loop._add_reader`, so they use the Rust reactor
without any changes. Futures and tasks are CPython's C-accelerated
`asyncio.Future` and `asyncio.Task`, the same as uvloop uses.

The Rust state lives behind a `Mutex`, and the lock is never held across a call
into Python. Handles and the loop implement `__traverse__` and `__clear__`, so
the cycle collector can collect reference cycles that run through callbacks.

## Results (2026-11-05, Python 3.13.18, 4 vCPUs, not pinned)

Full tables are in `benchmarks/results/2026-11-05/rust-event-loop/`.

### Compatibility: CPython's asyncio tests

The harness is in `compat/`. It runs 24 modules of CPython's own
`test_asyncio`, 2016 tests in total, against the chosen loop.

| loop | pass | fail + error | skip | tests that ran on the loop |
|---|---:|---:|---:|---:|
| asyncio (stdlib) | 2011 | 0 | 5 | 1062 |
| **bessemer** | **2011** | **0** | 5 | 1062 |
| uvloop 0.23 | 1397 | 614 (+2 hangs) | 3 | 1059 |

About half of the tests (most of `test_futures` and `test_tasks`) run on
CPython's fake test loop, so they pass on any loop. The last column counts the
tests that actually created the loop under test. uvloop's failures mostly
come from the test suite reaching into stdlib internals that uvloop does not
have, such as `_default_executor`, child watchers and eager task factory
arguments. They are not failures of uvloop in real use.

### Throughput (higher is better, relative to the stdlib loop)

The harness is in `bench/`. Each figure is the median of 5 runs, measured as
CPU and wall time.

| benchmark | uvloop | bessemer |
|---|---:|---:|
| call_soon chain | 2.56x | **3.37x** |
| call_soon batch of 1000 | **3.01x** | 2.69x |
| call_soon_threadsafe chain | 2.97x | **9.17x** |
| timers: schedule, cancel half, fire | **6.27x** | 4.61x |
| timers: far-future, all cancelled | 0.77x | **2.11x** |
| future resolved by call_soon | 2.44x | **2.78x** |
| create_task + await | 2.14x | **2.37x** |
| task ping-pong (switches) | 2.17x | **2.34x** |
| sock_recv/sock_sendall echo, 64 B | 1.47x | **2.31x** |
| sock_recv/sock_sendall echo, 16 KiB | 1.45x | **1.93x** |
| streams echo, 64 B | **1.62x** | 1.42x |
| Protocol echo, 64 B | **1.75x** | 1.48x |
| run_in_executor, 32 concurrent | **1.63x** | 1.58x |
| geometric mean, all 23 cases | 2.04x | **2.24x** |

## Reading the results

The scheduler work is done. On the paths Phase 4 would put on the request
path (callbacks, task switches, future resolution and raw socket I/O),
bessemer is at uvloop's level or ahead of it. It also passes CPython's asyncio
suite as well as the stdlib loop does.

The remaining gap with uvloop is in transports. The streams and Protocol
echoes are about 15% behind because those still use asyncio's Python
`_SelectorSocketTransport`, while uvloop's transports are compiled. That gap
does not matter for Bessemer. Phase 4 has the server hand requests to
handlers straight from the Rust I/O thread, so Bessemer's own traffic never
goes through an asyncio transport. Only third-party clients that handlers use
(database drivers, HTTP clients) would. Moving the socket transport to Rust is
the next step if that turns out to matter.

My recommendation is to go ahead with Phase 4 on this design, once Phases 1
and 2 have landed.

## Known gaps

- Linux only, because it uses epoll directly. macOS would need kqueue.
- `Handle` is not a subclass of `asyncio.Handle`. A native type cannot inherit
  from it, and uvloop has the same limitation. Code that checks
  `isinstance(h, asyncio.TimerHandle)` will see the difference.
- Debug mode records source tracebacks and checks callbacks and threads. It
  does not log slow callbacks.
- `sock_recv_into`, `sock_accept`, `sock_connect` and `sock_sendto` use
  asyncio's implementations, on the Rust reactor.
- Signals still wake the loop through asyncio's self-pipe (`set_wakeup_fd`).
- Not yet built or tested on free-threaded CPython.

## Building and running

Build and install the `bessemer` wheel, then:

```
python -m unittest tests/python/test_event_loop.py           # behaviour tests
python crates/bessemer-loop/compat/run_compat.py --loop bessemer   # CPython asyncio suite (downloads it)
python crates/bessemer-loop/bench/run_all.py                 # benchmarks against asyncio and uvloop
```

Outside the server: `bessemer._loop.install()`, or
`asyncio.Runner(loop_factory=bessemer._loop.new_event_loop)`.
