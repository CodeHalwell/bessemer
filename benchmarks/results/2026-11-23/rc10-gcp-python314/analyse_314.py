"""Qualify the matched GCP CPython 3.14 and 3.14t RC10 result bundles.

Use --repository with the RC10 checkout containing the measured benchmark
patch, and --standard and --free-threaded with the corresponding raw bundles.
The report goes to stdout. --output sets the machine-readable summary path.
"""

from __future__ import annotations

import argparse
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
RELEASE = "687e70739416d0cca7b4c64de6d4272edf03b101"
ROLES = ("bessemer-auto", "fastapi-standard", "axum")
VERSIONS = {
    "python": "3.14.7",
    "bessemer": "1.0.0rc10",
    "fastapi": "0.165.0",
    "uvicorn": "0.54.0",
    "uvloop": "0.23.0",
    "httptools": "0.9.0",
    "pydantic": "2.14.4",
}
WHEELS = {
    "standard": "bd342f9c91c1fba6f75a42c1a3e09cb3b39f7ffc520a83f23445feb9020606fd",
    "free-threaded": "228fc4874c76b14ebeb3d89b74112ffbaaf389c82bea497346a5aec009d210e6",
}
MACHINE_FIELDS = ("hostname", "kernel", "cpu_model", "cpus")
PUBLIC_HOST_FIELDS = (
    "kernel",
    "cpu_model",
    "cpus",
    "python",
    "oha",
    "gil_enabled",
    "free_threaded",
)
PUBLIC_SETTINGS = (
    "configs",
    "routes",
    "frameworks",
    "rounds",
    "duration",
    "warmup",
    "connections",
    "route_connections",
    "check_bodies",
    "large_echo_input",
)
WORKLOADS = {
    "json": "Small JSON response",
    "items": "Integer path parameter and string query parameter",
    "echo": "20-item typed JSON request and response",
    "cpu-inline": "50,000-iteration integer checksum inside an async handler",
    "cpu-sync": "Same checksum using sync handler dispatch or Tokio spawn_blocking",
    "large-echo": "1,000-item typed JSON request and response",
    "bytes": "262,144-byte immutable response",
    "models256": "256-item typed JSON response",
}
MIB = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_file(path):
    return json.loads(path.read_text())


def read_source(directory, repository, mode):
    source = json_file(directory / "source.json")
    files = source["files"]
    require(
        source["release_commit"] == RELEASE and files,
        f"{mode}: expected RC10 source manifest",
    )
    for name, digest in files.items():
        path = (repository / name).resolve()
        require(
            path.is_relative_to(repository)
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
            f"{mode}: source hash mismatch for {name}",
        )
    bootstrap = "benchmarks/compare/remote/bootstrap.sh"
    require(
        bootstrap in files and WHEELS[mode] in (repository / bootstrap).read_text(),
        f"{mode}: expected a hash-pinned Linux RC10 wheel",
    )
    return source


def read_lock(directory, mode):
    installed = dict(
        line.split("==", 1)
        for line in (directory / "python-packages.txt").read_text().splitlines()
        if "==" in line
    )
    for package, version in (VERSIONS | {"pydantic-core": "2.50.5"}).items():
        if package != "python":
            require(
                installed.get(package) == version,
                f"{mode}: unexpected {package} package lock",
            )
    return installed


def runtime_reasons(observed, free, prefix, pid_required=False):
    if not isinstance(observed, dict):
        return [f"{prefix}: runtime identity missing"]
    reasons = []
    if observed.get("gil_enabled") is not (not free):
        reasons.append(f"{prefix}: unexpected live GIL state")
    if observed.get("free_threaded") is not free:
        reasons.append(f"{prefix}: unexpected free-threaded ABI")
    if pid_required and (type(observed.get("pid")) is not int or observed["pid"] < 1):
        reasons.append(f"{prefix}: worker PID unavailable")
    if observed.get("python", "3.14.7") != "3.14.7":
        reasons.append(f"{prefix}: unexpected live Python version")
    if "jit_enabled" in observed and observed["jit_enabled"] is not False:
        reasons.append(f"{prefix}: JIT enabled")
    return reasons


