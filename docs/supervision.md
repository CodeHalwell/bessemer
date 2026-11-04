# Process supervision

Use the CLI for deployments that need worker recovery and a hard shutdown
deadline. `App.run()` serves in the calling process; it does not create a
supervisor.

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

`--max-requests` counts handled requests per worker, including native health and
error responses. On reaching the threshold, that worker drains and exits normally,
then the parent replaces it. Normal request, lifetime, RSS, and reload recycling
does not consume the unexpected-failure budget. A single-worker service can have
a brief serving gap during request-count recycling.

Resource settings such as `--max-in-flight` and `--max-connections` apply to each
worker. Budget memory for replacement overlap. RSS recycling samples the worker
process through Linux `/proc`; it is not a hard memory limit and does not include
application child processes. Use a container or service-manager memory limit for
a hard cap.

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
