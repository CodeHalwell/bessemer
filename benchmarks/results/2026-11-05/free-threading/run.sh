#!/bin/bash
# usage: run.sh python path label
PY=$1; P=$2; L=$3; PORT=$((20000 + RANDOM % 20000))
cd /tmp/claude-0/bench
taskset -c 0-2 $PY -m bessemer run ftapp:app --host 127.0.0.1 --port $PORT --workers 1 >/dev/null 2>/tmp/claude-0/bench/srv-$L.log &
SRV=$!
for i in $(seq 100); do curl -s -o /dev/null http://127.0.0.1:$PORT$P && break; sleep 0.1; done
taskset -c 3 /tmp/claude-0/oha/bin/oha -z 3s -c 32 --no-tui http://127.0.0.1:$PORT$P >/dev/null
for r in 1 2 3; do
 taskset -c 3 /tmp/claude-0/oha/bin/oha -z 8s -c 32 --no-tui --output-format json http://127.0.0.1:$PORT$P | python3 -c "import json,sys;d=json.load(sys.stdin);print('$L $P rps=%.0f ok=%s'%(d['summary']['requestsPerSec'],d['statusCodeDistribution']))"
done
kill -INT $SRV; wait $SRV 2>/dev/null
