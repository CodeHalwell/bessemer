#!/usr/bin/env bash
# Count user-space instructions per request in a Bessemer worker with
# Valgrind's callgrind. Unlike req/s or CPU time on a shared VM (±7% between
# runs), the count repeats to about 0.1%, so it shows small changes to the
# request path. It excludes kernel time, which is about 40% of a request.
#
#   benchmarks/profile/instructions.sh ROUTE [REQUESTS]
#
# Environment: PYTHON (venv with bessemer installed), OHA, CONNECTIONS
# (default 8), and for a request with a body METHOD (default GET), BODY_FILE
# (sent with each request) and CONTENT_TYPE (default application/json when
# BODY_FILE is set), e.g. METHOD=POST BODY_FILE=order.json for /echo. The worker runs about 50 times slower under callgrind, so a few
# thousand requests suffice. With several connections the loop takes requests
# in batches of varying size, which moves the count by about 1% between runs;
# CONNECTIONS=1 gives one request per batch and repeats to about 0.1%.
# The per-function profile is left in $OUT (default ./instructions-out) for
# callgrind_annotate.
set -euo pipefail
route=$1
requests=${2:-5000}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$PWD/instructions-out}
python=${PYTHON:-python3}
oha=${OHA:-oha}
port=${PORT:-18095}
connections=${CONNECTIONS:-8}
method=${METHOD:-GET}
request=(-m "$method")
probe=(-X "$method")
if [[ -n ${BODY_FILE:-} ]]; then
    body_file=$(realpath -- "$BODY_FILE")
    content_type=${CONTENT_TYPE:-application/json}
    request+=(-D "$body_file" -T "$content_type")
    probe+=(--data-binary "@$body_file" -H "content-type: $content_type")
fi
mkdir -p "$out"
rm -f "$out"/callgrind.out.*

cd "$here/../compare"
valgrind --tool=callgrind --trace-children=yes \
    --callgrind-out-file="$out/callgrind.out.%p" \
    "$python" -m bessemer run bessemer_app:app --host 127.0.0.1 --port "$port" \
    --workers 1 --event-loop asyncio >"$out/server.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
for _ in $(seq 300); do
    curl -sf "${probe[@]}" "http://127.0.0.1:$port$route" >/dev/null && break
    sleep 0.5
done
"$oha" -n 2000 -c 8 --no-tui "${request[@]}" "http://127.0.0.1:$port$route" >/dev/null
# Zero the counters, serve exactly $requests requests, and dump them.
callgrind_control -z >/dev/null 2>&1
"$oha" -n "$requests" -c "$connections" --no-tui "${request[@]}" "http://127.0.0.1:$port$route" >/dev/null
callgrind_control -d >/dev/null 2>&1
sleep 1
kill "$server"
wait "$server" 2>/dev/null || true
trap - EXIT
# The worker's dump of the measured window is the largest numbered dump.
best=0
best_file=
for file in "$out"/callgrind.out.*.*; do
    total=$(grep -m1 '^summary:' "$file" | cut -d' ' -f2)
    if ((total > best)); then
        best=$total
        best_file=$file
    fi
done
echo "$route: $((best / requests)) instructions per request ($best_file)"