def verify_command(sample, seconds, connections, phase):
    args = sample.get("command") or []
    try:
        host = IPv4Address(urlsplit(args[-1]).hostname)
        valid = (
            str(args[args.index("-z") + 1]) == f"{seconds}s"
            and int(args[args.index("-c") + 1]) == connections
            and urlsplit(args[-1]).scheme == "http"
            and host.is_private
            and not host.is_loopback
            and "--wait-ongoing-requests-after-deadline" in args
            and "--disable-compression" in args
        )
    except (IndexError, ValueError, TypeError):
        valid = False
    return [] if valid else [f"{phase}: timed command does not match study settings"]


def body_reasons(row, phase, bytes_per_reply):
    if (
        row["non_200"] == 0
        and row["errors"] == 0
        and row.get("response_body_bytes")
        != row["successful_responses"] * bytes_per_reply
    ):
        return [f"{phase}: aggregate body bytes mismatch"]
    return []


def load_study(directory, repository, mode, eligibility_reasons):
    source = read_source(directory, repository, mode)
    packages = read_lock(directory, mode)
    raw = json_file(directory / "raw.json")
    meta = raw["meta"]
    settings = meta["settings"]
    free = mode == "free-threaded"
    initial = runtime_reasons(meta["server"], free, mode)
    require(not initial, "; ".join(initial))
    require(
        meta["mode"] == "two-machine" and settings["check_bodies"] is True,
        f"{mode}: private two-host body-checked run required",
    )
    require(
        all(
            meta["server"]["versions"].get(package) == version
            for package, version in VERSIONS.items()
        ),
        f"{mode}: runtime drift in application versions",
    )
    require(meta["client"]["oha"] == "oha 1.16.0", f"{mode}: wrong oha version")
    require(
        meta["server"]["hostname"] != meta["client"]["hostname"],
        f"{mode}: expected separate hosts",
    )

    configs, routes = settings["configs"].split(","), settings["routes"].split(",")
    expected_roles = settings["frameworks"].split(",")
    for field in ("rounds", "duration", "warmup", "connections"):
        require(
            type(settings[field]) is int and settings[field] > 0,
            f"{mode}: invalid {field}",
        )
    require(expected_roles == list(ROLES), f"{mode}: expected {ROLES}")
    require(
        all(re.fullmatch(r"[1-9][0-9]*-core", config) for config in configs),
        f"{mode}: invalid CPU configuration",
    )
    overrides = settings.get("route_connections", {})
    require(
        isinstance(overrides, dict) and overrides.keys() <= set(routes),
        f"{mode}: invalid route connection overrides",
    )
    for count in overrides.values():
        require(
            type(count) is int and count > 0, f"{mode}: invalid route connection count"
        )
    if "large-echo" in routes:
        payload = settings.get("large_echo_input") or {}
        require(
            type(payload.get("bytes")) is int
            and payload["bytes"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256"))),
            f"{mode}: large request size and digest missing",
        )
    plan = set(product(configs, routes, ROLES, range(settings["rounds"])))
    require(
        len(plan) == len(configs) * len(routes) * len(ROLES) * settings["rounds"],
        f"{mode}: duplicate configurations or routes",
    )
    rows = {}
    totals = {
        name: {"status": Counter(), "errors": Counter(), "missing": 0}
        for name in ("warmup", "measured")
    }
    groups = defaultdict(list)
    for raw_row in raw["samples"]:
        key = tuple(
            raw_row[field] for field in ("config", "route", "framework", "round")
        )
        require(
            key in plan and key not in rows,
            f"{mode}: unplanned or duplicated row {key}",
        )
        row = raw_row.copy()
        config, route, role, _ = key
        connections = overrides.get(route, settings["connections"])
        require(
            row.get("connections") == connections,
            f"{mode} {key}: wrong recorded connection count",
        )
        reasons = eligibility_reasons(row)
        if row.get("eligible") is not True:
            reasons.append("runner marked sample ineligible")
        if role != "axum":
            for phase in ("before", "after"):
                reasons.extend(
                    runtime_reasons(
                        row.get(f"runtime_{phase}"),
                        free,
                        f"worker {phase}",
                        pid_required=True,
                    )
                )
        cpu = row.get("client_cpu")
        if isinstance(cpu, (float, int)) and not isinstance(cpu, bool) and cpu > 0.85:
            reasons.append("client CPU exceeded qualification limit")
        before = row.get("body_before")
        size = before.get("bytes") if isinstance(before, dict) else None
        if (
            type(size) is not int
            or size <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(before.get("sha256")))
            or before != row.get("body_after")
        ):
            reasons.append("probe body missing or changed during load")
            size = None
        for phase, sample, seconds in (
            ("warmup", row.get("warmup") or {}, settings["warmup"]),
            ("measured", row, settings["duration"]),
        ):
            totals[phase]["status"].update(sample.get("status_codes") or {})
            totals[phase]["errors"].update(sample.get("error_distribution") or {})
            if (
                sample.get("status_codes") is None
                or sample.get("error_distribution") is None
            ):
                totals[phase]["missing"] += 1
            reasons.extend(verify_command(sample, seconds, connections, phase))
            if size is not None and all(
                type(sample.get(field)) is int
                for field in ("non_200", "errors", "successful_responses")
            ):
                reasons.extend(body_reasons(sample, phase, size))
        if any(
            not isinstance(row.get(field), (int, float))
            or not math.isfinite(row[field])
            or row[field] < 0
            for field in ("p50_ms", "p99_ms")
        ):
            reasons.append("latency percentiles unavailable")
        row["validation_reasons"] = list(dict.fromkeys(reasons))
        rows[key] = row
        groups[config, route, role].append(row)
    return {
        "mode": mode,
        "source": source,
        "packages": packages,
        "meta": meta,
        "rows": rows,
        "groups": groups,
        "plan": plan,
        "totals": totals,
    }


