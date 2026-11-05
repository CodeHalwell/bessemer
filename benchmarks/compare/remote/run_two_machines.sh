#!/usr/bin/env bash
# Run the comparison with the servers and the load generator on separate hosts.
#
#   run_two_machines.sh SERVER CLIENT SERVER_PRIVATE_IP OUT_DIR [run_compare args]
#
# SERVER and CLIENT are SSH destinations (user@host) for two Ubuntu 24.04
# hosts that can reach each other on SERVER_PRIVATE_IP. The script ships the
# current commit to both, bootstraps them in parallel, starts server_agent.py
# on the server, runs run_compare.py on the client and copies the results and
# a Markdown summary into OUT_DIR. Extra arguments go to run_compare.py.
#
# Set TRANSPORT=gcloud (with ZONE) to use `gcloud compute ssh/scp` with
# instance names instead of plain ssh/scp.
set -euo pipefail

server=${1:?usage: run_two_machines.sh SERVER CLIENT SERVER_PRIVATE_IP OUT_DIR [args]}
client=${2:?}
server_ip=${3:?}
out_dir=${4:?}
shift 4
TRANSPORT=${TRANSPORT:-ssh}
AGENT_PORT=${AGENT_PORT:-9100}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
commit=$(git -C "$repo" rev-parse --short HEAD)

remote() {  # remote HOST COMMAND
  if [ "$TRANSPORT" = gcloud ]; then
    gcloud compute ssh "$1" --zone "${ZONE:?set ZONE}" --quiet --command "$2"
  else
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$1" "$2"
  fi
}
copy() {  # copy SRC DEST (either side may be HOST:path)
  if [ "$TRANSPORT" = gcloud ]; then
    gcloud compute scp --zone "${ZONE:?set ZONE}" --quiet "$1" "$2"
  else
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$1" "$2"
  fi
}

if [ -n "$(git -C "$repo" status --porcelain)" ]; then
  echo "warning: uncommitted changes are not shipped; benchmarking $commit" >&2
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$repo" archive --format=tar.gz -o "$work/bessemer.tar.gz" HEAD

echo "== shipping $commit and bootstrapping both hosts"
for host in "$server" "$client"; do
  copy "$work/bessemer.tar.gz" "$host:bessemer.tar.gz"
  remote "$host" 'rm -rf ~/bessemer && mkdir ~/bessemer && tar -xzf ~/bessemer.tar.gz -C ~/bessemer'
done
remote "$server" 'cd ~/bessemer && benchmarks/compare/remote/bootstrap.sh server' >"$work/server.log" 2>&1 &
server_pid=$!
remote "$client" 'cd ~/bessemer && benchmarks/compare/remote/bootstrap.sh client' >"$work/client.log" 2>&1 &
client_pid=$!
wait "$server_pid" || { tail -50 "$work/server.log"; exit 1; }
wait "$client_pid" || { tail -50 "$work/client.log"; exit 1; }

token=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
echo "== starting the agent on $server_ip:$AGENT_PORT"
remote "$server" "pkill -f server_agent.py || true; cd ~/bessemer/benchmarks/compare && \
  ulimit -n 65535 && BENCH_AGENT_TOKEN=$token nohup ~/bench/venv/bin/python server_agent.py \
  --python ~/bench/venv/bin/python --axum axum_app/target/release/axum-compare \
  --listen $server_ip:$AGENT_PORT --bind $server_ip >~/agent.log 2>&1 </dev/null &"
trap 'remote "$server" "pkill -f server_agent.py" || true; rm -rf "$work"' EXIT

extra=""
for arg in "$@"; do extra+=" $(printf '%q' "$arg")"; done
echo "== running the comparison from the client"
remote "$client" "cd ~/bessemer/benchmarks/compare && ulimit -n 65535 && \
  BENCH_AGENT_TOKEN=$token python3 run_compare.py --oha ~/.cargo/bin/oha \
  --agent http://$server_ip:$AGENT_PORT --out ~/results.json$extra"

mkdir -p "$out_dir"
copy "$client:results.json" "$out_dir/raw.json"
python3 "$repo/benchmarks/compare/summarise.py" "$out_dir/raw.json" >"$out_dir/summary.md"
echo "== results in $out_dir (commit $commit)"
cat "$out_dir/summary.md"
