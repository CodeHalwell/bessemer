# Bessemer

Bessemer is a Rust HTTP runtime with a Python application API. This checkout
contains an initial single-worker server and the original M0 performance spike.
It is alpha software; the full production specification and performance targets
have not yet been met.

## Run an application

Build requirements: CPython 3.12 or 3.13 and Rust 1.92 or newer. From this repository:

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
and application lifespan. Sync handlers use a bounded thread pool. JSON object
and array body parameters and optional Pydantic v2 models are supported.

```sh
bessemer routes examples.basic:app
bessemer run examples.basic:app --max-body-size 1048576 --max-in-flight 128 \
  --request-timeout 30 --graceful-timeout 30
```

Supported settings resolve from CLI flags, then `BESSEMER_*` environment variables,
then `[tool.bessemer]` in `pyproject.toml`, then defaults. See
[production readiness](docs/production-readiness.md) for the supported surface,
operational limits and remaining work. The
[original specification](docs/technical-specification.md) is preserved in the
repository so it does not depend on a conversation remaining available.

## Container and checks

```sh
docker build -t bessemer .
docker run --rm -p 8000:8000 bessemer
```

The example container runs as a non-root user and checks `/healthz`. Replace
`examples.basic:app` with your application when building your deployment image.
Use an ingress proxy for TLS and connection/header timeouts. The current process
has one asyncio worker; a process supervisor must enforce a hard exit deadline
for non-cooperative Python code.

After installing the wheel, run:

```sh
BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh
```

This runs Rust formatting, Clippy, Rust tests, Python API and real HTTP tests,
benchmark harness tests and shell syntax checks. CI builds and tests installed
wheels on Linux with CPython 3.12 and 3.13.

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
