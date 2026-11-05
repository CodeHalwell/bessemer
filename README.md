# Bessemer

Bessemer is a Rust HTTP runtime with a Python application API. This checkout
contains the application runtime, process supervisor and the original M0 performance spike.
The 1.0 line freezes the public API on Linux x86_64 with CPython 3.12 and 3.13;
the full specification and formal performance targets have not yet been met.
See the [changelog](CHANGELOG.md) for what is covered.

## Run an application

Build requirements: CPython 3.12 or 3.13 and Rust 1.92 or newer. The supervised
CLI currently requires POSIX; the strongest process cleanup guarantees are Linux-specific.
From this repository:

```sh
python -m pip install .
bessemer run examples.basic:app --host 127.0.0.1 --port 8000
```

```python
import asyncio
from bessemer import App

app = App()
app.health("/healthz")

@app.get("/items/{item_id}")
async def read_item(item_id: int, include_deleted: bool = False):
    await asyncio.sleep(0.001)
    return {"id": item_id, "include_deleted": include_deleted}
```

The server compiles route declarations at startup. Rust handles HTTP routing,
scalar path/query parsing, JSON parsing and serialization, body limits, admission
control and request deadlines. A persistent Python asyncio worker runs handlers
and application lifespan. Sync handlers use a bounded thread pool. The API supports
compiled dependencies, constrained inputs, explicit responses, response models,
OpenAPI 3.1 and optional Pydantic 2.10.6+ validation. Native controls include
API-key authentication, CORS, host/proxy checks, rate limits, protected metrics
and redacted access logs.

```sh
bessemer routes examples.basic:app
bessemer run examples.basic:app --workers 2 --max-body-size 1048576 --max-in-flight 128 \
  --request-timeout 30 --graceful-timeout 30
```

Supported settings resolve from CLI flags, then `BESSEMER_*` environment variables,
then `[tool.bessemer]` in `pyproject.toml`, then defaults. See
[production readiness](docs/production-readiness.md) for the supported surface,
operational limits and remaining work. The
[original specification](docs/technical-specification.md) is preserved in the
repository so it does not depend on a conversation remaining available.

- [Application API and production example](docs/application-api.md)
- [Worker supervision and operational bounds](docs/supervision.md)
- [Native security controls, logs and metrics](docs/production-controls.md)
- [Executed qualification evidence](docs/qualification-results.md)
- [Changelog](CHANGELOG.md), [releasing](docs/releasing.md) and [security policy](SECURITY.md)

## Container and checks

```sh
docker build -t bessemer .
docker run --rm -p 127.0.0.1:8000:8000 bessemer
```

The example container runs as a non-root user and checks `/healthz`. Replace
`examples.basic:app` with your application when building your deployment image.
The supplied image uses Tini to forward termination signals and reap orphaned
application descendants. Bessemer supervises its worker processes beneath it.
See [container deployment](docs/deployment.md) for the authenticated example,
resource limits, read-only filesystem and recorded image qualification.
TLS can be terminated natively with `--tls-cert` and `--tls-key` (rustls, TLS 1.3
minimum unless `--tls-min 1.2`), or at an ingress proxy. HTTP/2 is served
alongside HTTP/1.1 by ALPN over TLS and by prior knowledge on plaintext. Use ingress for
deployment access controls. The native transport also bounds connections, header-read time, header count/bytes and request-target
size. The built-in supervisor starts fresh worker processes, monitors their
event-loop heartbeats, restarts failed workers and enforces a hard shutdown
deadline. Worker count defaults to the available cgroup CPU quota. Per-worker
rate limits are process-local; protected metrics currently require `--workers 1`.

For development, install the qualified tools and optional validator, then run:

```sh
python -m pip install '.[dev,pydantic]'
BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh
```

This runs Rust formatting, Clippy, Rust tests, Python API and real HTTP tests,
benchmark harness tests, Ruff and shell syntax checks. CI builds and tests installed
wheels on Linux with CPython 3.12/Pydantic 2.10.6 and CPython 3.13/current Pydantic v2.
`scripts/security-check.sh` audits the installed runtime and locked Rust graph
using separately installed pinned scanner tools.

## M0 performance spike

This repository contains the M0 throughput spike: compare an Axum JSON handler
that stays in Rust with an immediately completing Python async handler driven
by Rust through PyO3. Both use a single-threaded Tokio runtime, the same HTTP
stack, response shape and Rust JSON serialisation.

For the M0 comparison, build on the server machine and start both modes on the
same dedicated, pinned core. In this Linux example, replace `192.0.2.10` with
the server's interface address. The two commands need separate terminals.

```sh
cargo build --release
taskset -c 2 target/release/bessemer-m0 rust 192.0.2.10:8000
taskset -c 2 target/release/bessemer-m0 python 192.0.2.10:8001
```

On a separate load-generator machine with Python and `oha` 1.16 or later:

```sh
python3 benchmarks/measure_m0.py http://192.0.2.10:8000/ http://192.0.2.10:8001/
```

The comparison warms each endpoint for 30 seconds, then alternates five
60-second HTTP/1.1 keep-alive runs. It verifies the endpoint modes and
responses, rejects request failures and a spread over 3%, and reports the
median Python/Rust ratio. Exit status 1 means the ratio is below the 40% stop
threshold; status 2 means the measurement is invalid. The 60% Tier 1 target
is reported separately.

For a quick local check, the paired runner starts each server in turn:

```sh
DURATION=1s CONNECTIONS=25 ./benchmarks/run_m0.sh
```

Local results and shorter `--warmup`, `--duration` or `--runs` settings are
exploratory, not the acceptance gate. This spike excludes route compilation,
dependencies, and the rest of the planned request pipeline. It does not yet
measure the separate fixed-rate latency scenarios.
