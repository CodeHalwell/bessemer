#!/usr/bin/env bash
# Step 5: instructions per request with benchmarks/profile/instructions.sh (unmodified), CONNECTIONS=1, 5000 requests.
R=/home/user/bessemer/benchmarks/results/2026-11-06/reprofile
export PATH=$PATH:/usr/local/bin PYTHON=/home/user/venv/bin/python OHA=$HOME/.cargo/bin/oha CONNECTIONS=1
S=/home/user/bessemer/benchmarks/profile/instructions.sh
p=18700
for loop in asyncio bessemer; do
  for r in json items echo; do
    p=$((p+1)); name=$loop-$r; out=$R/raw/callgrind/$name
    case $r in json) route=/json;; items) route='/items/7?q=abc';; echo) route=/echo;; esac
    if [[ $r == echo ]]; then export METHOD=POST BODY_FILE=$R/scripts/order.json; else unset METHOD BODY_FILE; fi
    BESSEMER_EVENT_LOOP=$loop OUT=$out PORT=$p bash $S "$route" 2>&1 | tail -2 | tee -a $R/raw/callgrind/summary.txt
  done
done
