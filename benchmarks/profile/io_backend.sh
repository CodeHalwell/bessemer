#!/usr/bin/env bash
# CPU per request of the Bessemer worker on the Rust event loop with the epoll
# and io_uring I/O backends, using the method of the io_uring spike
# (benchmarks/results/2026-11-05/io-uring-spike, crates/uring-spike/measure.sh)
# on the real server and the /json route of benchmarks/compare/bessemer_app.py.
#
#   benchmarks/profile/io_backend.sh OUT.tsv
#
# Each run starts `python -m bessemer run bessemer_app:app --workers 1` with
# BESSEMER_EVENT_LOOP=bessemer and BESSEMER_IO_BACKEND=<backend>, pinned to
# SERVER_CPU. oha runs on each of CLIENT_CPUS: "3" leaves the server about
# half busy (client-bound), "1-3" saturates the server core. Every
# connections x client CPUs x round runs each backend once, so backends are
# interleaved and drift on a shared VM affects them alike.
#
# Environment: BACKENDS (default "epoll io_uring"), CONNECTIONS (default
# "64 1024"), CLIENT_CPUS (default "3 1-3"), REPS (default 3), SERVER_CPU
# (default 0), WARMUP (seconds, default 2), DURATION (seconds, default 10),
# ROUTE (default /json), PYTHON (interpreter with the bessemer wheel
# installed, default python3), OHA (default oha), PORT (first port, default
# 18200). A smoke run:
#   BACKENDS=epoll CONNECTIONS=64 CLIENT_CPUS=3 REPS=1 WARMUP=1 DURATION=2 \
#       benchmarks/profile/io_backend.sh smoke.tsv
#
# Method. The server has no request counter, so CPU and requests are both
# taken over the whole measured oha run: after a separate WARMUP oha run, the
# worker's on-CPU time (summed over its threads from
# /proc/PID/task/*/schedstat, which is what perf's task-clock counts) and its
# utime/stime ticks (/proc/PID/stat) are read immediately before the measured
# oha run starts and immediately after it exits. CPU per request is that CPU
# time divided by the responses oha received in the run (the sum of its status
# code distribution). The worker is idle outside the run, so the few
# milliseconds of process start and exit at the ends add almost nothing; the
# requests oha abandons at the deadline (at most one per connection) were
# partly served but are not counted, which overstates CPU per request by at
# most CONNECTIONS / requests (about 0.1% at 1024 connections). server_cpus
# is that CPU time over oha's run time. The user/sys split is the CPU per
# request divided in the proportion of the tick counts. Only the worker is
# measured (the supervisor process idles). A run with any non-200 response
# or any error other than oha's deadline abort is rejected: it is reported
# on stderr and left out of the TSV and the medians.
set -euo pipefail
out=${1:?usage: io_backend.sh OUT.tsv}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out_dir=$(cd -- "$(dirname -- "$out")" && pwd)
out=$out_dir/$(basename -- "$out")
python=${PYTHON:-python3}
oha=${OHA:-oha}
backends=${BACKENDS:-epoll io_uring}
server_cpu=${SERVER_CPU:-0}
warmup=${WARMUP:-2}
duration=${DURATION:-10}
route=${ROUTE:-/json}
port=${PORT:-18200}
hz=$(getconf CLK_TCK)
tmp=$(mktemp -d)
server=
cleanup() {
    [[ -n $server ]] && kill "$server" 2>/dev/null || true
    rm -rf "$tmp"
}
trap cleanup EXIT

command -v "$oha" >/dev/null || { echo "oha not found (cargo install oha --locked)" >&2; exit 1; }
"$python" -c 'import bessemer._native' || {
    echo "$python cannot import bessemer._native; install the wheel (pip install .)" >&2
    exit 1
}

