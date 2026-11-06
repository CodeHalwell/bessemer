#!/usr/bin/env bash
# Step 6: 2 server cores (CPUs 0-1), oha on CPUs 2-3, GET /json at 1024 connections, 2 runs each.
R=/home/user/bessemer/benchmarks/results/2026-11-06/reprofile
export PATH=$PATH:/usr/local/bin PYTHON=/home/user/venv/bin/python OHA=$HOME/.cargo/bin/oha CONNECTIONS=1024 SERVER_CPU=0-1 CLIENT_CPU=2-3
export PORT0=18800
p=18800
run() { # name which [ENV=VAL...]
  local name=$1 which=$2; shift 2
  for r in 1 2; do p=$((p+1))
    env "$@" PORT=$p WORKERS=2 AXUM_THREADS=2 bash $R/scripts/profile_route.sh $which /json $R/raw/multicore/$name/run$r 2>&1 | tail -1
    sleep 3
  done
}
run bessemer-rust bessemer BESSEMER_EVENT_LOOP=bessemer
run bessemer-asyncio bessemer BESSEMER_EVENT_LOOP=asyncio
run axum axum X=1
run bessemer-asyncio-max-in-flight-4096 bessemer BESSEMER_EVENT_LOOP=asyncio BESSEMER_MAX_IN_FLIGHT=4096
