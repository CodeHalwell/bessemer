"""Qualify the GCP comparison of main and the same-toolchain RC10 build.

Reads the public CSV and build provenance and verifies the benchmark source
against the patched checkout before qualifying observations and summarizing them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import tomllib
from collections import Counter, defaultdict
from ipaddress import IPv4Address
from itertools import product
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
MAIN = "678e3184f6280eae287a5e19616f7747e9ef20f1"
RC10 = "687e70739416d0cca7b4c64de6d4272edf03b101"
COMMITS = {"main": MAIN, "rc10": RC10}
BUILD_ROLES = {"bessemer-auto": "main", "bessemer-rc10": "rc10"}
ROLES = (*BUILD_ROLES, "fastapi-standard", "axum")
LABELS = ("Bessemer main", "Bessemer RC10", "FastAPI", "Rust Axum")
MODES = {"standard": "3.14.7", "free-threaded": "3.14.7t"}
CONFIGS = ("1-core", "4-core")
ROUTES = {
    "json": ("/json", "GET", 22),
    "items": ("/items/7?q=abc", "GET", 18),
    "echo": ("/echo", "POST", 1133),
    "cpu-inline": ("/cpu-inline", "GET", 19),
    "cpu-sync": ("/cpu-sync", "GET", 19),
    "large-echo": ("/echo", "POST", 60403),
    "bytes": ("/bytes", "GET", 262144),
    "models256": ("/models256", "GET", 14885),
}
OVERRIDES = dict.fromkeys(("cpu-inline", "cpu-sync", "large-echo", "bytes"), 16)
LARGE_ECHO = {
    "bytes": 69403,
    "sha256": "4bb4a57404b9fd07a74eaa6cc13e930a4f9b6bc965f252599f07a951c1185ffc",
}
VERSIONS = {
    "bessemer": "1.0.0rc10",
    "fastapi": "0.165.0",
    "uvicorn": "0.54.0",
    "uvloop": "0.23.0",
    "httptools": "0.9.0",
    "pydantic": "2.14.4",
}
HOST_FIELDS = ("kernel", "cpu_model", "cpus")
PAIRS = {
    "main_rc10": ("bessemer-auto", "bessemer-rc10"),
    "main_fastapi": ("bessemer-auto", "fastapi-standard"),
    "main_axum": ("bessemer-auto", "axum"),
    "rc10_fastapi": ("bessemer-rc10", "fastapi-standard"),
    "rc10_axum": ("bessemer-rc10", "axum"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text())


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def check_source(source, repo):
    require(
        source.get("source_commit", source.get("release_commit")) == MAIN,
        "unexpected harness commit",
    )
    require(is_sha256(source.get("bundle_sha256")), "missing benchmark bundle SHA-256")
    files = source["files"]
    required_files = {
        "benchmarks/compare/remote/bootstrap.sh",
        "benchmarks/compare/axum_app/Cargo.lock",
        "benchmarks/compare/axum_app/src/main.rs",
        "benchmarks/compare/bessemer_app.py",
        "benchmarks/compare/fastapi_app.py",
        "benchmarks/compare/run_compare.py",
        "benchmarks/compare/measurement.py",
        "benchmarks/compare/servers.py",
        "benchmarks/compare/server_agent.py",
    }
    require(required_files <= files.keys(), "incomplete harness file manifest")
    for name, expected in files.items():
        path = (repo / name).resolve()
        require(
            path.is_relative_to(repo)
            and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
            f"benchmark source hash mismatch for {name}",
        )


def runtime_matches(runtime, free, worker=False):
    return (
        isinstance(runtime, dict)
        and runtime.get("gil_enabled") is (not free)
        and runtime.get("free_threaded") is free
        and runtime.get("python_version", runtime.get("python", "3.14.7")) == "3.14.7"
        and runtime.get("jit_enabled", False) is False
        and (not worker or (type(runtime.get("pid")) is int and runtime["pid"] > 0))
    )


def read_builds(receipt, source, mode):
    require(receipt["python"] == MODES[mode], "build receipt interpreter mismatch")
    require(
        receipt["rustc"].split()[:2] == ["rustc", "1.92.0"], "unexpected Rust compiler"
    )
    require(
        receipt["maturin"].split()[:2] == ["maturin", "1.16.0"], "unexpected maturin"
    )
    require(is_sha256(receipt.get("axum_sha256")), "missing native Axum SHA-256")
    require(
        receipt["axum_lock_sha256"]
        == source["files"]["benchmarks/compare/axum_app/Cargo.lock"],
        "Axum Cargo.lock hash differs from the benchmark source",
    )
    builds = {}
    free = mode == "free-threaded"
    abi = "cp314t" if free else "cp314"
    for role, commit in COMMITS.items():
        built = receipt["roles"][role]
        packed = source["build_sources"][role]
        require(
            built["commit"] == packed["commit"] == commit,
            f"{role} build commit mismatch",
        )
        require(
            is_sha256(built["source_sha256"])
            and built["source_sha256"] == packed["source_sha256"],
            f"{role} source archive hash mismatch",
        )
        require(
            is_sha256(built["wheel_sha256"]) and is_sha256(built["native_sha256"]),
            f"{role} missing wheel or installed extension hash",
        )
        require(
            built["wheel_filename"].endswith("x86_64.whl")
            and f"-cp314-{abi}-" in built["wheel_filename"],
            f"{role} unexpected wheel filename or ABI",
        )
        require(
            built["python_version"] == "3.14.7" and runtime_matches(built, free),
            f"{role} unexpected interpreter or GIL state in the build receipt",
        )
        pinned = VERSIONS | {"pydantic-core": "2.50.5"}
        require(
            all(
                built["versions"].get(name) == version
                for name, version in pinned.items()
            ),
            f"{role} pinned Python dependency mismatch",
        )
        builds[role] = {
            key: built[key]
            for key in (
                "commit",
                "source_sha256",
                "wheel_sha256",
                "wheel_filename",
                "native_sha256",
                "versions",
            )
        }
    require(
        builds["main"]["versions"] == builds["rc10"]["versions"],
        "main and RC10 Python package sets differ",
    )
    for field in ("source_sha256", "wheel_sha256", "native_sha256"):
        require(
            builds["main"][field] != builds["rc10"][field],
            f"main and RC10 report the same {field}",
        )
    return builds, {
        key: receipt[key]
        for key in ("rustc", "maturin", "axum_sha256", "axum_lock_sha256")
    }


def check_dependencies(dependencies, receipt):
    environments = [dependencies[role] for role in COMMITS]
    require(environments[0] == environments[1], "main and RC10 dependency sets differ")
    packages = environments[0]
    pins = VERSIONS | {"pydantic-core": "2.50.5"}
    for name, version in pins.items():
        require(packages.get(name) == version, f"wrong {name} pin")
    for role in COMMITS:
        require(
            all(
                packages.get(name) == version
                for name, version in receipt["roles"][role]["versions"].items()
            ),
            f"{role} build receipt differs from package freeze",
        )
    return {name: version for name, version in packages.items() if name != "bessemer"}


# CSV fields preserve the observed counters and probe results. A worker PID is
# exported only as a presence flag. The HTTP target is a documentation address.
PHASE_FIELDS = {
    "req_s": "rps",
    "successful_req_s": "ok_rps",
    "elapsed_s": "elapsed_seconds",
    "successful": "successful_responses",
    "completed": "completed_responses",
    "response_bytes": "response_body_bytes",
    "p50_ms": "p50_ms",
    "p99_ms": "p99_ms",
    "non_200": "non_200",
    "transport_errors": "errors",
    "http_statuses": "status_codes",
    "transport_types": "error_distribution",
    "command": "command",
}
CSV_FIELDS = {
    "connections": "connections",
    "eligible": "eligible",
    "exclusion_reasons": "eligibility_reasons",
    "successful_req_s": "ok_rps",
    "p50_ms": "p50_ms",
    "p99_ms": "p99_ms",
    "server_cpu_fraction": "server_cpu",
    "client_cpu_fraction": "client_cpu",
    "client_bound": "client_bound",
    "expected_reply_bytes": ("body_before", "bytes"),
    "body_bytes_after": ("body_after", "bytes"),
    "body_sha256_before": ("body_before", "sha256"),
    "body_sha256_after": ("body_after", "sha256"),
}
for phase in ("before", "after"):
    for key in ("gil_enabled", "free_threaded", "native_sha256", "pid_observed"):
        CSV_FIELDS[f"{key}_{phase}"] = (f"runtime_{phase}", key)
for label, key in PHASE_FIELDS.items():
    if label not in CSV_FIELDS:
        CSV_FIELDS[f"timed_{label}"] = key
    CSV_FIELDS[f"warmup_{label}"] = ("warmup", key)

CSV_KEYS = ("stage", "python_mode", "cores", "round", "framework", "workload")
STAGES = ("smoke", "measured")


def read_public(directory):
    provenance = read_json(directory / "provenance.json")
    series = {(r["stage"], r["python_mode"]): r for r in provenance["series"]}
    require(
        len(provenance["series"]) == len(series)
        and series.keys() == set(product(STAGES, MODES)),
        "unexpected series in build provenance",
    )
    grouped = {key: [] for key in series}
    samples = directory / "samples.csv"
    hashes = provenance.get("published_artifact_sha256", {})
    if "samples.csv" in hashes:
        require(
            hashlib.sha256(samples.read_bytes()).hexdigest() == hashes["samples.csv"],
            "sample archive hash mismatch",
        )
    with samples.open(newline="") as handle:
        reader = csv.DictReader(handle)
        require(
            reader.fieldnames == list(CSV_KEYS) + list(CSV_FIELDS),
            "unexpected sample columns",
        )
        for record in reader:
            stage, mode = record["stage"], record["python_mode"]
            row = {
                "config": f"{int(record['cores'])}-core",
                "round": int(record["round"]),
                "framework": record["framework"],
                "route": record["workload"],
            }
            for column, field in CSV_FIELDS.items():
                cell = record[column]
                if cell == "":
                    continue
                value = json.loads(cell)
                if isinstance(field, str):
                    row[field] = value
                    continue
                parent, key = field
                nested = row.setdefault(parent, {})
                if key == "pid_observed":
                    require(type(value) is bool, "invalid worker PID presence flag")
                    if value:
                        nested["pid"] = 1
                else:
                    nested[key] = value
            grouped[(stage, mode)].append(row)
    return provenance, series, grouped


def command_matches(sample, route, seconds, connections):
    expected_path, method, _ = ROUTES[route]
    args = sample.get("command") or []
    try:
        target = urlsplit(args[-1])
        address = IPv4Address(target.hostname)
        path = target.path + ("?" + target.query if target.query else "")
        return (
            str(args[args.index("-z") + 1]) == f"{seconds}s"
            and int(args[args.index("-c") + 1]) == connections
            and args[args.index("-m") + 1] == method
            and path == expected_path
            and target.scheme == "http"
            and address.is_private
            and not address.is_loopback
            and "--wait-ongoing-requests-after-deadline" in args
            and "--disable-compression" in args
        )
    except (IndexError, ValueError, TypeError):
        return False


def qualify_sample(row, route, role, settings, free, builds, eligibility_reasons):
    connections = settings["route_connections"].get(route, settings["connections"])
    size = ROUTES[route][2]
    reasons = eligibility_reasons(row)
    if row.get("eligible") is not True:
        reasons.append("runner did not qualify sample")
    if row.get("connections") != connections:
        reasons.append("recorded connections differ from the workload")
    cpu = row.get("client_cpu")
    if isinstance(cpu, (int, float)) and cpu > 0.85:
        reasons.append("client CPU exceeded qualification limit")
    before = row.get("body_before") or {}
    if (
        before.get("bytes") != size
        or not is_sha256(before.get("sha256"))
        or before != row.get("body_after")
    ):
        reasons.append("response probe body size or digest mismatch")
    if role != "axum":
        for phase in ("before", "after"):
            observed = row.get(f"runtime_{phase}")
            if not runtime_matches(observed, free, worker=True):
                reasons.append(
                    f"worker {phase}: missing or wrong interpreter, GIL or PID"
                )
            elif (
                role in BUILD_ROLES
                and observed.get("native_sha256")
                != builds[BUILD_ROLES[role]]["native_sha256"]
            ):
                reasons.append(
                    f"worker {phase}: wrong installed Bessemer extension hash"
                )
    for phase, sample, seconds in (
        ("warmup", row.get("warmup") or {}, settings["warmup"]),
        ("timed", row, settings["duration"]),
    ):
        if not command_matches(sample, route, seconds, connections):
            reasons.append(
                f"{phase}: command differs from workload, deadline or private path"
            )
        successful = sample.get("successful_responses")
        if (
            type(successful) is not int
            or sample.get("response_body_bytes") != successful * size
        ):
            reasons.append(f"{phase}: aggregate response bytes mismatch")
    for field in ("p50_ms", "p99_ms"):
        value = row.get(field)
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            reasons.append(f"missing {field}")
    return list(dict.fromkeys(reasons))


def load_mode(raw, source, receipt, dependencies, mode, stage, eligibility_reasons):
    builds, toolchain = read_builds(receipt, source, mode)
    smoke = stage == "smoke"
    configs = ("1-core",) if smoke else CONFIGS
    meta = raw["meta"]
    server, client = meta["server"], meta["client"]
    settings = meta["settings"]
    free = mode == "free-threaded"
    require(
        meta["mode"] == "two-machine" and client["hostname"] != server["hostname"],
        f"{mode}: a dedicated load generator is required",
    )
    require(
        client["oha"] == "oha 1.16.0" and runtime_matches(server, free),
        f"{mode}: unexpected oha or Python runtime",
    )
    for field in ("versions", "baseline_versions"):
        expected = VERSIONS | {"python": "3.14.7"}
        require(
            all(
                server[field].get(name) == version for name, version in expected.items()
            ),
            f"{mode}: live {field} mismatch",
        )
    for framework, role in BUILD_ROLES.items():
        artifacts = server["native_artifacts"]
        require(
            artifacts.get(framework, artifacts.get(role))
            == builds[role]["native_sha256"],
            f"{mode}: {framework} native artifact identity mismatch",
        )
    required = {
        "configs": ",".join(configs),
        "routes": ",".join(ROUTES),
        "frameworks": ",".join(ROLES),
        "duration": 3 if smoke else 5,
        "warmup": 1 if smoke else 2,
        "connections": 4 if smoke else 64,
        "route_connections": {} if smoke else OVERRIDES,
        "check_bodies": True,
        "large_echo_input": LARGE_ECHO,
    }
    require(
        all(settings.get(key) == value for key, value in required.items()),
        f"{mode}: settings differ from the planned PR144 workloads",
    )
    rounds = settings["rounds"]
    require(
        type(rounds) is int and (rounds == 1 if smoke else rounds >= 3),
        f"{stage}/{mode}: unexpected round count",
    )
    expected = set(product(configs, ROUTES, ROLES, range(rounds)))
    rows = {}
    excluded = {}
    totals = {
        phase: {"status_codes": Counter(), "transport_errors": Counter(), "missing": 0}
        for phase in ("warmup", "timed")
    }
    for row in raw["samples"]:
        config, route, role, number = key = tuple(
            row[field] for field in ("config", "route", "framework", "round")
        )
        require(
            key in expected and key not in rows and type(number) is int,
            f"{mode}: duplicate or unexpected sample {key}",
        )
        rows[key] = row
        reasons = qualify_sample(
            row, route, role, settings, free, builds, eligibility_reasons
        )
        if reasons:
            excluded[key] = reasons
        for phase, sample in (("warmup", row.get("warmup") or {}), ("timed", row)):
            statuses = sample.get("status_codes")
            errors = sample.get("error_distribution")
            if isinstance(statuses, dict) and isinstance(errors, dict):
                totals[phase]["status_codes"].update(statuses)
                totals[phase]["transport_errors"].update(errors)
            else:
                totals[phase]["missing"] += 1
    for config, route in product(configs, ROUTES):
        # Both Bessemer builds should return the same bytes. Axum's small
        # JSON fixture uses a different object key order.
        candidates = {
            key: row
            for key, row in rows.items()
            if key[:2] == (config, route)
            and key not in excluded
            and key[2] in BUILD_ROLES
        }
        if len({row["body_before"]["sha256"] for row in candidates.values()}) > 1:
            for key in candidates:
                excluded[key] = [
                    "Bessemer baseline and candidate response digests differ"
                ]
    groups = defaultdict(dict)
    for key, row in rows.items():
        if key not in excluded:
            groups[key[:3]][key[3]] = row
    return {
        "settings": settings,
        "source": source,
        "builds": builds,
        "toolchain": toolchain,
        "dependencies": dependencies,
        "grouped": {
            key: group for key, group in groups.items() if len(group) == rounds
        },
        "recorded": len(rows),
        "expected": len(expected),
        "totals": totals,
        "missing": sorted(expected - rows.keys()),
        "excluded": excluded,
        "hosts": {
            role: {key: meta[role][key] for key in (*HOST_FIELDS, "hostname")}
            for role in ("client", "server")
        },
    }


def distribution(values):
    values = list(values)
    return {"median": median(values), "range": [min(values), max(values)]}


def ratio(numerator, denominator, field="ok_rps"):
    if not numerator or numerator.keys() != denominator.keys():
        return None
    if any(row[field] <= 0 for row in denominator.values()):
        return None
    return distribution(numerator[n][field] / denominator[n][field] for n in numerator)


def metrics(group, config):
    if not group:
        return None
    rows = list(group.values())
    cores = int(config.split("-")[0])
    return {
        "rps": distribution(row["ok_rps"] for row in rows),
        "p50_ms": median(row["p50_ms"] for row in rows),
        "p99_ms": median(row["p99_ms"] for row in rows),
        "server_cpu": median(row["server_cpu"] for row in rows),
        "client_cpu": median(row["client_cpu"] for row in rows),
        "server_cpu_seconds_per_million": median(
            cores * row["server_cpu"] * 1e6 / row["ok_rps"] for row in rows
        ),
        "response_mib_per_second": median(
            row["response_body_bytes"] / (2**20 * row["elapsed_seconds"])
            for row in rows
        ),
    }


def summarize(studies, prior_summary):
    standard, free = (studies[name] for name in MODES)
    comparison = all(standard[key] == free[key] for key in ("settings", "hosts"))
    require(
        standard["source"]["files"] == free["source"]["files"],
        "benchmark source hashes differ between Python modes",
    )
    require(
        standard["dependencies"] == free["dependencies"],
        "Python dependency environments differ between Python modes",
    )
    for role in COMMITS:
        for key in ("versions", "source_sha256"):
            require(
                standard["builds"][role][key] == free["builds"][role][key],
                f"{role} {key} differs between Python modes",
            )

    summary = {
        "settings": standard["settings"],
        "same_hosts_and_settings": comparison,
        "dependencies": standard["dependencies"],
        "modes": {},
        "workloads": {},
    }
    for mode, study in studies.items():
        summary["modes"][mode] = {
            "recorded": study["recorded"],
            "planned": study["expected"],
            "eligible": study["recorded"] - len(study["excluded"]),
            "excluded": {
                "/".join(map(str, key)): val for key, val in study["excluded"].items()
            },
            "missing": study["missing"],
            "totals": study["totals"],
            "hosts": {
                role: {key: fields[key] for key in HOST_FIELDS}
                for role, fields in study["hosts"].items()
            },
            "builds": study["builds"],
            "toolchain": study["toolchain"],
            "bundle_sha256": study["source"]["bundle_sha256"],
        }
    for config, route in product(CONFIGS, ROUTES):
        matrix = {}
        selected = {}
        for mode, study in studies.items():
            groups = study["grouped"]
            roles = {role: groups.get((config, route, role), {}) for role in ROLES}
            selected[mode] = roles
            matrix[mode] = {
                "roles": {
                    role: metrics(group, config) for role, group in roles.items()
                },
                "ratios": {
                    key: ratio(roles[left], roles[right])
                    for key, (left, right) in PAIRS.items()
                },
                "main_rc10_p99": ratio(roles[ROLES[0]], roles[ROLES[1]], "p99_ms"),
                "scaling_over_one_core": {
                    role: ratio(group, groups.get(("1-core", route, role), {}))
                    for role, group in roles.items()
                },
            }
        if comparison:
            matrix["free_over_standard"] = {
                role: ratio(selected["free-threaded"][role], selected["standard"][role])
                for role in ROLES
            }
        summary["workloads"][f"{config}/{route}"] = matrix
    if prior_summary is not None:
        prior = read_json(prior_summary)
        require(
            all(prior["modes"][mode]["release_source"] == RC10 for mode in MODES),
            "historical context is not the PR144 RC10 publication",
        )
        summary["pr144_reference_published_wheel_rps"] = {
            config_route: {
                mode: details[mode]["roles"]["bessemer-auto"]["rps"] for mode in MODES
            }
            for config_route, details in prior["workloads"].items()
        }
    return summary


def shown_ratio(value):
    if value is None:
        return "withheld"
    low, high = value["range"]
    return f"{value['median']:.3f}× ({low:.3f}–{high:.3f})"


def show_report(summary):
    print("# Bessemer main vs RC10 on GCP")
    print(
        "\nThe release-mode Linux wheels for main and RC10 were built with the same toolchain."
    )
    print(f"Main commit {MAIN}. RC10 source {RC10}.")
    print(
        "\nStandard CPython 3.14.7 and free-threaded CPython 3.14.7t ran on the same"
        " Compute Engine server and private load generator. The eight workloads ran"
        " on one and four pinned server cores. Each of three rotated rounds used"
        " two seconds of warmup and five seconds of measured HTTP/1.1 load."
    )
    print(
        "Successful HTTP 200 replies per second include drain time. Each p99 is the median"
        " of the round p99 measurements. Speed ratios pair observations by numbered round."
    )
    for mode, data in summary["modes"].items():
        print(f"\n## {mode} Python")
        print(
            f"\n{data['eligible']}/{data['planned']} qualified. {data['recorded']} recorded."
        )
        for phase, total in data["totals"].items():
            status = total["status_codes"]
            errors = total["transport_errors"]
            non_ok = sum(count for code, count in status.items() if code != "200")
            print(
                f"- {phase} HTTP 200 {status['200']:,}, other HTTP {non_ok:,},"
                f" transport errors {sum(errors.values()):,}, missing observations {total['missing']}."
            )
        header = " | ".join(LABELS)
        print(
            f"\n| Cores | Workload | {header} | Main / RC10 | Main / FastAPI | Main / Axum |"
        )
        print("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, workloads in summary["workloads"].items():
            config, route = name.split("/")
            stats = workloads[mode]
            cells = []
            for role in ROLES:
                measure = stats["roles"][role]
                cells.append(
                    f"{measure['rps']['median']:,.0f} / {measure['p99_ms']:.3f} ms"
                    if measure
                    else "withheld"
                )
            cells += [
                shown_ratio(stats["ratios"][key])
                for key in ("main_rc10", "main_fastapi", "main_axum")
            ]
            print("| " + " | ".join((config.split("-")[0], route, *cells)) + " |")
    if summary.get("pr144_reference_published_wheel_rps"):
        print(
            "\nPR #144 used published RC10 wheels in an earlier GCP run. Its throughput is historical"
            " context. Build-to-build gains above use the paired RC10 control from this run."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        type=Path,
        required=True,
        help="checkout of 678e318 containing benchmark-source.patch",
    )
    parser.add_argument("--results", type=Path, default=HERE)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--prior-summary",
        type=Path,
        default=HERE.parents[1] / "2026-11-23/rc10-gcp-python314/summary.json",
        help="published PR144 summary for historical context",
    )
    args = parser.parse_args()
    repo = args.repository.resolve()
    try:
        provenance, series, grouped = read_public(args.results)
        hashes = provenance["benchmark_source_file_sha256"]
        for entry in series.values():
            check_source(entry["source"] | {"files": hashes}, repo)
        sys.path.insert(0, str(repo / "benchmarks/compare"))
        from measurement import eligibility_reasons

        studies = {}
        for (stage, mode), entry in series.items():
            dependencies = check_dependencies(entry["python_packages"], entry["build"])
            study = load_mode(
                {"meta": entry["runtime"], "samples": grouped[stage, mode]},
                entry["source"] | {"files": hashes},
                entry["build"],
                dependencies,
                mode,
                stage,
                eligibility_reasons,
            )
            require(
                study["expected"] == entry["planned"], "planned sample count mismatch"
            )
            require(
                study["recorded"] == entry["recorded"], "recorded sample count mismatch"
            )
            require(
                study["recorded"] - len(study["excluded"]) == entry["eligible"],
                "eligible sample count mismatch",
            )
            studies[stage, mode] = study
        cargo_lock = tomllib.loads(
            (repo / "benchmarks/compare/axum_app/Cargo.lock").read_text()
        )
        (axum_version,) = (
            package["version"]
            for package in cargo_lock["package"]
            if package["name"] == "axum"
        )
        require(axum_version == provenance["native_axum"], "Axum version mismatch")
        summary = summarize(
            {mode: studies["measured", mode] for mode in MODES},
            args.prior_summary,
        )
        summary["native_axum"] = axum_version
        summary["smoke"] = {
            mode: {
                "settings": study["settings"],
                "planned": study["expected"],
                "recorded": study["recorded"],
                "eligible": study["recorded"] - len(study["excluded"]),
                "excluded": {
                    "/".join(map(str, key)): reasons
                    for key, reasons in study["excluded"].items()
                },
                "missing": study["missing"],
                "totals": study["totals"],
            }
            for mode in MODES
            for study in (studies["smoke", mode],)
        }
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"{error}\n")
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2) + "\n")
    show_report(summary)
    return int(
        not summary["same_hosts_and_settings"]
        or any(study["missing"] or study["excluded"] for study in studies.values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
