from pathlib import Path

cols = ["kernel", "rust", "pyo3", "handler", "orch", "loop"]
# 64c splits from perf runs (us), derived in analysis; total = perf-run total
base = {
    ("rust", "json"): {
        "kernel": 6.08,
        "rust": 4.45,
        "pyo3": 0.22,
        "handler": 0.17,
        "orch": 1.18,
        "loop": 0.12,
    },
    ("asyncio", "json"): {
        "kernel": 7.16,
        "rust": 4.37,
        "pyo3": 0.16,
        "handler": 0.17,
        "orch": 1.02,
        "loop": 0.92,
    },
    ("rust", "echo"): {
        "kernel": 5.55,
        "rust": 8.32,
        "pyo3": 0.55,
        "handler": 0.17,
        "orch": 1.52,
        "loop": 0.28,
    },
    ("asyncio", "echo"): {
        "kernel": 6.67,
        "rust": 8.57,
        "pyo3": 0.47,
        "handler": 0.17,
        "orch": 1.44,
        "loop": 1.15,
    },
}
# items: json + callgrind delta shares (rust loop): rust_http 3004+58, orch 774+40-5, pyo3 409, handler 185, kernel(syscall wrap) 317, loop 119
d = {"rust": 3062, "orch": 809, "pyo3": 409, "handler": 185, "kernel": 317, "loop": 119}
ds = sum(d.values())
sat64 = {
    "axum": {"json": 11.62, "items": 11.84, "echo": 15.60},
    "rust": {"json": 12.23, "items": 13.04, "echo": 16.49},
    "uring": {"json": 10.19, "items": 10.88, "echo": 16.19},
    "asyncio": {"json": 13.72, "items": 14.51, "echo": 18.17},
}
c1024 = {
    "axum": {"json": 13.26, "items": 14.84, "echo": 20.92},
    "rust": {"json": 13.55, "items": 14.00, "echo": 18.72},
    "uring": {"json": 10.35, "items": 11.27, "echo": 15.42},
    "asyncio": {"json": 23.84, "items": 28.82, "echo": 29.38},
}


def scaled(sp, total):
    s = sum(sp.values())
    return {k: v * total / s for k, v in sp.items()}


rows = {}
for lp in ["rust", "asyncio"]:
    rows[(lp, "json")] = scaled(base[(lp, "json")], sat64[lp]["json"])
    rows[(lp, "echo")] = scaled(base[(lp, "echo")], sat64[lp]["echo"])
    j = rows[(lp, "json")]
    delta = sat64[lp]["items"] - sat64[lp]["json"]
    rows[(lp, "items")] = {k: j[k] + delta * d.get(k, 0) / ds for k in cols}
for r in ["json", "items", "echo"]:
    u = {k: v for k, v in rows[("rust", r)].items() if k != "kernel"}
    rows[("uring", r)] = dict(u, kernel=sat64["uring"][r] - sum(u.values()))
axk = {"json": 0.658 * 11.62}
for r in ["json", "items", "echo"]:
    k = axk["json"]
    rows[("axum", r)] = {
        "kernel": k,
        "rust": sat64["axum"][r] - k,
        "pyo3": 0,
        "handler": 0,
        "orch": 0,
        "loop": 0,
    }
out = []
names = {
    "axum": "Axum (1 thread)",
    "rust": "Bessemer, Rust loop (epoll)",
    "uring": "Bessemer, Rust loop + io_uring",
    "asyncio": "Bessemer, asyncio loop (default)",
}
for r in ["json", "items", "echo"]:
    out.append(f"\n#### {r} — 64 connections, server saturated (oha on CPUs 1-3)\n")
    out.append(
        "| configuration | total | kernel | Rust HTTP parse/route/serialise | pyo3 crossing + GIL | Python handler body | Bessemer orchestration | asyncio / loop | gap to Axum |"
    )
    out.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for s in ["axum", "rust", "uring", "asyncio"]:
        x = rows[(s, r)]
        t = sat64[s][r]
        g = t - sat64["axum"][r]
        f = lambda v: f"{v:.2f}" if v > 0.004 else "0"
        out.append(
            f"| {names[s]} | **{t:.2f}** | {f(x['kernel'])} | {f(x['rust'])} | {f(x['pyo3'])} | {f(x['handler'])} | {f(x['orch'])} | {f(x['loop'])} | {'' if s == 'axum' else f'{g:+.2f} ({100 * g / sat64[chr(97) + "xum"][r]:+.0f}%)'} |"
        )
    out.append(f"\n#### {r} — 1024 connections (oha on CPUs 1-3)\n")
    out.append(
        "| configuration | total | 64-conn split total | extra at 1024 (not attributed) | gap to Axum |"
    )
    out.append("|---|--:|--:|--:|--:|")
    for s in ["axum", "rust", "uring", "asyncio"]:
        t = c1024[s][r]
        b = sat64[s][r]
        g = t - c1024["axum"][r]
        out.append(
            f"| {names[s]}{' (max_in_flight=4096)' if s == 'asyncio' else ''} | **{t:.2f}** | {b:.2f} | {t - b:+.2f} | {'' if s == 'axum' else f'{g:+.2f} ({100 * g / c1024[chr(97) + "xum"][r]:+.0f}%)'} |"
        )
Path("tables.md").write_text("\n".join(out))
print("\n".join(out))
