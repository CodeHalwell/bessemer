#!/usr/bin/env bash
# Explicit opt-in host-wide affinity change for other same-user threads.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${HARNESS_PYTHON:-${PYTHON:-python3}}" "$here/cpu_isolation.py" "$@"
