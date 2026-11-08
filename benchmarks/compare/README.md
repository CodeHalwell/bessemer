# Bessemer vs FastAPI vs Axum

Three apps with identical routes (`bessemer_app.py`, `fastapi_app.py` and
`axum_app/`) and a runner, `run_compare.py`. The routes are `GET /json`,
`GET /items/{id}?q=`, a 20-item Pydantic `POST /echo` and a 1 ms `GET /io`
sleep. Frameworks are interleaved within each round so drift hits all three.

Every sample records how busy the client and server CPUs were. A sample is
flagged `client_bound` when the load generator's CPUs were at least 85% busy,
because the number may then describe `oha` rather than the server.
`summarise.py results.json` prints Markdown tables and marks those ratios
with `*`.

## Same host

Quick and exploratory. Servers are pinned with `taskset` to the first N CPUs
and `oha` to CPU 3:

    python run_compare.py --python VENV/bin/python --oha PATH \
        --axum axum_app/target/release/axum-compare --out results.json

On a four-CPU host the single client core tops out at about 130k requests a
second, so multi-core results for Bessemer and Axum on the small routes are
not meaningful (see `results/2026-11-05/compare-fastapi-axum`).

## Two machines

The servers run on one host under `server_agent.py`, and `oha` runs on a
second host with several times as many cores. The agent is a small HTTP
service that starts one of the three fixed servers on request, pinned to the
first N cores, and reports CPU counters for those cores. It requires a
shared bearer token and should listen on a private address only.

`remote/run_two_machines.sh` does the whole run against any two Ubuntu 24.04
hosts you can reach over SSH:

    benchmarks/compare/remote/run_two_machines.sh user@server user@client \
        10.0.0.2 benchmarks/results/$(date -u +%F)/compare-two-machine \
        --configs 1-core,2-core,4-core,8-core --connections 256

It ships the current commit (`git archive HEAD`, so commit first), runs
`remote/bootstrap.sh` on both hosts in parallel (build tools, Rust, the
Bessemer wheel, FastAPI and uvicorn at the versions used in the same-host run,
the Axum release binary, and `oha`), starts the agent with a fresh random
token, runs the comparison from the client, and copies `raw.json` and
`summary.md` back.

### Google Cloud

`remote/gcp.sh` creates the two VMs, runs the above through `gcloud compute
ssh`, and deletes them:

    PROJECT=my-project benchmarks/compare/remote/gcp.sh all

- Server: `c3-standard-16` with SMT off, so 8 physical cores and configs up
  to `8-core` without two workers sharing a core.
- Client: `c3-highcpu-44`, about five times the server's cores.
- Both in `europe-west2-a` (override with `ZONE`) in one compact placement
  group, talking over the default VPC's internal network.
- About US$3 an hour on demand. A default run is three rounds of four
  configs, three frameworks and four routes at 13 seconds each, so about 30
  minutes plus 10-15 minutes of building.

`gcp.sh up`, `run` and `down` do the steps separately, for example to rerun
without rebuilding the VMs. `all` deletes the VMs even if the run fails.

### Why not GitHub Actions

Hosted runners cannot accept connections from one another, so a server job
and a client job cannot talk without a tunnel, and a single runner puts both
on the same small VM again. Runner hardware also varies between runs. A
self-hosted pair of runners would work, but needs the same two machines as
above.

## Reading the results

- Check the Server CPU column. If the server cores were not close to fully
  busy and the sample is not client-bound, something else (connections,
  network, the `/io` timer) is the limit; raise `--connections` or look
  closer before quoting a ratio.
- Treat `/io` as Bessemer vs FastAPI only. Tokio's millisecond timer rounds a
  1 ms sleep up, which caps Axum near 27k/s at 64 connections.

## FastAPI feature matrix

`feature_matrix_app.py` builds the same FastAPI-style app for `FW=bessemer` or
`FW=fastapi`. It covers async and sync JSON handlers, nested and yield
dependencies, headers, cookies, constrained query inputs, response models,
plain text, Request metadata, forms, HTTPException, BackgroundTasks, JSON/model
bodies, large lists and a 1 ms async sleep. `MW=1` (or `--middleware`) adds one
HTTP middleware that sets `x-mw`. This is the matrix used during the November
performance effort, now with caller-configurable paths and CPU placement.

Install the release Bessemer wheel before measuring it, and install the
comparison dependencies in each Python environment:

```sh
uv pip install --python VENV/bin/python fastapi uvicorn pydantic python-multipart
cargo build --release --manifest-path benchmarks/compare/axum_app/Cargo.toml
cargo install oha --locked
```

The runners require Linux, `taskset` (util-linux), Python and `oha`. The shell
entry points work from any directory. These examples assume eight
available CPUs; change the CPU lists to match the host/cgroup. Each feature
server runs on **one core**, with the load generator on separate cores:

