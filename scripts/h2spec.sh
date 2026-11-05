#!/usr/bin/env bash
# HTTP/2 conformance with h2spec, over TLS (ALPN) and plaintext (prior
# knowledge). Needs an installed bessemer, openssl and docker.
set -euo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python_command=${PYTHON:-python3}
image=${H2SPEC_IMAGE:-summerwind/h2spec:2.6.0}
reports=${H2SPEC_REPORTS:-$repository/target/h2spec}
work=$(mktemp -d)
pids=()
cleanup() {
    for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    rm -rf -- "$work"
}
trap cleanup EXIT
mkdir -p "$reports"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -keyout "$work/key.pem" -out "$work/cert.pem" \
    -subj /CN=localhost -addext subjectAltName=DNS:localhost >/dev/null 2>&1

"$python_command" "$repository/scripts/h2spec_app.py" 18443 "$work/cert.pem" "$work/key.pem" &
pids+=($!)
"$python_command" "$repository/scripts/h2spec_app.py" 18080 &
pids+=($!)

for port in 18443 18080; do
    for _ in $(seq 100); do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            break
        fi
        sleep 0.1
    done
done

status=0
echo "h2spec over TLS (ALPN h2)"
docker run --rm --network host -v "$reports:/reports" "$image" \
    -h 127.0.0.1 -p 18443 -t -k -o 5 -j /reports/tls.xml || status=1
echo "h2spec over plaintext (prior knowledge)"
docker run --rm --network host -v "$reports:/reports" "$image" \
    -h 127.0.0.1 -p 18080 -o 5 -j /reports/plaintext.xml || status=1
exit "$status"
