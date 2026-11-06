#!/usr/bin/env bash
# Step 4 (supplement, no so Python frames and idle detection are py-spy default): py-spy --native on the bessemer worker, 64 connections, oha on CPUs 1-3, server on CPU 0, py-spy on CPU 2.
R=/home/user/bessemer/benchmarks/results/2026-11-06/reprofile
export PATH=$PATH:/usr/local/bin
PY=/home/user/venv/bin/python; OHA=$HOME/.cargo/bin/oha; SPY=/home/user/venv/bin/py-spy
n=18500
one() { # name loop route [post]
  local name=$1 loop=$2 route=$3 post=${4:-} out=$R/raw/pyspy/$1; n=$((n+1)); mkdir -p $out
  local req=(-m GET); [[ $post == post ]] && req=(-m POST -D $R/scripts/order.json -T application/json)
  cd /home/user/bessemer/benchmarks/compare
  BESSEMER_EVENT_LOOP=$loop taskset -c 0 $PY -m bessemer run bessemer_app:app --host 127.0.0.1 --port $n --workers 1 >$out/server.log 2>&1 &
  local srv=$!
  for _ in $(seq 100); do curl -sf http://127.0.0.1:$n/json >/dev/null && break; sleep 0.2; done
  local wpid=$(ps -o pid= --ppid $srv | tr -d ' ')
  taskset -c 1-3 $OHA -z 16s -c 64 --no-tui "${req[@]}" http://127.0.0.1:$n$route >$out/oha-during-pyspy-nonnative.txt 2>&1 &
  local load=$!
  sleep 4
  taskset -c 2 $SPY record --rate 2000 -d 8 -f raw -p $wpid -o $out/folded-nonnative.txt >$out/py-spy-nonnative.log 2>&1
  echo "$name worker=$wpid rc=$?" | tee -a $out/py-spy-nonnative.log
  wait $load
  kill $srv; wait $srv 2>/dev/null; sleep 2
}
one asyncio-json asyncio /json
one asyncio-echo asyncio /echo post
one rust-json bessemer /json
one rust-echo bessemer /echo post
