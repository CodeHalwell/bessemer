# Process supervision

Use the CLI for deployments that need worker recovery and a hard shutdown
deadline. `App.run()` serves in the calling process by default; with
`App.run(workers=...)` it starts this same supervisor (see
[from a script](#from-a-script) below).

```sh
bessemer run service:app \
  --host 0.0.0.0 --port 8000 \
  --workers 2 \
  --startup-timeout 30 --worker-timeout 30 --graceful-timeout 20
```

Linux with Python 3.12 is the qualified process-supervision target. The CLI
requires POSIX descriptor inheritance and process groups; Windows fails with an
explicit unsupported-platform error. Other POSIX platforms do not have the
Linux parent-death and descendant-cleanup guarantees described below.

## From a script

`app.run(workers=N)` runs the supervisor from Python, for a script started with
`python service.py`:

```python
from bessemer import App

app = App()

if __name__ == "__main__":
    app.run(host="0.0.0.0", workers=0)  # one worker per usable CPU
```

`workers` is 1 by default, which serves from the calling process with no
supervisor, as before. 0 selects one worker per usable CPU, like
`bessemer run`, and 2 to 256 start that many. The default stays at one so a
development script keeps one process, a debugger attached to it sees the
requests, and objects created before `app.run()` are the ones handlers use.

Each worker is a fresh interpreter, so it imports the application again by
module and variable name rather than receiving the object. That means:

- The application must be a module-level variable. One built inside a function
  is refused with an error naming the fix.
- The `app.run()` call must sit under `if __name__ == "__main__":`. Each
  worker imports your script; an unguarded call there fails with an error
  that says so, instead of starting servers recursively.
- Anything the script does at import time runs again in each worker, and
  state set up in the parent after import is not seen by them. Do per-worker
  setup in the lifespan.
- The script's directory is put on the workers' `PYTHONPATH`, so it works from
  any working directory. `python -m package.module` is imported by its module
  name.

The other `App.run()` settings (limits, TLS, `loops`, `event_loop`,
`io_backend` and the rest) are passed to every worker, and are checked before
the first worker starts. The supervisor's own recovery settings, such as
`worker_timeout` and `max_rss`, keep their defaults here; use `bessemer run`
or `[tool.bessemer]` with the CLI when you need to change them. `app.run()`
does not read `BESSEMER_*` variables or `pyproject.toml`; its arguments are
the whole configuration. It returns after `SIGINT` or `SIGTERM` stops the
workers, and raises `RuntimeError` if the supervisor gives up on a worker.

## Worker startup and recovery

The supervisor binds one TCP listener before importing the application. Each
worker starts a fresh Python interpreter and inherits that listener. Application
imports, route compilation, lifespan startup, and Python threads run in workers.
The supervisor does not import the user application.

A worker becomes ready after lifespan startup and native listener initialization.
An event-loop heartbeat follows every half second. The startup deadline bounds
workers that never become ready; the heartbeat deadline detects a blocked Python
event loop. A failed or closed control channel also triggers recovery, even when
heartbeat monitoring is disabled.

Unexpected exits, lost control channels, and stalled heartbeats share a per-slot
restart budget and bounded exponential backoff. A worker must demonstrate 30
seconds of healthy heartbeat before its prior failure count can reset. Readiness
alone does not reset it. Exhausting the budget makes the supervisor exit with
status 1, so the deployment's service manager can apply its restart policy.
Application import, type, and validation errors reported as configuration failures
are not retried during initial startup.

The parent retains its listener across crashes and recycling. Connections queued
in the kernel can wait while a replacement starts. Recovery does not guarantee
that a request already executing in a failed worker succeeds; clients need an
appropriate retry policy for their operations.

## Reloading and recycling

Send `SIGHUP` to the supervisor PID to reload application code. It replaces workers
one slot at a time, waits for each replacement to become ready, then drains the
old worker. Reload requests arriving during an existing cycle are coalesced. A
replacement that fails validation or exhausts its restart budget leaves a healthy
old worker running and applies a cooldown. Configuration changes require a new
supervisor process; `SIGHUP` reloads application code using the existing settings.

Lifetime and RSS recycling also start a replacement before draining the old
worker. There are at most two tracked workers per configured slot: an active
worker plus its starting replacement or draining predecessor. Repeated reloads
or a very short lifetime do not accumulate draining processes.

`--max-requests` counts produced responses per worker, including native health
and error responses. The counter advances before a slow client has necessarily
received the response body. On reaching the threshold, the native listener
announces drain before waiting for its connections. The supervisor starts a
replacement and arms a deadline from the first drain notification. Native
connection draining and Python cleanup share this parent deadline; a healthy
heartbeat or `--worker-timeout 0` does not extend it. Repeated drain notifications
also do not extend it. A worker that cannot exit, including one blocked in Python
executor shutdown, is killed at the deadline. Normal recycling does not consume
the unexpected-failure budget. A single-worker service can have a brief serving
gap while a request-count replacement starts.

Resource settings such as `--max-in-flight` and `--max-connections` apply to each
worker. Budget memory for replacement overlap. RSS recycling samples the worker
process through Linux `/proc`; it is not a hard memory limit and does not include
application child processes. Use a container or service-manager memory limit for
a hard cap.

## Request deadlines and stuck work

A request timeout can return `504` and request cancellation. It does not forcibly
terminate Python code, interrupt a running Python thread, or guarantee that an
async handler or finalizer finishes cancellation. Admission remains held until
the actual work and cleanup finish. Configure timeouts on database/network
clients and keep application cleanup bounded and cooperative.

An async finalizer can keep yielding forever while the event loop and heartbeat
remain healthy. Running synchronous calls can also occupy every executor thread
indefinitely. If such work holds every admission slot, Python routes can return
`503` indefinitely while native health routes still return `200`. The heartbeat
watchdog detects event-loop stalls, not exhausted request capacity. The default
request-count, lifetime, and RSS recycling settings are all disabled (`0`), so
they provide no automatic recovery from this case. Even positive request-count
recycling needs its response threshold to be reached.

A positive `--max-lifetime` starts parent-initiated replacement even when a
worker's heartbeat stays healthy. Once the replacement is ready, the old worker
gets a hard drain deadline; failed replacements follow the rollback/restart
policy above. Manual `SIGHUP` or an external service restart provides the same
process boundary. Use
an application-level functional probe with an explicit restart policy when this
recovery is required; a native liveness probe or readiness failure alone does
not guarantee replacement. Isolate work that requires forced per-call
termination in an application-owned process with its own hard deadline.

Each worker has eight sync executor threads by default. For framework-dispatched
HTTP requests, queued and running work remains bounded by `--max-in-flight`;
queued cancellation does not release admission while its executor item remains
pending. This HTTP bound does not govern direct use of the internal `Worker`
class or tasks/threads created by application code.

## Shutdown and process ownership

`SIGTERM` starts shutdown; `SIGINT` does the same and returns status 130. The
supervisor signals every tracked worker process group, waits for the shared
graceful deadline, then sends `SIGKILL` to survivors. A second `SIGTERM` or
`SIGINT` shortens the current deadline immediately. Reaping has a further shared
one-second budget. Python handlers and lifespan cleanup that cannot finish by
the hard deadline are terminated.

On Linux, an exited worker's remaining process group is killed before its leader
is reaped. Keeping the leader's PID reserved until that step avoids signaling a
reused process-group identifier. Application subprocesses that remain in their
worker's group are covered by this cleanup. Applications must not take over the
supervisor's child reaping.

The supervisor waits for its tracked worker leaders; it is not a general init
for adopted application grandchildren. The supplied image runs pinned Tini as
PID 1 above Bessemer to reap those descendants. Custom containers that run apps
which spawn subprocesses need an equivalent init/subreaper. Process-group killing
terminates descendants, while init performs their final reaping; a stopped
container having no live processes alone does not prove this live-container
behavior. The container orphan regression verifies that a child disappears
completely after its worker crashes while init and the supervisor remain alive.

Linux workers also install a kernel parent-death signal before application import.
If the supervisor is killed, its workers receive `SIGKILL`, including a worker
whose Python event loop is blocked. This kernel setting covers workers themselves.
Detached application sessions and the complete descendant tree after abrupt
supervisor death require the deployment's container or cgroup lifecycle. For
systemd, use `KillMode=control-group`. Allow the deployment's termination window
to exceed the configured graceful timeout plus the one-second reap budget and a
scheduling margin.

## Configuration

Precedence is command line, then `BESSEMER_*` environment variables, then
`[tool.bessemer]` in the current directory's `pyproject.toml`, then defaults. Use
snake-case keys in TOML and uppercase names in the environment, for example
`max_requests` and `BESSEMER_MAX_REQUESTS`. Unknown TOML settings and invalid
values fail before the application is imported.

| CLI setting | Default | Meaning |
| --- | --- | --- |
| `--workers` | `0` | Automatic CPU quota/affinity count; explicit range 1–256. |
| `--startup-timeout` | `30` seconds | Deadline for each worker to become ready. |
| `--worker-timeout` | `30` seconds | Event-loop heartbeat deadline; `0` disables, positive values must be at least 1 second. |
| `--graceful-timeout` | `30` seconds | Shared hard shutdown deadline and individual drain deadline. |
| `--max-requests` | `0` | Handled requests before normal recycling; `0` disables. |
| `--max-lifetime` | `0` seconds | Worker age from process start before replacement; `0` disables. |
| `--max-rss` | `0` MiB | Sampled worker RSS recycling threshold on Linux; `0` disables. |
| `--restart-limit` | `5` | Retries per slot following repeated unexpected failures. |
| `--restart-backoff` | `0.1` seconds | Initial exponential restart delay. |
| `--tls-cert`, `--tls-key` | none | PEM certificate chain and private key for native TLS; both or neither. |
| `--tls-reload-interval` | `60` seconds | How often the TLS files are checked for changes; `0` disables reload. |
| `--tls-min` | `1.3` | Lowest TLS version accepted: `1.3` or `1.2`. |
| `--tls-client-ca` | none | PEM CA bundle for mutual TLS; needs `--tls-cert`. |
| `--tls-client-crl` | none | PEM revocation lists for `--tls-client-ca`; revoked clients, and clients whose issuer has no current CRL, are refused. Reloaded with the other TLS files. |
| `--tls-ocsp-response` | none | DER OCSP response for `--tls-cert`, stapled to every handshake; refresh the file and it is reloaded. |
| `--tls-client-auth` | `required` | With `--tls-client-ca`: `required` refuses clients without a certificate, `optional` verifies one only when presented. |
| `--http` | `auto` | `auto` serves HTTP/1.1 and HTTP/2; `1.1` serves HTTP/1.1 only. |
| `--websocket-compression` | `none` | `deflate` accepts `permessage-deflate` from WebSocket clients that offer it. |

Automatic worker count uses the tightest readable cgroup v1/v2 CPU quota and
process affinity, rounds a fractional quota up, and remains between 1 and 256.
Set an explicit worker count when deployment memory or workload behavior needs a
smaller pool.

The current Prometheus endpoint is process-local. An application configured with
`metrics_path` requires `--workers 1`; multi-worker startup is rejected until
aggregation is supported.

## Verification

[Supervisor tests](../tests/python/test_supervisor.py) exercise actual subprocess
crashes, readiness, restart limits, control-channel failure, bounded draining,
reloads, request-count recycling, heartbeat recovery, and hard shutdown through
the installed native CLI. Linux tests compile the small
[subreaper fixture](../tests/security/parent_death_harness.c) into a temporary
directory. The fixture proves parent-death signaling with a blocked worker loop,
checks the exact `SIGKILL` wait status, and reaps adopted descendants without
changing the main test process or leaving zombies.

See [production readiness](production-readiness.md) and
[qualification results](qualification-results.md) for the wider supported surface
and deployment qualification limits. These supervision checks do not establish
the project's formal performance gates.
