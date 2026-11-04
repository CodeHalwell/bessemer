"""Exploratory micro-benchmark for Bessemer's native JSON codec.

Times the native encoder and decoder against the standard library (and orjson
when installed) on representative payloads. Run pinned to one core, e.g.
``taskset -c 1 python benchmarks/codec/codec_bench.py``. Results are
exploratory and do not establish formal performance acceptance.
"""

from __future__ import annotations

import argparse
import json
import statistics
import timeit

from bessemer import _native

PAYLOADS = {
    "small": {"ok": True, "count": 42},
    "item": {
        "id": 12345,
        "name": "Widget deluxe",
        "price": 19.99,
        "tags": ["a", "b", "c"],
        "in_stock": True,
        "owner": None,
    },
    "list100": [
        {"id": i, "name": f"item-{i}", "score": i * 1.5, "active": i % 2 == 0}
        for i in range(100)
    ],
    "text": {"title": "Ünïcödé — test", "body": 'line\n"quoted"\t' * 50},
}


def best(fn, number: int, repeat: int) -> float:
    samples = timeit.repeat(fn, number=number, repeat=repeat)
    return min(samples) / number * 1e9, statistics.median(samples) / number * 1e9


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        import orjson
    except ImportError:
        orjson = None
    limit = 4_194_304
    results = {}
    for name, value in PAYLOADS.items():
        raw = json.dumps(value).encode()
        number = 20000 if name != "list100" else 500
        row = {}
        candidates = {
            "encode/native": lambda value=value: _native._encode_plain_json(
                value, limit
            ),
            "encode/stdlib": lambda value=value: json.dumps(
                value, separators=(",", ":")
            ).encode(),
            "decode/native": lambda raw=raw: _native._parse_json(raw),
            "decode/stdlib": lambda raw=raw: json.loads(raw),
        }
        if orjson is not None:
            candidates["encode/orjson"] = lambda value=value: orjson.dumps(value)
            candidates["decode/orjson"] = lambda raw=raw: orjson.loads(raw)
        for label, fn in candidates.items():
            low, median = best(fn, number, args.repeat)
            row[label] = {"min_ns": round(low), "median_ns": round(median)}
        results[name] = row
    if args.json:
        print(json.dumps(results, indent=2))
        return
    for name, row in results.items():
        print(f"{name}")
        for label, value in row.items():
            print(
                f"  {label:16} {value['min_ns']:>10,} ns  (median {value['median_ns']:,})"
            )


if __name__ == "__main__":
    main()
