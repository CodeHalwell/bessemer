#!/usr/bin/env bash
# One host: the server on CPUs 0-2, oha on CPU 3, 64 connections, 10 s per run.
# Usage: run.sh PYTHON OHA > raw.txt   (PYTHON is a free-threaded 3.14t)
set -euo pipefail
python=$1
oha=$2
apps=$(cd "$(dirname "$0")/../../../compare" && pwd)
body=$("$python" -c 'import json; print(json.dumps({"items": [{"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]} for i in range(20)]}))')
for round in 1 2 3; do
  for setup in "1 1" "1 3" "3 1"; do
    read -r workers loops <<<"$setup"
    port=$((20000 + RANDOM % 20000))
    (cd "$apps" && exec taskset -c 0-2 "$python" -m bessemer run bessemer_app:app \
      --port "$port" --workers "$workers" --loops "$loops") >/dev/null 2>&1 &
    server=$!
    until curl -fs "http://127.0.0.1:$port/healthz" >/dev/null; do sleep 0.1; done
    for route in json items echo io; do
      case $route in
        items) url="/items/7?q=hello"; extra=() ;;
        echo) url="/echo"; extra=(-m POST -H "content-type: application/json" -d "$body") ;;
        *) url="/$route"; extra=() ;;
      esac
      taskset -c 3 "$oha" --no-tui -z 3s -c 64 "${extra[@]}" "http://127.0.0.1:$port$url" >/dev/null
      rps=$(taskset -c 3 "$oha" --no-tui --output-format json -z 10s -c 64 "${extra[@]}" "http://127.0.0.1:$port$url" |
        "$python" -c 'import json,sys; d=json.load(sys.stdin); bad=sum(v for k,v in d.get("statusCodeDistribution",{}).items() if k!="200"); print(round(d["summary"]["requestsPerSec"]), "non200=%d" % bad)')
      echo "round=$round workers=$workers loops=$loops route=$route rps=$rps"
    done
    kill -TERM "$server"; wait "$server" || true
  done
done
