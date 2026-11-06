# Timer diagnostic and optimisation candidates

Measured on 6 November 2026 against the same installed PyPI wheel and Python
environment as the comparison. Each measurement contains 40 sequential sleeps.
These are isolated timer measurements, not throughput improvement estimates.

| Requested sleep | Served default median | Served Rust loop median |
| --- | ---: | ---: |
| 100 µs | 0.164 ms | 2.297 ms |
| 1 ms | 1.223 ms | 2.290 ms |
| 5 ms | 5.715 ms | 6.795 ms |

The standalone Rust loop's median for a 1 ms sleep was 1.227 ms. The additional
delay appears in its embedded driver. The scheduler rounds up to whole
milliseconds (`crates/bessemer-loop/src/core.rs:502–505`), then the driver
uses a Tokio timeout in milliseconds (`driver.rs:124–127`). Preserve the timer
deadline through scheduling and investigate a finer-resolution wake mechanism.
Changing to Tokio `sleep_until` alone does not remove its timer granularity.

Other candidates found in the current source, with no measured improvement yet:

- Precompile escaped response-model keys rather than escape fixed field names
  for each response (`crates/bessemer-codec/src/model.rs:617–623`).
- Transfer response-buffer ownership where possible instead of copying codec
  scratch output (`src/invoker.rs:426,432`) or Python bytes (`src/lib.rs:703`).
- Carry parsed bodies into fallback validation rather than repeating parsing
  (`src/invoker.rs:391–404,1083–1085`, `src/bridge.rs:477–487`).

## Reproduce

Run from `/tmp`, using the installed wheel rather than the checkout:

```sh
/Users/danielhalwell/Library/Caches/bessemer-bench/pypi-1.0.0rc1/venv/bin/python - <<'PY'
import asyncio, statistics, time
from bessemer import App
from bessemer.testclient import TestClient
from bessemer._loop import new_event_loop

async def measure(delay, n=40):
    values = []
    for _ in range(n):
        started = time.perf_counter()
        await asyncio.sleep(delay)
        values.append((time.perf_counter() - started) * 1000)
    return {
        "median_ms": round(statistics.median(values), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }

for name, factory in [
    ("asyncio", asyncio.new_event_loop),
    ("bessemer-standalone", new_event_loop),
]:
    loop = factory()
    try:
        for delay in (0.0001, 0.001, 0.005):
            print(name, delay, loop.run_until_complete(measure(delay)))
    finally:
        loop.close()

for name in ("asyncio", "bessemer"):
    app = App()

    @app.get("/sleep")
    async def sleep_route(delay: float = 0.001):
        return await measure(delay)

    with TestClient(app, server_options={"event_loop": name}) as client:
        for delay in (0.0001, 0.001, 0.005):
            print(
                name + "-served",
                delay,
                client.get(f"/sleep?delay={delay}").json(),
            )
PY
```

## Captured output

```text
asyncio 0.0001 {'median_ms': 0.179, 'min_ms': 0.14, 'max_ms': 0.285}
asyncio 0.001 {'median_ms': 1.188, 'min_ms': 1.075, 'max_ms': 1.235}
asyncio 0.005 {'median_ms': 5.719, 'min_ms': 5.143, 'max_ms': 5.883}
bessemer-standalone 0.0001 {'median_ms': 1.179, 'min_ms': 0.204, 'max_ms': 1.364}
bessemer-standalone 0.001 {'median_ms': 1.227, 'min_ms': 1.084, 'max_ms': 1.403}
bessemer-standalone 0.005 {'median_ms': 5.695, 'min_ms': 5.105, 'max_ms': 6.838}
asyncio-served 0.0001 {'max_ms': 0.387, 'median_ms': 0.164, 'min_ms': 0.139}
asyncio-served 0.001 {'max_ms': 1.402, 'median_ms': 1.223, 'min_ms': 1.171}
asyncio-served 0.005 {'max_ms': 5.926, 'median_ms': 5.715, 'min_ms': 5.418}
bessemer-served 0.0001 {'max_ms': 2.364, 'median_ms': 2.297, 'min_ms': 2.082}
bessemer-served 0.001 {'max_ms': 2.312, 'median_ms': 2.29, 'min_ms': 2.05}
bessemer-served 0.005 {'max_ms': 8.354, 'median_ms': 6.795, 'min_ms': 5.714}
```
