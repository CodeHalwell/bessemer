#!/usr/bin/env bash
# Transport profiles: MODE=keep|close|h2c|h2tls|tls-full|tls-resume.
# HTTP/2 uses CONNECTIONS connections and STREAMS concurrent streams each.
# TLS profiles use a private self-signed RSA certificate and verify it locally.
# Isolated vgdb prefix and PID-scoped controls permit concurrent profiles.
# Count user-space instructions per request in a Bessemer worker with
# Valgrind's callgrind. Unlike req/s or CPU time on a shared VM (±7% between
# runs), the count repeats to about 0.1%, so it shows small changes to the
# request path. It excludes kernel time.
#
#   MODE=h2c STREAMS=64 benchmarks/profile/instructions_transport.sh ROUTE [REQUESTS]
#
# Environment: PYTHON (venv with bessemer installed), OHA, BESSEMER_EVENT_LOOP
# (asyncio, bessemer or auto; default asyncio), CONNECTIONS
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
out=${OUT:-$PWD/instructions-out}
out=$(realpath -m "$out")
mode=${MODE:-close}
streams=${STREAMS:-64}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=${PYTHON:-python3}
oha=${OHA:-oha}
port=${PORT:-18095}
connections=${CONNECTIONS:-8}
method=${METHOD:-GET}
request=(-m "$method")
server_options=()
scheme=http
case "$mode" in
    keep) ;;
    close) request+=(--disable-keepalive) ;;
    h2c) request+=(--http2 -p "$streams") ;;
    h2tls) request+=(--http2 -p "$streams"); scheme=https ;;
    tls-full|tls-resume) scheme=https ;;
    *) echo "unknown MODE=$mode" >&2; exit 2 ;;
esac
probe=(-X "$method")
if [[ -n ${BODY_FILE:-} ]]; then
    body_file=$(realpath -- "$BODY_FILE")
    content_type=${CONTENT_TYPE:-application/json}
    request+=(-D "$body_file" -T "$content_type")
    probe+=(--data-binary "@$body_file" -H "content-type: $content_type")
fi
mkdir -p "$out"
rm -f "$out"/callgrind.out.*
if [[ $scheme == https ]]; then
    certificate=${PROFILE_TLS_CERT:-$out/cert.pem}
    private_key=${PROFILE_TLS_KEY:-$out/key.pem}
    if [[ ! -f $certificate || ! -f $private_key ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -keyout "$private_key" \
            -out "$certificate" -days 1 -subj /CN=localhost \
            -addext subjectAltName=DNS:localhost,IP:127.0.0.1 \
            -addext basicConstraints=critical,CA:FALSE >"$out/openssl.log" 2>&1
    fi
    server_options+=(--tls-cert "$certificate" --tls-key "$private_key")
    request+=(--cacert "$certificate")
    probe+=(--cacert "$certificate")
fi
url="$scheme://127.0.0.1:$port$route"
load() {
    local count=$1
    if [[ $mode == tls-* ]]; then
        "$python" "$here/tls_connections.py" "$port" "$certificate" "$count" "$mode" "$route"
    else
        local output
        output=$("$oha" -n "$count" -c "$connections" --no-tui "${request[@]}" "$url")
        printf '%s\n' "$output"
        if ! [[ $output == *"[200] $count responses"* ]]; then
            echo "profile rejected: expected $count successful HTTP 200 responses" >&2
            return 1
        fi
    fi
}

export FW=bessemer
cd "${MATRIX_DIR:-$here/../compare}"
valgrind --vgdb-prefix="$out/vgdb" --tool=callgrind --trace-children=yes \
    --callgrind-out-file="$out/callgrind.out.%p" \
    "$python" -m bessemer run "${PROFILE_APP:-bessemer_app:app}" --host 127.0.0.1 --port "$port" \
    "${server_options[@]}" --workers 1 --max-in-flight "${PROFILE_MAX_IN_FLIGHT:-auto}" --startup-timeout 180 --event-loop "${BESSEMER_EVENT_LOOP:-asyncio}" >"$out/server.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
ready=false
for _ in $(seq 300); do
    if curl --max-time 2 -sf "${probe[@]}" "$url" >/dev/null; then
        ready=true
        break
    fi
    kill -0 "$server" 2>/dev/null || break
    sleep 0.5
done
if [[ "$ready" != true ]]; then
    echo "profile worker did not become ready; see $out/server.log" >&2
    exit 1
fi
load "${WARMUP:-1000}" >"$out/warmup.txt"
# Zero the counters, serve exactly $requests requests, and dump them.
profile_pids=("$server")
while read -r child; do
    profile_pids+=("$child")
done < <(pgrep -P "$server")
callgrind_control --vgdb-prefix="$out/vgdb" -z "${profile_pids[@]}" >/dev/null 2>&1
load "$requests" >"$out/client.txt"
callgrind_control --vgdb-prefix="$out/vgdb" -d "${profile_pids[@]}" >/dev/null 2>&1
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
echo "$mode c$connections p$streams $route: $((best / requests)) instructions per request ($best_file)"
