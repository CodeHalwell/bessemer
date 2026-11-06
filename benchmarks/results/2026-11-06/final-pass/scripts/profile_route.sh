#!/usr/bin/env bash
# Copy of benchmarks/profile/profile_route.sh extended for the 2026-11-06
# reprofile (original untouched). Differences:
#   * METHOD / BODY_FILE / CONTENT_TYPE (same convention as instructions.sh)
#   * writes oha latency percentiles (p50, p99, p99.9) and success/error info
#   * CLIENT_CPU may be a list ("1-3"); SERVER_CPU may be a list ("0-1")
#   * WORKERS (bessemer --workers) and AXUM_THREADS (axum's second argument)
#   * measures oha's own CPU (perf stat) to flag client-limited runs
#   * per-thread CPU from /proc/<pid>/task/*/stat before/after the same window
#   * the stat window has NO perf record in it. With PERF_RECORD=1 a second,
#     separate load phase follows the measured window and perf record runs in it.
#
#   profile_route.sh bessemer|axum ROUTE [OUT_DIR]
# Environment: PYTHON, OHA, AXUM, SERVER_CPU(0), CLIENT_CPU(3), CONNECTIONS(64),
#   PORT, DURATION(10), WORKERS(1), AXUM_THREADS(1), PERF_RECORD(0),
#   METHOD(GET), BODY_FILE, CONTENT_TYPE(application/json when BODY_FILE set).
# Set PYTHONPERFSUPPORT=1 yourself to name Python frames (it costs CPU, so the
# matrix runs leave it unset). BESSEMER_* variables pass through to the server.
set -euo pipefail
which=$1
route=$2
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=/home/user/bessemer
out=${3:-$PWD/profile-out}
mkdir -p "$out"
out=$(cd -- "$out" && pwd)
python=${PYTHON:-python3}
oha=${OHA:-oha}
axum=${AXUM:-$repo/benchmarks/compare/axum_app/target/release/axum-compare}
server_cpu=${SERVER_CPU:-0}
client_cpu=${CLIENT_CPU:-3}
connections=${CONNECTIONS:-64}
port=${PORT:-18080}
duration=${DURATION:-10}
workers=${WORKERS:-1}
axum_threads=${AXUM_THREADS:-1}
method=${METHOD:-GET}
request=(-m "$method")
probe=(-X "$method")
if [[ -n ${BODY_FILE:-} ]]; then
    body_file=$(realpath -- "$BODY_FILE")
    content_type=${CONTENT_TYPE:-application/json}
    request+=(-D "$body_file" -T "$content_type")
    probe+=(--data-binary "@$body_file" -H "content-type: $content_type")
fi
url="http://127.0.0.1:$port$route"

cd "$repo/benchmarks/compare"
if [[ $which == bessemer ]]; then
    taskset -c "$server_cpu" "$python" -m bessemer run bessemer_app:app \
        --host 127.0.0.1 --port "$port" --workers "$workers" >"$out/server.log" 2>&1 &
else
    taskset -c "$server_cpu" "$axum" "$port" "$axum_threads" >"$out/server.log" 2>&1 &
fi
server=$!
trap 'kill $server 2>/dev/null || true; wait $server 2>/dev/null || true' EXIT
ready=0
for _ in $(seq 100); do
    if curl -sf "${probe[@]}" "$url" >/dev/null; then ready=1; break; fi
    sleep 0.2
done
if [[ $ready == 0 ]]; then echo "server did not become ready; see $out/server.log" >&2; exit 3; fi
# warm-up
taskset -c "$client_cpu" "$oha" -z 3s -c "$connections" --no-tui "${request[@]}" "$url" >/dev/null
# Bessemer's supervisor forks the workers; measure the workers only.
pids=$( (ps -o pid= --ppid "$server" || true) | tr -d " " | paste -sd, -)
[[ -z $pids ]] && pids=$server
echo "server=$server measured_pids=$pids" >"$out/pids.txt"
ps -L -o pid,tid,comm,psr --ppid "$server" >>"$out/pids.txt" 2>/dev/null || true

