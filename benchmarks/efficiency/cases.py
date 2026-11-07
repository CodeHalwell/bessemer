"""Existing response contracts with small, explicitly offered-rate ramps."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from concurrency.workloads import WORKLOADS, Workload
from multicore.work import checksum

CASES = {
    case.name: case
    for case in WORKLOADS
    if case.name
    in {
        "json",
        "echo",
        "dependencies",
        "io",
        "fanout",
        "sync",
        "stream",
        "sync-stream",
        "events",
    }
}
CASES["mixed"] = Workload("mixed", "/mixed", {"checksum": 2 * checksum(25000)})
RATES = {
    "json": [10000, 40000],
    "echo": [3000, 15000],
    "dependencies": [5000, 25000],
    "io": [10000, 40000],
    "fanout": [1000, 5000],
    "sync": [1000, 10000],
    "stream": [2000, 10000],
    "sync-stream": [1000, 8000],
    "events": [2000, 10000],
    "mixed": [300, 1500],
}
