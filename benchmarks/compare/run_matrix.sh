#!/usr/bin/env bash
# FastAPI-style features, one server core. See README.md for flags and packages.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${HARNESS_PYTHON:-${PYTHON:-python3}}" "$here/matrix.py" feature "$@"
