#!/usr/bin/env bash
set -euo pipefail

if ! command -v oha >/dev/null 2>&1; then
    echo "oha is required for M0; install it with your platform package manager" >&2
    exit 1
fi

cargo build --release
binary=target/release/bessemer-m0
duration=${DURATION:-15s}
connections=${CONNECTIONS:-50}
server_pid=
log_file=

cleanup() {
    if [[ -n "$server_pid" ]]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    if [[ -n "$log_file" ]]; then
        rm -f -- "$log_file"
    fi
}
trap cleanup EXIT

run_server() {
    log_file=$(mktemp -t bessemer-m0.XXXXXX)
    "$binary" "$1" >"$log_file" 2>&1 &
    server_pid=$!
    ready=false
    for _ in $(seq 1 50); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
            cat "$log_file" >&2
            return 1
        fi
        if grep -Fq "mode=$1 address=127.0.0.1:8000" "$log_file" \
            && curl --silent --fail http://127.0.0.1:8000/ >/dev/null; then
            ready=true
            break
        fi
        sleep 0.1
    done
    if [[ "$ready" != true ]]; then
        echo "M0 $1 server did not become ready" >&2
        cat "$log_file" >&2
        return 1
    fi
    echo "=== $1 ==="
    oha -z "$duration" -c "$connections" -w -t 5s --http-version 1.1 --no-tui http://127.0.0.1:8000/
    cleanup
    server_pid=
    log_file=
}

run_server rust
run_server python
