#!/usr/bin/env bash
# Step 3: 64-connection profile runs with PYTHONPERFSUPPORT=1 and perf record (oha on CPUs 1-3).
R=/home/user/bessemer/benchmarks/results/2026-11-06/reprofile
export PATH=$PATH:/usr/local/bin PYTHON=/home/user/venv/bin/python OHA=$HOME/.cargo/bin/oha CONNECTIONS=64 SERVER_CPU=0 CLIENT_CPU=1-3 PERF_RECORD=1
n=18300
run() { # name which route loop [post]
  local name=$1 which=$2 route=$3 loop=$4 post=${5:-}
  n=$((n+1)); rm -f /tmp/perf-*.map
  if [[ $post == post ]]; then export METHOD=POST BODY_FILE=$R/scripts/order.json; else unset METHOD BODY_FILE; fi
  if [[ $which == bessemer ]]; then export BESSEMER_EVENT_LOOP=$loop PYTHONPERFSUPPORT=1; else unset BESSEMER_EVENT_LOOP PYTHONPERFSUPPORT; fi
  PORT=$n bash $R/scripts/profile_route.sh $which "$route" $R/raw/perf/$name 2>&1 | tail -1
  sleep 3
}
run bessemer-asyncio-json bessemer /json asyncio
run bessemer-asyncio-echo bessemer /echo asyncio post
run bessemer-rust-json bessemer /json bessemer
run bessemer-rust-echo bessemer /echo bessemer post
run axum-json axum /json x
