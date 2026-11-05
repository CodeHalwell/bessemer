#!/usr/bin/env bash
# Two-machine comparison on Google Cloud.
#
#   gcp.sh up      create the server and client VMs in one zone
#   gcp.sh run     bootstrap both and run the comparison (see run_two_machines.sh)
#   gcp.sh down    delete both VMs and the placement policy
#   gcp.sh all     up, run, then down even if the run fails
#
# Needs an authenticated gcloud with a default project (or PROJECT set) and
# the default VPC, whose default-allow-internal rule lets the VMs talk.
#
# Defaults: the server is a c3-standard-16 with SMT off (8 physical cores, so
# configs go up to 8-core); the client is a c3-highcpu-44 so the load
# generator has several times the server's cores. Both sit in one compact
# placement group. Roughly US$3/hour together on demand; a default run takes
# about 45 minutes including the build.
set -euo pipefail

ZONE=${ZONE:-europe-west2-a}
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
PREFIX=${PREFIX:-bessemer-bench}
SERVER_TYPE=${SERVER_TYPE:-c3-standard-16}
CLIENT_TYPE=${CLIENT_TYPE:-c3-highcpu-44}
SERVER_THREADS_PER_CORE=${SERVER_THREADS_PER_CORE:-1}
CONFIGS=${CONFIGS:-1-core,2-core,4-core,8-core}
CONNECTIONS=${CONNECTIONS:-256}
OUT_DIR=${OUT_DIR:-$(git rev-parse --show-toplevel)/benchmarks/results/$(date -u +%F)/compare-two-machine}
region=${ZONE%-*}
server=$PREFIX-server
client=$PREFIX-client
policy=$PREFIX-placement
gc() { gcloud --project "$PROJECT" "$@"; }

up() {
  if ! gc compute resource-policies describe "$policy" --region "$region" >/dev/null 2>&1; then
    gc compute resource-policies create group-placement "$policy" \
      --region "$region" --collocation collocated
  fi
  common=(--zone "$ZONE" --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud
    --boot-disk-size 50GB --boot-disk-type pd-balanced --resource-policies "$policy"
    --maintenance-policy TERMINATE --labels purpose=bessemer-bench)
  gc compute instances create "$server" "${common[@]}" --machine-type "$SERVER_TYPE" \
    --threads-per-core "$SERVER_THREADS_PER_CORE"
  gc compute instances create "$client" "${common[@]}" --machine-type "$CLIENT_TYPE"
  echo "waiting for SSH"
  for host in "$server" "$client"; do
    for _ in $(seq 30); do
      gc compute ssh "$host" --zone "$ZONE" --quiet --command true >/dev/null 2>&1 && break
      sleep 10
    done
  done
}

run() {
  server_ip=$(gc compute instances describe "$server" --zone "$ZONE" \
    --format 'value(networkInterfaces[0].networkIP)')
  CLOUDSDK_CORE_PROJECT=$PROJECT TRANSPORT=gcloud ZONE=$ZONE \
    "$(dirname "$0")/run_two_machines.sh" "$server" "$client" "$server_ip" "$OUT_DIR" \
    --configs "$CONFIGS" --connections "$CONNECTIONS" "$@"
}

down() {
  gc compute instances delete "$server" "$client" --zone "$ZONE" --quiet || true
  gc compute resource-policies delete "$policy" --region "$region" --quiet || true
}

action=${1:-}
shift || true
case "$action" in
  up) up ;;
  run) run "$@" ;;
  down) down ;;
  all)
    trap down EXIT
    up
    run "$@"
    ;;
  *)
    echo "usage: gcp.sh up|run|down|all [run_compare args]" >&2
    exit 2
    ;;
esac
