#!/usr/bin/env bash
# Instructions per request with benchmarks/profile/instructions.sh, CONNECTIONS=1, 5000 requests
# (the same count for every config), for main (7475503 wheel) and the branch wheel.
R=/home/user/bessemer/benchmarks/results/2026-11-06/final-pass
export PATH=$PATH:/usr/local/bin OHA=$HOME/.cargo/bin/oha CONNECTIONS=1
S=/home/user/bessemer/benchmarks/profile/instructions.sh
p=18700
for cfg in main-asyncio main-rust branch-asyncio branch-rust; do
  case $cfg in main-*) export PYTHON=/home/user/venv/bin/python;; *) export PYTHON=/home/user/venvs/integrated/bin/python;; esac
  case $cfg in *-asyncio) loop=asyncio;; *) loop=bessemer;; esac
  for r in json items echo; do
    p=$((p+1)); name=$cfg-$r; out=$R/raw/callgrind/$name
    case $r in json) route=/json;; items) route='/items/7?q=abc';; echo) route=/echo;; esac
    if [[ $r == echo ]]; then export METHOD=POST BODY_FILE=$R/scripts/order.json; else unset METHOD BODY_FILE; fi
    cat /proc/loadavg > "$R/raw/callgrind-loadavg-$name.txt"
    BESSEMER_EVENT_LOOP=$loop OUT=$out PORT=$p bash $S "$route" 5000 2>&1 | tail -2 | sed "s|^|$name: |" | tee -a $R/raw/callgrind/summary.txt
  done
done