def qualified_group(study, config, route, role):
    rows = study["groups"][config, route, role]
    if len(rows) != study["meta"]["settings"]["rounds"] or any(
        row["validation_reasons"] for row in rows
    ):
        return None
    return rows


def ratios(left, right, field="ok_rps"):
    if left is None or right is None:
        return None
    base = {sample["round"]: sample[field] for sample in right}
    if not all(value > 0 for value in base.values()):
        return None
    values = [sample[field] / base[sample["round"]] for sample in left]
    return interval(values)


def interval(values):
    return {"median": median(values), "range": [min(values), max(values)]}


def native_normalised(free, standard, role):
    roles = [free[role], standard[role], free["axum"], standard["axum"]]
    if any(role_rows is None for role_rows in roles):
        return None
    ft, std, ft_native, std_native = (
        {row["round"]: row["ok_rps"] for row in role_rows} for role_rows in roles
    )
    return interval([(ft[n] / ft_native[n]) / (std[n] / std_native[n]) for n in ft])


def metrics(samples, cores):
    if not samples:
        return None
    costs = [
        sample["server_cpu"]
        * cores
        * sample["elapsed_seconds"]
        * 1e6
        / sample["successful_responses"]
        for sample in samples
    ]
    rates = [sample["ok_rps"] for sample in samples]
    return {
        "rps": median(rates),
        "rps_range": [min(rates), max(rates)],
        "p50_ms": median(sample["p50_ms"] for sample in samples),
        "p99_ms": median(sample["p99_ms"] for sample in samples),
        "server_cpu": median(sample["server_cpu"] for sample in samples),
        "client_cpu": median(sample["client_cpu"] for sample in samples),
        "server_cpu_seconds_per_million": median(costs),
        "response_bytes_per_reply_range": [
            min(sample["body_before"]["bytes"] for sample in samples),
            max(sample["body_before"]["bytes"] for sample in samples),
        ],
        "response_mib_per_second": median(
            sample["response_body_bytes"] / (MIB * sample["elapsed_seconds"])
            for sample in samples
        ),
    }


def study_summary(study):
    return {
        "raw": len(study["rows"]),
        "planned": len(study["plan"]),
        "eligible": sum(
            not row["validation_reasons"] for row in study["rows"].values()
        ),
        "totals": study["totals"],
        "hosts": {
            name: {
                field: study["meta"][name][field]
                for field in PUBLIC_HOST_FIELDS
                if field in study["meta"][name]
            }
            for name in ("server", "client")
        },
        "release_source": study["source"]["release_commit"],
        "release_wheel_sha256": WHEELS[study["mode"]],
        "bundle_sha256": study["source"].get("bundle_sha256"),
        "excluded": {
            "/".join(map(str, key)): row["validation_reasons"]
            for key, row in study["rows"].items()
            if row["validation_reasons"]
        },
        "missing": [list(row) for row in sorted(study["plan"] - study["rows"].keys())],
    }


