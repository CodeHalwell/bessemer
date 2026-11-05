#!/usr/bin/env bash
# CPU per request for each backend of the io_uring spike (and optionally the
# Axum comparison app), with the server pinned to one core and oha on others.
#
#   crates/uring-spike/measure.sh OUT.tsv
#
# Environment: BACKENDS (default "tokio mio uring uring-plain"), AXUM (path to
# benchmarks/compare/axum_app's release binary, to add it as a reference),
# CONNECTIONS (default "64 1024"), CLIENT_CPUS (default "3 1-3"), REPS
# (default 3), SERVER_CPU (default 0), OHA (default oha).
#
# CPU per request = the server's on-CPU time over a 5 s window (summed over
# its threads from /proc/PID/task/*/schedstat, which is what perf's
# task-clock counts) / requests the server answered in that same window (its
# GET /stats counter, read at both ends). Axum has no counter, so for it the
# request count is oha's average rate x 5 s. A run with any failed request is
# rejected.
# User and system shares come from /proc/PID/stat at clock-tick resolution.
set -euo pipefail
out=${1:?usage: measure.sh OUT.tsv}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin=$here/target/release/uring-spike
oha=${OHA:-oha}
backends=${BACKENDS:-tokio mio uring uring-plain}
[[ -n ${AXUM:-} ]] && backends="$backends axum"
server_cpu=${SERVER_CPU:-0}
port=${PORT:-18090}
window=5
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cpu_ns() { # total on-CPU ns over the process's threads
    cat /proc/"$1"/task/*/schedstat | awk '{s += $1} END {printf "%d\n", s}'
}
served() { # requests answered so far, or empty if the server has no counter
    curl -sf "http://127.0.0.1:$1/stats" || true
}
ticks() { # utime stime
    awk '{print $14, $15}' /proc/"$1"/stat
}

printf 'backend\tconnections\tclient_cpus\trep\treq_per_s\tserver_cpus\tus_per_req\tuser_us\tsys_us\tp50_us\tp99_us\n' >"$out"
for conns in ${CONNECTIONS:-64 1024}; do
for client in ${CLIENT_CPUS:-3 1-3}; do
for rep in $(seq "${REPS:-3}"); do
for backend in $backends; do
    port=$((port + 1))
    case $backend in
        axum) taskset -c "$server_cpu" "$AXUM" "$port" 1 2>/dev/null & ;;
        uring-plain) SPIKE_URING_PLAIN=1 taskset -c "$server_cpu" "$bin" uring "$port" 2>/dev/null & ;;
        *) taskset -c "$server_cpu" "$bin" "$backend" "$port" 2>/dev/null & ;;
    esac
    server=$!
    for _ in $(seq 50); do
        curl -sf "http://127.0.0.1:$port/json" >/dev/null && break
        sleep 0.1
    done
    url=http://127.0.0.1:$port/json
    taskset -c "$client" "$oha" -z 2s -c "$conns" --no-tui "$url" >/dev/null
    taskset -c "$client" "$oha" -z 10s -c "$conns" --no-tui --output-format json "$url" >"$tmp/oha.json" &
    load=$!
    sleep 2
    n0=$(served "$port"); read -r u0 s0 < <(ticks "$server"); c0=$(cpu_ns "$server")
    sleep "$window"
    n1=$(served "$port"); read -r u1 s1 < <(ticks "$server"); c1=$(cpu_ns "$server")
    wait "$load"
    kill "$server"; wait "$server" 2>/dev/null || true
    python3 - "$tmp/oha.json" "$backend" "$conns" "$client" "$rep" "$window" \
        "$((c1 - c0))" "$((u1 - u0))" "$((s1 - s0))" "$(getconf CLK_TCK)" \
        "${n0:+$((n1 - n0))}" >>"$out" <<'PY'
import json, sys
path, backend, conns, client, rep = sys.argv[1:6]
window, cpu_ns, utick, stick, hz = map(float, sys.argv[6:11])
counted = sys.argv[11]
d = json.load(open(path))
rate = d["summary"]["requestsPerSec"]
codes = d["statusCodeDistribution"]
assert set(codes) == {"200"}, codes
# oha cuts off the requests in flight when -z expires; anything else is a failure.
errors = {k: v for k, v in d["errorDistribution"].items() if k != "aborted due to deadline"}
assert not errors, errors
assert d["summary"]["successRate"] == 1.0, d["summary"]["successRate"]
reqs = float(counted) if counted else rate * window
per = cpu_ns / 1e3 / reqs
share_u = utick / max(utick + stick, 1)
pct = d["latencyPercentiles"]
print(f"{backend}\t{conns}\t{client}\t{rep}\t{rate:.0f}\t{cpu_ns / 1e9 / window:.2f}\t{per:.2f}"
      f"\t{per * share_u:.2f}\t{per * (1 - share_u):.2f}\t{pct['p50'] * 1e6:.0f}\t{pct['p99'] * 1e6:.0f}")
PY
    tail -1 "$out"
done
done
done
done