python3 "$here/threads.py" snap "$pids" "$out/threads-before.json"
perf stat -e task-clock,context-switches -x, -o "$out/client-stat.txt" -- \
    taskset -c "$client_cpu" "$oha" -z "${duration}s" -c "$connections" --no-tui \
    --output-format json "${request[@]}" "$url" >"$out/oha.json" &
load=$!
perf stat -e task-clock,context-switches -p "$pids" -o "$out/stat.txt" -- sleep "$duration" &
stat=$!
wait "$stat"
python3 "$here/threads.py" snap "$pids" "$out/threads-after.json"
wait "$load"

if [[ ${PERF_RECORD:-0} == 1 ]]; then
    # separate load phase; not part of the numbers above
    taskset -c "$client_cpu" "$oha" -z 9s -c "$connections" --no-tui "${request[@]}" "$url" >/dev/null &
    load2=$!
    sleep 3
    perf record -F 4999 -e cpu-clock -g -p "$pids" -o "$out/perf.data" -- sleep 4 2>"$out/perf-record.log" || true
    wait "$load2"
    cp /tmp/perf-*.map "$out/" 2>/dev/null || true
fi

CLIENT_CPU=$client_cpu python3 - "$out" "$which" "$route" "$connections" "$here" <<'PY'
import json, re, subprocess, sys
out, which, route, conns, here = sys.argv[1:6]
oha = json.load(open(f"{out}/oha.json"))
s = oha["summary"]
rate = s["requestsPerSec"]
total = s["total"]  # seconds oha ran
ok = oha["statusCodeDistribution"]
nreq = sum(ok.values())
stat = open(f"{out}/stat.txt").read()
msec = float(re.search(r"([\d.,]+)\s+msec task-clock", stat)[1].replace(",", ""))
cs = float(re.search(r"([\d.,]+)\s+context-switches", stat)[1].replace(",", ""))
# window = 10 s of sleep; requests in window ~ rate * window
window = float(re.search(r"([\d.]+) seconds time elapsed", stat)[1])
reqs = rate * window
cstat = open(f"{out}/client-stat.txt").read().splitlines()
cmsec = next(float(l.split(",")[0]) for l in cstat if "task-clock" in l)
client_cpus = cmsec / 1000 / total
ncl = 0
import os
cl = os.environ.get("CLIENT_CPU", "3")
for part in cl.split(","):
    if "-" in part:
        a, b = part.split("-"); ncl += int(b) - int(a) + 1
    else:
        ncl += 1
lp = oha.get("latencyPercentiles", {})
good = ok.get("200", 0)
res = {
    "server": which, "route": route, "connections": int(conns),
    "req_s": rate, "window_s": window,
    "server_cpus": msec / 1000 / window,
    "us_cpu_per_req": msec * 1000 / window / rate,
    "ctx_switches_per_req": cs / window / rate,
    "p50_ms": lp.get("p50", 0) * 1000, "p99_ms": lp.get("p99", 0) * 1000,
    "p99_9_ms": lp.get("p99.9", 0) * 1000,
    "client_cpus": client_cpus, "client_cores": ncl,
    "client_util": client_cpus / ncl,
    "client_limited": client_cpus / ncl >= 0.95,
    "success_rate": s["successRate"], "status": ok,
    "errors": oha.get("errorDistribution", {}),
    "ok_responses": good, "total_responses": nreq,
}
th = subprocess.run([sys.executable, f"{here}/threads.py", "diff", f"{out}/threads-before.json",
                     f"{out}/threads-after.json", str(reqs)], capture_output=True, text=True).stdout
res["threads"] = json.loads(th)["threads"] if th else {}
json.dump(res, open(f"{out}/result.json", "w"), indent=1)
print(f"{which} {route} c={conns}: {rate:,.0f} req/s, {res['server_cpus']:.2f} CPUs, "
      f"{res['us_cpu_per_req']:.2f} us CPU/req, {res['ctx_switches_per_req']:.3f} cs/req, "
      f"p50 {res['p50_ms']:.3f} p99 {res['p99_ms']:.3f} p99.9 {res['p99_9_ms']:.3f} ms, "
      f"client {res['client_util']:.2f}, ok {good}/{nreq}")
PY
