# Broad API concurrency comparison

This extends the existing `benchmarks/compare` workloads to exercise model
coercion/errors, 256 KiB byte responses, 16-way async fan-out, yield dependencies,
blocking handlers, async/sync streaming, SSE and duplex WebSocket sessions.
Equivalent Bessemer and FastAPI apps live in `app.py`; `workloads.py` verifies
HTTP bodies/statuses before load. WebSockets verify every echoed message.

Use separate installed environments for the published baseline and local wheel.
Both need the same Pydantic, FastAPI and Uvicorn versions. The client environment
also needs `websockets`; HTTP load uses `oha` 1.16.0. From the repository root:

```sh
CANDIDATE/bin/python benchmarks/concurrency/run.py \
  --baseline-python /absolute/path/to/BASELINE/bin/python \
  --candidate-python /absolute/path/to/CANDIDATE/bin/python \
  --oha /absolute/path/to/oha --rounds 3 --duration 2 --warmup 1 \
  --connections 64 --ws-connections 32 --out /tmp/bessemer-concurrency/raw.json
```

Servers run one worker. Framework order rotates each round, and the JSON output
is saved after every sample. Native module hashes distinguish same-version
baseline and candidate wheels. All errors and unexpected statuses fail the run;
the invalid-body workload expects 422. Select a shorter subset with `--routes
io,fanout,websocket` or `--variants rc1-rust,candidate-rust`.

Both Bessemer modes use `--max-in-flight 16384 --max-connections 16384`, as in
the reference comparison. Other bounds retain their defaults. The “default”
label means the asyncio event loop, not an unchanged server configuration.

The WebSocket client sends bursts of 16 messages while concurrently receiving
echoes, with compression disabled for every framework. HTTP throughput counts
responses per second; WebSocket throughput counts echoed messages per second.
The Python WebSocket client and same-host network may limit that measurement.

CPU use is estimated from `ps` process-tree CPU-time deltas, including supervised
worker processes. Compare CPU time per response alongside throughput: the
default Bessemer runtime uses multiple native I/O threads, while the Rust loop
and Uvicorn largely use one core. Runs are unpinned local diagnostics, not formal
performance or scaling acceptance. Keep builds/tests idle while measuring.

`timers.py` records 100 real sleeps per delay inside a served route on each
loop, using the installed native module. `../codec/model_bench.py` separately
measures model encode/parse costs, including escaped aliases.

`capacity.py` establishes connections through bounded health checks before
running I/O and fan-out. It verifies sustained connection capacity separately
from `oha`'s simultaneous connection-establishment burst:

```sh
CANDIDATE/bin/python benchmarks/concurrency/capacity.py \
  --python /absolute/path/to/CANDIDATE/bin/python --connections 1024 \
  --duration 2 --out /tmp/capacity/raw.json
```

## Worker connection balance

`worker_balance.py` starts the installed Bessemer wheel with its actual default
event loop and admission limits, then measures one short JSON run by default.
`--workload native` uses Bessemer's native health response at `/native-healthz`
to check the same transport without Python endpoint invocation. The native
control has no FastAPI equivalent in this harness. The tool records
each worker's CPU time and established keep-alive sockets, the load client's CPU
separately, checked response status/body lengths from oha's SQLite records,
and process cleanup. Install `psutil` in the driver environment; `lsof` is an
optional socket-inspection fallback.

```sh
DRIVER/bin/python benchmarks/concurrency/worker_balance.py \
  --python /absolute/path/to/INSTALLED/bin/python --oha /absolute/path/to/oha \
  --workers 4 --connections 256 --warmup 2 --duration 5 \
  --out /tmp/bessemer-worker-balance/run.json

DRIVER/bin/python benchmarks/concurrency/worker_balance.py \
  --python /absolute/path/to/INSTALLED/bin/python --oha /absolute/path/to/oha \
  --workload native --workers 4 --connections 256 --warmup 2 --duration 5 \
  --out /tmp/bessemer-worker-balance/native.json
```

The JSON and adjacent oha/SQLite/server-log files are retained; an existing
output path is refused. Socket counts are null with an error if inspection is
denied. An uneven connection split is evidence about that run, not proof of
the cause of an overall throughput plateau. The worker CPU estimate covers
approximately 10–90% of the load window; the client's CPU is separate. Repeat
each worker count and workload in balanced order. Check client CPU saturation
and host contention before attributing a throughput plateau to workers. This
local diagnostic does not enforce vCPU quotas or establish scaling efficiency.
Keep builds and other load runs idle while measuring.
