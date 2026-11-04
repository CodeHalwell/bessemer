#!/usr/bin/env bash
# Audit the locked Rust graph and the installed Python runtime dependency graph.
# Install audit tools separately: pip-audit==2.10.1 and cargo-audit==0.22.2.
# PIP_AUDIT_PYTHON selects the scanner environment; BESSEMER_AUDIT_PYTHON selects
# the installed wheel environment. CARGO_AUDIT_BIN selects the pinned executable.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_directory="${1:-${project_root}/target/security}"
audit_python="${PIP_AUDIT_PYTHON:-python3}"
runtime_python="${BESSEMER_AUDIT_PYTHON:-python3}"
cargo_audit="${CARGO_AUDIT_BIN:-cargo-audit}"
mkdir -p -- "$output_directory"
output_directory="$(cd -- "$output_directory" && pwd)"

"$audit_python" - <<'PY'
import importlib.metadata
if importlib.metadata.version("pip-audit") != "2.10.1":
    raise SystemExit("security-check requires pip-audit==2.10.1 in PIP_AUDIT_PYTHON")
PY
if [[ "$("$cargo_audit" --version)" != "cargo-audit 0.22.2" ]]; then
    printf '%s\n' 'security-check requires cargo-audit==0.22.2 in CARGO_AUDIT_BIN' >&2
    exit 2
fi

"$runtime_python" - > "$output_directory/python-installed-metadata.json" <<'PY'
import importlib.metadata as metadata
import json
import os
import platform
import re
import sys

packages = {}
queue = ["bessemer", "pydantic", "maturin"]
while queue:
    name = re.sub(r"[-_.]+", "-", queue.pop()).lower()
    if name in packages:
        continue
    try:
        package = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        packages[name] = None
        continue
    requirements = package.requires or []
    packages[name] = {"version": package.version, "requires": requirements}
    queue.extend(re.match(r"[A-Za-z0-9_.-]+", value).group() for value in requirements)
if packages["bessemer"] is None:
    raise SystemExit("install the Bessemer wheel in BESSEMER_AUDIT_PYTHON before auditing")
environment = {
    "os_name": os.name,
    "sys_platform": sys.platform,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "implementation_name": sys.implementation.name,
    "implementation_version": platform.python_version(),
}
print(json.dumps({"packages": packages, "environment": environment}, indent=2))
PY

"$audit_python" - "$output_directory" "$project_root/Cargo.lock" <<'PY'
import datetime
import hashlib
import json
import sys
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

output = Path(sys.argv[1])
lockfile = Path(sys.argv[2])
lock_bytes = lockfile.read_bytes()
installed = json.loads((output / "python-installed-metadata.json").read_text())
packages = installed["packages"]
environment = dict(installed["environment"], extra="")

def closure(seeds):
    found = {}
    queue = list(seeds)
    while queue:
        name = canonicalize_name(queue.pop())
        if name in found:
            continue
        package = packages.get(name)
        if package is None:
            raise SystemExit(f"required runtime distribution is missing: {name}")
        found[name] = package["version"]
        for raw in package["requires"]:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate(environment):
                queue.append(requirement.name)
    return dict(sorted(found.items()))

core = closure(["bessemer"])
core.pop("bessemer")  # Unpublished local wheel; Rust graph is audited separately.
runtime = dict(core)
if packages.get("pydantic") is not None:
    runtime.update(closure(["pydantic"]))
build = closure(["maturin"]) if packages.get("maturin") is not None else {}
for name, graph in (("python-runtime", runtime), ("python-build", build)):
    (output / f"{name}.txt").write_text("".join(f"{key}=={value}\n" for key, value in sorted(graph.items())))
provenance = {
    "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "cargo_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
    "bessemer_version": packages["bessemer"]["version"],
    "pip_audit_version": "2.10.1",
    "cargo_audit_version": "0.22.2",
    "python_environment": installed["environment"],
    "python_core_runtime": core,
    "python_optional_pydantic_installed": packages.get("pydantic") is not None,
    "python_build_tools_installed": bool(build),
    "scope": "Installed Bessemer core and Pydantic optional runtime closure; installed maturin build closure; Cargo.lock including target-specific dependencies. Unrelated system/scanner packages excluded.",
}
(output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
(output / "Cargo.lock.audited").write_bytes(lock_bytes)
PY

audit_status=0
"$cargo_audit" audit --db "$output_directory/advisory-db" --file "$output_directory/Cargo.lock.audited" --format json > "$output_directory/rust-advisories.json" || audit_status=1
for scope in runtime build; do
    requirements="$output_directory/python-${scope}.txt"
    if [[ -s "$requirements" ]]; then
        "$audit_python" -m pip_audit --requirement "$requirements" --no-deps --disable-pip --progress-spinner off --format json --output "$output_directory/python-${scope}-advisories.json" || audit_status=1
    else
        printf '%s\n' '{"dependencies":[],"fixes":[]}' > "$output_directory/python-${scope}-advisories.json"
    fi
done
printf 'Security audit reports: %s\n' "$output_directory"
exit "$audit_status"
