#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 host for the two-machine comparison.
#
#   bootstrap.sh server   build Bessemer, the FastAPI venv and the Axum binary
#   bootstrap.sh client   install oha
#
# Run from the unpacked repository root. Idempotent; safe to re-run.
set -euo pipefail

role=${1:?usage: bootstrap.sh server|client}
RUST_VERSION=${RUST_VERSION:-1.92.0}
OHA_VERSION=${OHA_VERSION:-1.16.0}
BENCH_HOME=${BENCH_HOME:-$HOME/bench}
mkdir -p "$BENCH_HOME"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential pkg-config python3 python3-venv python3-dev util-linux >/dev/null

if ! command -v cargo >/dev/null && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain "$RUST_VERSION"
fi
export PATH="$HOME/.cargo/bin:$PATH"

# Steady clocks and enough sockets for high connection counts.
if [ -d /sys/devices/system/cpu/cpu0/cpufreq ]; then
  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null || true
fi
sudo sysctl -q -w net.core.somaxconn=65535 net.ipv4.tcp_max_syn_backlog=65535 \
  net.ipv4.ip_local_port_range="1024 65000" net.ipv4.tcp_tw_reuse=1

case "$role" in
  server)
    python3 -m venv "$BENCH_HOME/venv"
    "$BENCH_HOME/venv/bin/pip" install -q --disable-pip-version-check \
      maturin==1.9.6 fastapi==0.121.3 'uvicorn[standard]==0.38.0' pydantic==2.14.2
    "$BENCH_HOME/venv/bin/maturin" build --release --locked --features extension-module \
      --interpreter "$BENCH_HOME/venv/bin/python" --out "$BENCH_HOME/wheels"
    "$BENCH_HOME/venv/bin/pip" install -q --force-reinstall --no-deps "$BENCH_HOME"/wheels/bessemer-*.whl
    (cd benchmarks/compare/axum_app && cargo build --release --locked -q)
    ;;
  client)
    if ! "$HOME/.cargo/bin/oha" --version 2>/dev/null | grep -q "$OHA_VERSION"; then
      cargo install oha --locked --version "$OHA_VERSION" -q
    fi
    ;;
  *)
    echo "unknown role $role" >&2
    exit 2
    ;;
esac
echo "bootstrap $role done"
