#!/usr/bin/env bash
# Measure CPU per request for one route of the comparison apps, and record a
# perf profile of the server while oha keeps it saturated.
#
#   benchmarks/profile/profile_route.sh bessemer|axum ROUTE [OUT_DIR]
#
# Environment: PYTHON (venv with bessemer installed), OHA, AXUM (built
# benchmarks/compare/axum_app binary), SERVER_CPU (default 0), CLIENT_CPU
# (default 3), CONNECTIONS (default 64). Needs perf; set PYTHONPERFSUPPORT=1
# to name Python frames in the profile.
#
# CPU per request = task-clock / requests completed during the perf stat
# window. Use it rather than req/s when the single client core is the
# ceiling (Axum on one core is client-bound on a 4-vCPU host).
set -euo pipefail
which=$1
route=$2
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out=${3:-$PWD/profile-out}
mkdir -p "$out"
python=${PYTHON:-python3}
oha=${OHA:-oha}
axum=${AXUM:-$here/../compare/axum_app/target/release/axum-compare}
server_cpu=${SERVER_CPU:-0}
client_cpu=${CLIENT_CPU:-3}
connections=${CONNECTIONS:-64}
port=${PORT:-18080}

cd "$here/../compare"
if [[ $which == bessemer ]]; then
    taskset -c "$server_cpu" "$python" -m bessemer run bessemer_app:app \
        --host 127.0.0.1 --port "$port" --workers 1 >"$out/server.log" 2>&1 &
else
    taskset -c "$server_cpu" "$axum" "$port" 1 >"$out/server.log" 2>&1 &
fi
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
for _ in $(seq 50); do
    curl -sf "http://127.0.0.1:$port/json" >/dev/null && break
    sleep 0.2
done
taskset -c "$client_cpu" "$oha" -z 3s -c "$connections" --no-tui \
    "http://127.0.0.1:$port$route" >/dev/null
# Bessemer's supervisor forks one worker; profile the worker only.
pids=$( (ps -o pid= --ppid "$server" || true) | tr -d " " | paste -sd, -)
[[ -z $pids ]] && pids=$server
taskset -c "$client_cpu" "$oha" -z 12s -c "$connections" --no-tui \
    --output-format json "http://127.0.0.1:$port$route" >"$out/oha-$which.json" &
load=$!
sleep 2
perf stat -e task-clock,context-switches -p "$pids" -o "$out/stat-$which.txt" -- sleep 5
perf record -F 4999 -e cpu-clock -g -p "$pids" -o "$out/perf-$which.data" -- sleep 3 \
    2>/dev/null
wait "$load"
"$python" - "$out" "$which" <<'PY'
import json, re, sys
out, which = sys.argv[1], sys.argv[2]
rate = json.load(open(f"{out}/oha-{which}.json"))["summary"]["requestsPerSec"]
stat = open(f"{out}/stat-{which}.txt").read()
msec = float(re.search(r"([\d.,]+)\s+msec task-clock", stat)[1].replace(",", ""))
print(f"{which}: {rate:,.0f} req/s, {msec / 5000:.2f} CPUs, "
      f"{msec * 1000 / 5 / rate:.2f} us CPU per request")
PY
echo "perf report -i $out/perf-$which.data --no-children --sort dso,sym"
