#!/usr/bin/env bash
# Private per-agent variant of benchmarks/profile/instructions.sh for concurrent
# agents on one machine: a private vgdb prefix keeps callgrind_control's
# zero/dump away from other agents' processes, and APP_DIR/APP choose the app.
#
#   instructions-pi.sh ROUTE [REQUESTS]
#
# Environment: PYTHON, OHA, BESSEMER_EVENT_LOOP (asyncio, bessemer or auto),
# CONNECTIONS (default 1), PORT, METHOD, BODY_FILE, CONTENT_TYPE, APP
# (default bessemer_app:app), APP_DIR (default benchmarks/compare), OUT
# (default ./instructions-out), VG_PREFIX (default /tmp/vg-pi-models/$PORT).
set -euo pipefail
route=$1
requests=${2:-2000}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$PWD/instructions-out}
app=${APP:-bessemer_app:app}
app_dir=${APP_DIR:-$here/../compare}
python=${PYTHON:-python3}
oha=${OHA:-oha}
port=${PORT:-18131}
connections=${CONNECTIONS:-1}
method=${METHOD:-GET}
prefix=${VG_PREFIX:-/tmp/vg-pi-models/$port/m}
mkdir -p "$out" "$(dirname "$prefix")"
rm -f "$out"/callgrind.out.* "$prefix"* "$prefix"/*

request=(-m "$method")
probe=(-X "$method")
if [[ -n ${BODY_FILE:-} ]]; then
    body_file=$(realpath -- "$BODY_FILE")
    content_type=${CONTENT_TYPE:-application/json}
    request+=(-D "$body_file" -T "$content_type")
    probe+=(--data-binary "@$body_file" -H "content-type: $content_type")
fi

cd "$app_dir"
valgrind --tool=callgrind --trace-children=yes \
    --vgdb-prefix="$prefix" \
    --callgrind-out-file="$out/callgrind.out.%p" \
    "$python" -m bessemer run "$app" --host 127.0.0.1 --port "$port" \
    --workers 1 --event-loop "${BESSEMER_EVENT_LOOP:-auto}" >"$out/server.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
for _ in $(seq 300); do
    curl -sf "${probe[@]}" "http://127.0.0.1:$port$route" >/dev/null && break
    sleep 0.5
done
# The worker is the run command's direct child.
worker=$(pgrep -P "$server" 2>/dev/null | tail -1)
echo "worker pid $worker" >&2
# Warm up, then zero only this worker, serve exactly $requests, dump. A pause
# before zeroing (and before the dump) lets the worker park in epoll, so the
# counters and the dump both start on a shallow call stack: callgrind records
# the open call context's inclusive cost at zero/dump time, which otherwise
# varies with where the last request was interrupted and inflates the summary.
"$oha" -n 2000 -c "$connections" --no-tui "${request[@]}" "http://127.0.0.1:$port$route" >/dev/null 2>&1
sleep 2
callgrind_control --vgdb-prefix="$prefix" "$worker" -z >/dev/null 2>&1
"$oha" -n "$requests" -c "$connections" --no-tui "${request[@]}" "http://127.0.0.1:$port$route" >/dev/null 2>&1
sleep 2
callgrind_control --vgdb-prefix="$prefix" "$worker" -d >/dev/null 2>&1
sleep 1
kill "$server" 2>/dev/null || true
wait "$server" 2>/dev/null || true
trap - EXIT
dump=$out/callgrind.out."$worker".1
[[ -e $dump ]] || dump=$out/callgrind.out."$worker"
if [[ ! -e $dump ]]; then
    echo "no dump in $out" >&2
    exit 1
fi
summary=$(grep -m1 '^summary:' "$dump" | cut -d' ' -f2)
self=$(python3 - "$dump" <<'PYEOF'
import re, sys, collections
defs = {}
for line in open(sys.argv[1]):
    if line.startswith(('fn=', 'cfn=')):
        m = re.match(r'(fn|cfn)=\((\d+)\)((?: (.*))?)[ ]*$', line.rstrip())
        if m and (m.group(3) or '').strip():
            defs[m.group(2)] = m.group(3).strip()
cost = collections.Counter()
cur = None
after_call = False
for line in open(sys.argv[1]):
    if line.startswith('fn=('):
        cur = defs.get(re.match(r'fn=\((\d+)\)', line).group(1), cur)
        after_call = False
    elif line.startswith('fn='):
        cur = line[3:]
        after_call = False
    elif line.startswith(('cfn=', 'cob=', 'cfi=', 'calls=')):
        after_call = line.startswith('calls=')
    elif line and line[0].isdigit() and cur is not None:
        if after_call:
            after_call = False
            continue
        cost[cur] += int(line.split()[1])
print(sum(cost.values()))
PYEOF
)
echo "$route: summary $((summary / requests)), self cost $((self / requests)) per request ($dump)"
# Companion to instructions.sh for agents sharing one machine:
#
# * the worker is zeroed and dumped by PID, through a private vgdb prefix, so
#   concurrent agents' callgrind processes are untouched;
# * pauses before zeroing and dumping let the worker park in epoll, because
#   callgrind's `summary:` includes the inclusive record of the call context
#   open at zero/dump time, which otherwise varies with where those land and
#   can add thousands of instructions per request;
# * the report counts the dump's self costs (all function costs, excluding
#   the call-context records), which repeat to well under 1%.
#
# Environment: PYTHON, OHA, BESSEMER_EVENT_LOOP, CONNECTIONS, PORT, METHOD,
# BODY_FILE, CONTENT_TYPE, APP (default bessemer_app:app), APP_DIR (default
# benchmarks/compare), OUT (default ./instructions-out),
# VG_PREFIX (default /tmp/vg-private/$PORT).
