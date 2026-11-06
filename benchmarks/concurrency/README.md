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