def make_summary(standard, free, axum_version):
    settings = standard["meta"]["settings"]
    require(
        free["meta"]["settings"] == settings,
        "measurement settings differ across Python builds",
    )
    require(
        free["source"]["files"] == standard["source"]["files"],
        "fixture and runner hashes differ",
    )
    require(free["packages"] == standard["packages"], "Python package locks differ")
    same_hosts = all(
        standard["meta"][role][field] == free["meta"][role][field]
        for role in ("server", "client")
        for field in MACHINE_FIELDS
    )
    summary = {
        "settings": {
            field: settings[field] for field in PUBLIC_SETTINGS if field in settings
        },
        "axum_version": axum_version,
        "same_hosts": same_hosts,
        "modes": {study["mode"]: study_summary(study) for study in (standard, free)},
        "workloads": {},
    }
    for config, route in product(
        settings["configs"].split(","), settings["routes"].split(",")
    ):
        cores = int(config.split("-")[0])
        results = {}
        selected = {}
        for study in (standard, free):
            mode = study["mode"]
            selected[mode] = {
                role: qualified_group(study, config, route, role) for role in ROLES
            }
            bessemer, fastapi, axum = selected[mode].values()
            results[mode] = {
                "roles": {
                    role: metrics(rows, cores) for role, rows in selected[mode].items()
                },
                "bessemer_fastapi": ratios(bessemer, fastapi),
                "bessemer_axum": ratios(bessemer, axum),
                "axum_fastapi": ratios(axum, fastapi),
                "scaling_over_one_core": {
                    role: ratios(rows, qualified_group(study, "1-core", route, role))
                    for role, rows in selected[mode].items()
                },
            }
        results["free_over_standard"] = {
            role: ratios(selected["free-threaded"][role], selected["standard"][role])
            if same_hosts
            else None
            for role in ROLES
        }
        results["free_over_standard_p99"] = {
            role: ratios(
                selected["free-threaded"][role], selected["standard"][role], "p99_ms"
            )
            if same_hosts
            else None
            for role in ROLES
        }
        results["axum_normalised_free_over_standard"] = {
            role: native_normalised(
                selected["free-threaded"], selected["standard"], role
            )
            if same_hosts
            else None
            for role in ROLES
            if role != "axum"
        }
        summary["workloads"][f"{config}/{route}"] = results
    return summary


def show_ratio(value):
    if value is None:
        return "withheld"
    centre = value["median"]
    lo, hi = value["range"]
    return f"{centre:.3f}× ({lo:.3f}–{hi:.3f})"


def show_bulk(summary):
    settings = summary["settings"]
    bulk = {
        key: result
        for key, result in summary["workloads"].items()
        if key.split("/")[1] in {"large-echo", "bytes"}
    }
    if not bulk:
        return
    print("\n### Large HTTP payloads\n")
    upload = settings.get("large_echo_input")
    if upload:
        print(
            f"The large echo request contained {upload['bytes']:,} bytes (SHA-256 `{upload['sha256']}`)."
        )
    print(
        "The bytes endpoint has no request body. MiB/s counts successful HTTP response bodies, "
        "with HTTP headers and request bytes excluded. Private network bandwidth can limit these rates.\n"
    )
    print(
        "| Python | Cores | Route | Framework | Response bytes per reply | Response MiB/s |"
    )
    print("| --- | ---: | --- | --- | ---: | ---: |")
    for mode in summary["modes"]:
        for key, result in bulk.items():
            config, route = key.split("/")
            for role, data in result[mode]["roles"].items():
                if not data:
                    print(
                        f"| {mode} | {config.split('-')[0]} | {route} | {role} | withheld | withheld |"
                    )
                    continue
                low, high = data["response_bytes_per_reply_range"]
                size = f"{low:,}" if low == high else f"{low:,}–{high:,}"
                print(
                    f"| {mode} | {config.split('-')[0]} | {route} | {role} "
                    f"| {size} | {data['response_mib_per_second']:,.1f} |"
                )


