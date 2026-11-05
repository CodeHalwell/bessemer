#!/usr/bin/env bash
set -euo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$repository"

python_command=${PYTHON:-python3}
python_executable=$("$python_command" -c 'import sys; print(sys.executable)')
export PYO3_PYTHON="$python_executable"
export PYTHONDONTWRITEBYTECODE=1

# Relocated Python distributions can retain the build machine's LIBDIR in
# sysconfig. Give the compiler and test loader the shared library's real path.
python_library_path=$("$python_executable" - <<'PY'
import os
import sys
import sysconfig
from pathlib import Path

candidates = [
    Path(sys.base_prefix) / "lib",
    Path(sys.executable).resolve().parent.parent / "lib",
]
configured = sysconfig.get_config_var("LIBDIR")
if configured:
    candidates.append(Path(configured))
libraries = []
for directory in candidates:
    if directory.is_dir() and any(directory.glob("libpython*")):
        resolved = str(directory.resolve())
        if resolved not in libraries:
            libraries.append(resolved)
print(os.pathsep.join(libraries))
PY
)
if [[ -n "$python_library_path" ]]; then
    export LIBRARY_PATH="$python_library_path${LIBRARY_PATH:+:$LIBRARY_PATH}"
    export LD_LIBRARY_PATH="$python_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export DYLD_FALLBACK_LIBRARY_PATH="$python_library_path${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi

# Installed wheels are checked as installed. A source-only checkout can still
# run Python API tests before building its extension.
if ! "$python_executable" -c 'import bessemer' >/dev/null 2>&1; then
    export PYTHONPATH="$repository/python${PYTHONPATH:+:$PYTHONPATH}"
fi
if [[ "${BESSEMER_REQUIRE_NATIVE:-0}" == 1 ]]; then
    "$python_executable" -c 'import bessemer._native'
fi

cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --all-targets --locked
"$python_executable" -m ruff check python tests examples benchmarks scripts deploy crates/bessemer-loop
"$python_executable" -m ruff format --check python tests examples benchmarks scripts deploy crates/bessemer-loop
"$python_executable" -m unittest discover -s tests/python -p 'test_*.py' -v
"$python_executable" -m unittest discover -s benchmarks -p 'test_*.py' -v

shopt -s nullglob
for script in scripts/*.sh benchmarks/*.sh; do
    bash -n "$script"
done
