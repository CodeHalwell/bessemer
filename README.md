# Bessemer M0 performance spike

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