def show_scaling(summary):
    scaled = {
        key: group
        for key, group in summary["workloads"].items()
        if not key.startswith("1-core/")
    }
    if not scaled or "1-core" not in summary["settings"]["configs"].split(","):
        return
    print("\n### Throughput scaling from one core\n")
    print(
        "Each ratio matches the same runtime, route and numbered round. "
        "Python workers and Tokio worker threads scale with the number of pinned guest cores.\n"
    )
    print("| Python | Cores | Route | Bessemer | FastAPI | Axum |")
    print("| --- | ---: | --- | ---: | ---: | ---: |")
    for mode in summary["modes"]:
        for key, group in scaled.items():
            config, route = key.split("/")
            values = map(show_ratio, group[mode]["scaling_over_one_core"].values())
            print(
                "| " + " | ".join([mode, config.split("-")[0], route, *values]) + " |"
            )


def show_native_control(summary):
    print("\n### Free-threaded throughput normalised to the repeated Axum control\n")
    print(
        "Each mode ran Axum on the same server with the same workload. For each corresponding "
        "round, divide the free-threaded Python-to-Axum ratio by the standard Python-to-Axum "
        "ratio. The result estimates the mode change relative to native baseline drift. "
        "Read it alongside the direct mode ratios, especially on high-bandwidth routes.\n"
    )
    print("| Cores | Route | Bessemer | FastAPI |")
    print("| ---: | --- | ---: | ---: |")
    for key, result in summary["workloads"].items():
        config, route = key.split("/")
        values = map(show_ratio, result["axum_normalised_free_over_standard"].values())
        print("| " + " | ".join([config.split("-")[0], route, *values]) + " |")


