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
