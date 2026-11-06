#!/usr/bin/env bash
# perf record (64 connections, branch Rust loop, /json), same method as the reprofile's run_perf.sh.
R=/home/user/bessemer/benchmarks/results/2026-11-06/final-pass
export PATH=$PATH:/usr/local/bin OHA=$HOME/.cargo/bin/oha CONNECTIONS=64 SERVER_CPU=0 CLIENT_CPU=1-3 PERF_RECORD=1
export PYTHON=/home/user/venvs/integrated/bin/python BESSEMER_EVENT_LOOP=bessemer PYTHONPERFSUPPORT=1 PORT=18399
rm -f /tmp/perf-*.map
mkdir -p $R/raw/perf/branch-rust-json
cat /proc/loadavg > $R/raw/perf/branch-rust-json/loadavg.txt
bash $R/scripts/profile_route.sh bessemer /json $R/raw/perf/branch-rust-json 2>&1 | tail -1