def show_report(summary):
    settings = summary["settings"]
    print("# Bessemer RC10 on GCP with CPython 3.14.7 and 3.14.7t\n")
    for mode, data in summary["modes"].items():
        measured = data["totals"]["measured"]
        warmup = data["totals"]["warmup"]
        label = "Standard CPython" if mode == "standard" else "Free-threaded CPython"
        print(
            f"- {label} qualified {data['eligible']}/{data['planned']} samples with "
            f"{measured['status']['200']:,} timed HTTP 200 replies, "
            f"{sum(n for code, n in measured['status'].items() if code != '200'):,} other timed HTTP statuses "
            f"and {sum(measured['errors'].values()):,} timed transport errors. "
            f"Warmup returned {warmup['status']['200']:,} HTTP 200 replies, "
            f"{sum(n for code, n in warmup['status'].items() if code != '200'):,} other statuses "
            f"and {sum(warmup['errors'].values()):,} transport errors. "
            f"The count of missing warmup or timed observations was {warmup['missing'] + measured['missing']}."
        )
    server = summary["modes"]["standard"]["hosts"]["server"]
    client = summary["modes"]["standard"]["hosts"]["client"]
    hosts = (
        "The same server and load generator served both runs."
        if summary["same_hosts"]
        else "Host identities differed. Cross-runtime ratios are withheld."
    )
    print(
        f"\nServer {server['cpu_model']} with {server['cpus']} guest CPUs. "
        f"Client {client['cpu_model']} with {client['cpus']} guest CPUs. {hosts}"
    )
    print(
        "\nBessemer 1.0.0rc10 and FastAPI 0.165.0 used Pydantic 2.14.4. "
        "FastAPI used Uvicorn 0.54.0 with uvloop 0.23.0 and httptools 0.9.0. "
        f"Axum {summary['axum_version']} was built from the locked native fixture and ran in both mode passes."
    )
    print()
    for mode, data in summary["modes"].items():
        print(f"- {mode} RC10 Linux wheel SHA-256 `{data['release_wheel_sha256']}`.")
    print(
        f"\n{settings['rounds']} rotated rounds per mode with {settings['warmup']} seconds warmup "
        f"and {settings['duration']} seconds timed HTTP/1.1 load with request drain. "
        "Runtime modes were scheduled separately. The ratio range is the minimum to maximum across corresponding rounds. "
        "Only complete, qualified round sets produce rates or ratios. "
        "CPU seconds per million estimates pinned server CPU time from guest counters.\n"
    )
    print(
        "Python workers reported their interpreter build and actual GIL state before and after load. "
        "Response probes checked the complete synthetic payloads and matching body digests. "
        "The benchmark also checked the aggregate response body bytes against the successful request count. "
        "Samples qualify when warmup and timed traffic both have zero HTTP and transport errors "
        "and the load generator stays below the client CPU limit.\n"
    )
    print("| Workload | Fixture | Connections |")
    print("| --- | --- | ---: |")
    for route in settings["routes"].split(","):
        conns = settings.get("route_connections", {}).get(
            route, settings["connections"]
        )
        print(f"| {route} | {WORKLOADS.get(route, route)} | {conns} |")
    print()
    for mode in summary["modes"]:
        print(f"### {mode} successful HTTP replies per second\n")
        print(
            "| Cores | Route | Clients | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |"
        )
        print("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for key, result in summary["workloads"].items():
            config, route = key.split("/")
            values = result[mode]
            rates = [
                f"{data['rps']:,.0f}" if data else "withheld"
                for data in values["roles"].values()
            ]
            conns = settings.get("route_connections", {}).get(
                route, settings["connections"]
            )
            fields = [
                config.split("-")[0],
                route,
                str(conns),
                *rates,
                show_ratio(values["bessemer_fastapi"]),
                show_ratio(values["bessemer_axum"]),
            ]
            print("| " + " | ".join(fields) + " |")
        print()
    print("### Free-threaded over standard throughput\n")
    print("| Cores | Route | Bessemer | FastAPI | Axum control |")
    print("| ---: | --- | ---: | ---: | ---: |")
    for key, result in summary["workloads"].items():
        config, route = key.split("/")
        print(
            "| "
            + " | ".join(
                [
                    config.split("-")[0],
                    route,
                    *map(show_ratio, result["free_over_standard"].values()),
                ]
            )
            + " |"
        )
    show_native_control(summary)
    show_scaling(summary)
    show_bulk(summary)
    for mode in summary["modes"]:
        print(f"\n### {mode} latency and CPU\n")
        print(
            "| Cores | Route | Framework | p50 ms | p99 ms | Server CPU | Client CPU | CPU s / million |"
        )
        print("| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for key, result in summary["workloads"].items():
            config, route = key.split("/")
            for role, data in result[mode]["roles"].items():
                if data:
                    cells = [
                        config.split("-")[0],
                        route,
                        role,
                        f"{data['p50_ms']:.3f}",
                        f"{data['p99_ms']:.3f}",
                        f"{data['server_cpu']:.1%}",
                        f"{data['client_cpu']:.1%}",
                        f"{data['server_cpu_seconds_per_million']:.2f}",
                    ]
                    print("| " + " | ".join(cells) + " |")
        excluded = summary["modes"][mode]["excluded"]
        if excluded:
            print(f"\nExcluded {mode} samples\n")
            for key, reasons in sorted(excluded.items()):
                print(f"- {key} — " + ", ".join(reasons))
        missing = summary["modes"][mode]["missing"]
        if missing:
            print(f"\nMissing {mode} samples\n")
            for key in missing:
                print("- " + "/".join(map(str, key)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standard", type=Path, default=HERE / "standard")
    parser.add_argument("--free-threaded", type=Path, default=HERE / "free-threaded")
    parser.add_argument(
        "--repository",
        type=Path,
        help="RC10 checkout containing the measured benchmark patch",
    )
    parser.add_argument("--output", type=Path, default=HERE / "summary-qualified.json")
    args = parser.parse_args()

    repository = args.repository
    if repository is None:
        local_checkout = HERE.parents[1] / "bessemer-gcp-rc10-314"
        repository = next(
            (
                root
                for root in (*HERE.parents, local_checkout)
                if (root / "benchmarks/compare/measurement.py").is_file()
            ),
            None,
        )
    if repository is None:
        parser.error("--repository must point to the measured RC10 benchmark checkout")
    repository = repository.resolve()
    sys.path.insert(0, str(repository / "benchmarks/compare"))
    from measurement import eligibility_reasons

    standard = load_study(args.standard, repository, "standard", eligibility_reasons)
    free = load_study(
        args.free_threaded, repository, "free-threaded", eligibility_reasons
    )
    cargo_lock = tomllib.loads(
        (repository / "benchmarks/compare/axum_app/Cargo.lock").read_text()
    )
    (axum_version,) = (
        package["version"]
        for package in cargo_lock["package"]
        if package["name"] == "axum"
    )
    summary = make_summary(standard, free, axum_version)
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    show_report(summary)


if __name__ == "__main__":
    main()