cpu_ns() { # total on-CPU ns over the threads of the given PIDs
    local pid
    for pid in "$@"; do cat /proc/"$pid"/task/*/schedstat; done | awk '{s += $1} END {printf "%d\n", s}'
}
ticks() { # summed utime stime of the given PIDs
    local pid
    for pid in "$@"; do awk '{print $14, $15}' /proc/"$pid"/stat; done |
        awk '{u += $1; s += $2} END {print u, s}'
}

printf 'backend\tconnections\tclient_cpus\trep\treq_per_s\tserver_cpus\tus_per_req\tuser_us\tsys_us\tp50_us\tp99_us\n' >"$out"
cd "$here/../compare"
for conns in ${CONNECTIONS:-64 1024}; do
for client in ${CLIENT_CPUS:-3 1-3}; do
for rep in $(seq "${REPS:-3}"); do
for backend in $backends; do
    port=$((port + 1))
    url=http://127.0.0.1:$port$route
    BESSEMER_EVENT_LOOP=bessemer BESSEMER_IO_BACKEND=$backend \
        taskset -c "$server_cpu" "$python" -m bessemer run bessemer_app:app \
        --host 127.0.0.1 --port "$port" --workers 1 >"$tmp/server.log" 2>&1 &
    server=$!
    ready=
    for _ in $(seq 150); do
        if curl -sf "$url" >/dev/null; then ready=1; break; fi
        kill -0 "$server" 2>/dev/null || break
        sleep 0.2
    done
    if [[ -z $ready ]]; then
        echo "server ($backend) did not start; log:" >&2
        cat "$tmp/server.log" >&2
        exit 1
    fi
    # The supervisor forks the worker; measure the worker only.
    mapfile -t workers < <(ps -o pid= --ppid "$server" | tr -d ' ')
    ((${#workers[@]})) || workers=("$server")
    taskset -c "$client" "$oha" -z "${warmup}s" -c "$conns" --no-tui "$url" >/dev/null
    read -r u0 s0 < <(ticks "${workers[@]}"); c0=$(cpu_ns "${workers[@]}")
    taskset -c "$client" "$oha" -z "${duration}s" -c "$conns" --no-tui \
        --output-format json "$url" >"$tmp/oha.json"
    read -r u1 s1 < <(ticks "${workers[@]}"); c1=$(cpu_ns "${workers[@]}")
    kill "$server"; wait "$server" 2>/dev/null || true
    server=
    "$python" - "$tmp/oha.json" "$backend" "$conns" "$client" "$rep" \
        "$((c1 - c0))" "$((u1 - u0))" "$((s1 - s0))" "$hz" >>"$out" <<'PY' || continue
import json, sys
path, backend, conns, client, rep = sys.argv[1:6]
cpu_ns, utick, stick, hz = map(float, sys.argv[6:10])
d = json.load(open(path))
codes = d["statusCodeDistribution"]
# oha cuts off the requests in flight when -z expires; anything else is a failure.
errors = {k: v for k, v in d["errorDistribution"].items() if k != "aborted due to deadline"}
if set(codes) != {"200"} or errors:
    print(f"rejected {backend} c={conns} cpus={client} rep={rep}: {codes} {errors}", file=sys.stderr)
    sys.exit(1)
reqs = sum(codes.values())
elapsed = d["summary"]["total"]
per = cpu_ns / 1e3 / reqs
share_u = utick / max(utick + stick, 1)
pct = d["latencyPercentiles"]
print(f"{backend}\t{conns}\t{client}\t{rep}\t{reqs / elapsed:.0f}\t{cpu_ns / 1e9 / elapsed:.2f}"
      f"\t{per:.2f}\t{per * share_u:.2f}\t{per * (1 - share_u):.2f}"
      f"\t{pct['p50'] * 1e6:.0f}\t{pct['p99'] * 1e6:.0f}")
PY
    tail -1 "$out"
done
done
done
done

"$python" - "$out" <<'PY'
import csv, statistics, sys
rows = list(csv.DictReader(open(sys.argv[1]), delimiter="\t"))
groups = {}
for row in rows:
    groups.setdefault((int(row["connections"]), row["client_cpus"], row["backend"]), []).append(row)
fields = ["req_per_s", "server_cpus", "us_per_req", "user_us", "sys_us", "p50_us", "p99_us"]
print("\nmedians")
print("connections\tclient_cpus\tbackend\tn\t" + "\t".join(fields))
for (conns, client, backend), group in groups.items():
    medians = [statistics.median(float(r[f]) for r in group) for f in fields]
    print(f"{conns}\t{client}\t{backend}\t{len(group)}\t" + "\t".join(f"{m:g}" for m in medians))
PY
