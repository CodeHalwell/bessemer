#!/usr/bin/env bash
# 2 server cores (CPUs 0-1), oha on CPUs 2-3, GET /json at 1024 connections, 2 runs each, alternating order.
R=/home/user/bessemer/benchmarks/results/2026-11-06/final-pass
export PATH=$PATH:/usr/local/bin OHA=$HOME/.cargo/bin/oha CONNECTIONS=1024 SERVER_CPU=0-1 CLIENT_CPU=2-3
p=18800
run() { # name which python [ENV=VAL...]
  local name=$1 which=$2 py=$3 r=$4; shift 4
  p=$((p+1)); mkdir -p $R/raw/multicore/$name/run$r; cat /proc/loadavg > $R/raw/multicore/$name/run$r/loadavg.txt
  env "$@" PYTHON=$py PORT=$p WORKERS=2 AXUM_THREADS=2 bash $R/scripts/profile_route.sh $which /json $R/raw/multicore/$name/run$r 2>&1 | tail -1
  sleep 3
}
M=/home/user/venv/bin/python
B=/home/user/venvs/integrated/bin/python
for r in 1 2; do
  if [[ $r == 1 ]]; then order="main branch axum"; else order="axum branch main"; fi
  for c in $order; do
    case $c in
      main) run main-default bessemer $M $r BESSEMER_EVENT_LOOP=asyncio;;
      branch) run branch-default bessemer $B $r BESSEMER_EVENT_LOOP=auto;;
      axum) run axum axum $M $r X=1;;
    esac
  done
done
