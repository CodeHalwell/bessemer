"""Short installed-wheel microbenchmark for model encoding and body parsing.

Run the same file with each build's Python, preferably pinned to one core:
``taskset -c 1 /path/to/venv/bin/python benchmarks/codec/model_bench.py``.
Results are exploratory; no timing threshold is enforced.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import timeit
from pathlib import Path

from bessemer import _native
from bessemer._model_plan import model_plan
from pydantic import BaseModel, Field, TypeAdapter

LIMIT = 4_194_304


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class Order(BaseModel):
    items: list[Item]


class EscapedItem(BaseModel):
    id: int = Field(alias='id"')
    name: str = Field(alias="name\n")
    price: float = Field(alias="price\\")
    tags: list[str] = Field(alias="tags\x00é🙂")


class EscapedOrder(BaseModel):
    items: list[EscapedItem] = Field(alias="items\t")


def measure(function, number: int, repeat: int) -> dict:
    samples = timeit.repeat(function, number=number, repeat=repeat)
    return {
        "iterations": number,
        "min_ns": round(min(samples) / number * 1e9),
        "median_ns": round(statistics.median(samples) / number * 1e9),
        "samples_ns": [round(sample / number * 1e9) for sample in samples],
    }


def scenario(item_type, order_type, size: int, number: int, repeat: int) -> dict:
    items = [
        item_type.model_construct(
            id=index, name=f"item-{index}", price=index * 1.5, tags=["a", "b"]
        )
        for index in range(size)
    ]
    value = order_type.model_construct(items=items)
    adapter = TypeAdapter(order_type)
    dumped = adapter.dump_python(value, mode="json", by_alias=True, warnings="error")
    raw = _native._encode_plain_json(dumped, LIMIT)
    plan = model_plan(order_type)
    assert raw is not None and plan is not None
    assert _native._encode_model_json(plan, value, LIMIT) == raw
    parsed = _native._parse_model(plan, raw)
    assert parsed is not None
    assert adapter.dump_python(parsed, mode="json", by_alias=True) == dumped
    functions = {
        "encode_model": lambda: _native._encode_model_json(plan, value, LIMIT),
        "parse_model_including_validation": lambda: _native._parse_model(plan, raw),
        "encode_plain_control": lambda: _native._encode_plain_json(dumped, LIMIT),
    }
    return {
        "items": size,
        "json_bytes": len(raw),
        "timings": {
            name: measure(function, max(1, number // size), repeat)
            for name, function in functions.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", type=int, default=10_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.number < 1 or args.repeat < 1:
        parser.error("--number and --repeat must be positive")
    print(
        json.dumps(
            {
                "bessemer": importlib.metadata.version("bessemer"),
                "pydantic": importlib.metadata.version("pydantic"),
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "platform": platform.platform(),
                "native_file": _native.__file__,
                "native_sha256": hashlib.sha256(
                    Path(_native.__file__).read_bytes()
                ).hexdigest(),
                "repeat": args.repeat,
                "scenarios": {
                    f"{name}_{size}": scenario(
                        item_type, order_type, size, args.number, args.repeat
                    )
                    for name, item_type, order_type in (
                        ("plain", Item, Order),
                        ("escaped_aliases", EscapedItem, EscapedOrder),
                    )
                    for size in (1, 20, 100)
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