```sh
benchmarks/compare/run_matrix.sh --python VENV/bin/python --oha "$HOME/.cargo/bin/oha" \
  --server-cpus 0 --client-cpus 4-6 --duration 5 --out matrix.jsonl
MW=1 benchmarks/compare/run_matrix.sh --python VENV/bin/python \
  --server-cpus 0 --client-cpus 4-6 --out middleware.jsonl
# A short selected run, or framework-specific venvs:
benchmarks/compare/run_matrix.sh --bessemer-python BESSEMER_VENV/bin/python \
  --fastapi-python FASTAPI_VENV/bin/python --frameworks bessemer fastapi \
  --routes GET:json GET:sync GET:bg POSTF:form POSTJ:item --out selected.jsonl
```

The default feature routes reproduce the original requests, including the
`session_id=abc` cookie, `a=2&d=7` query, `name=a&age=3` form and fixed JSON
bodies. `/raise` deliberately returns 404; its non-200 count is expected and its
HTTP-200 throughput is zero. These are throughput workloads, not a replacement
for compatibility assertions in the Python suite.

## Concurrency matrix

`run_concurrency.sh` uses the existing minimal compare apps, not the feature
app: Bessemer/FastAPI/Axum × 1/2/4 workers (Axum runtime threads) ×
64/256/1024 connections × `/json` and `/io`. Each N-worker server is pinned to
the first N CPUs in `--server-cpus`; the client uses `--client-cpus`.

```sh
benchmarks/compare/run_concurrency.sh --python VENV/bin/python \
  --axum benchmarks/compare/axum_app/target/release/axum-compare \
  --oha "$HOME/.cargo/bin/oha" --server-cpus 0-3 --client-cpus 4-6 \
  --duration 5 --out concurrency.jsonl
# Small smoke run:
benchmarks/compare/run_concurrency.sh --python VENV/bin/python \
  --frameworks bessemer fastapi --workers 1 --connections 64 \
  --routes json io --duration 1 --warmup 0 --out smoke.jsonl
```

Both runners append one JSON object per measured sample to `--out` and print
it to stdout. Fields include `fw`, `workers`, `route`, `conns`, `mw`, total
`rps`, HTTP-200-only `ok_rps`, `p50_ms`/`p99_ms` (null if unavailable), `non200`,
`errors`, status/error distributions and actual server/client CPU lists. Oha's
expected end-of-duration aborts are included in `errors` and identified as
`aborted due to deadline` in `error_distribution`. Failed
connects count in total throughput but never in OK throughput. Server output
is saved beside the JSON-lines file in per-framework/worker `.log` files;
startup failure stops the run, and signals/errors stop the server process
group. Results append across runs, while server logs are overwritten.

```sh
python benchmarks/compare/summarise_matrix.py matrix.jsonl middleware.jsonl \
  concurrency.jsonl > tables.md
```

The summariser groups by workload, middleware, worker count and connections;
it reports median rates/latencies and summed status/error counts. Keep runs
from different hosts/settings in separate files when producing tables. The
original `summarise.py` still reads the original `run_compare.py` JSON format.

Run either shell entry point with `--help` for all flags. CPU lists default to
the current process's affinity (first N for servers, the remaining CPUs for
the client); the full concurrency grid needs at least five available CPUs.
Executables can be configured with flags or `PYTHON`, `BPY`, `FPY`, `OHA`,
`AXUM`; `SERVER_CPUS`, `CLIENT_CPUS`, `DUR`, `PORT` and `MW` are also accepted.
`HARNESS_PYTHON` selects the Python used by the shell wrapper. Relative executable
paths are resolved before entering the apps directory. `BESSEMER_*` environment
settings pass through unchanged, including admission and event-loop settings.
No fixed admission override is imposed. Use `--port 0` (default) for a free
loopback port; explicit occupied ports fail instead of probing another server.

## Optional same-user CPU isolation (Linux)

On a dedicated benchmark machine, `isolate_cpus.sh on` pins other existing
same-user threads to one housekeeping CPU; children inherit that affinity.
It does not run automatically. This affects shells, builds and other agents
owned by that user, so coordinate before using it. It neither changes system
services nor requires root. The helper excludes itself.

```sh
state="$PWD/isolation-state.json"
benchmarks/compare/isolate_cpus.sh on --cpus 7 --state "$state"
trap 'benchmarks/compare/isolate_cpus.sh off --state "$state"' EXIT
# Specify CPU lists explicitly: this shell was moved to CPU 7, but taskset
# in the runner expands the benchmark processes onto their chosen CPUs.
benchmarks/compare/run_concurrency.sh --python VENV/bin/python \
  --server-cpus 0-3 --client-cpus 4-6 --out isolated.jsonl
benchmarks/compare/isolate_cpus.sh off --state "$state"
trap - EXIT
```

`off` restores each saved thread's original mask, checking its start time so
reused thread IDs are not modified. The state file is written before affinity
changes and refuses accidental overwrite; retain it if a run is interrupted.
Threads/processes created after the snapshot are not restored automatically;
they retain inherited housekeeping affinity. Restart those tasks or explicitly
restore their intended masks. Isolation reduces interference but does not
prove the client is fast enough, eliminate frequency/timer differences, or
establish the formal M0 gate. Do not quote shared-host smoke-run rates as
performance acceptance.
