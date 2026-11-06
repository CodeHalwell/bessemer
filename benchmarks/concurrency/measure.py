"""Short HTTP throughput and bidirectional WebSocket samples."""

import asyncio
import json
import math
import resource
import subprocess
import time
from collections import deque

from server import tree_cpu


def http_sample(args, server, workload, duration):
    command = [
        args.oha,
        "--worker-threads",
        "2",
        "-z",
        f"{duration}s",
        "-c",
        str(args.connections),
        "--no-tui",
        "--output-format",
        "json",
        "--wait-ongoing-requests-after-deadline",
    ]
    if workload.payload is not None:
        command += [
            "-m",
            "POST",
            "-H",
            "content-type: application/json",
            "-d",
            workload.payload.decode(),
        ]
    command += [f"http://127.0.0.1:{server.port}{workload.path}"]
    before, started = tree_cpu(server.process.pid), time.monotonic()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    client_before = usage.ru_utime + usage.ru_stime
    raw = json.loads(subprocess.check_output(command, text=True, timeout=duration + 30))
    elapsed = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    client_cpu = (usage.ru_utime + usage.ru_stime - client_before) / elapsed
    cpu = tree_cpu(server.process.pid) - before
    codes = raw.get("statusCodeDistribution", {})
    errors = raw.get("errorDistribution", {})
    count = sum(codes.values())
    unexpected = sum(
        number for code, number in codes.items() if code != str(workload.status)
    )
    return {
        "workload": workload.name,
        "rps": raw["summary"]["requestsPerSec"],
        "p50_ms": raw["latencyPercentiles"]["p50"] * 1000,
        "p99_ms": raw["latencyPercentiles"]["p99"] * 1000,
        "responses": count,
        "unexpected_status": unexpected,
        "errors": errors,
        "elapsed_seconds": elapsed,
        "server_cpu_cores": cpu / elapsed,
        "client_cpu_cores": client_cpu,
        "client_bound": client_cpu >= 1.7,
        "server_cpu_us_per_response": cpu * 1e6 / count if count else None,
        "raw": raw,
    }


async def websocket_batch(socket, sequence):
    latencies, sent = [], deque()

    async def send():
        for index in range(sequence, sequence + 16):
            sent.append(time.monotonic())
            await socket.send(json.dumps({"id": index, "delta": "x" * 128}))

    async def receive():
        for index in range(sequence, sequence + 16):
            response = json.loads(await asyncio.wait_for(socket.recv(), 10))
            if response != {"id": index, "delta": "x" * 128}:
                raise ValueError("WebSocket echo mismatch")
            latencies.append((time.monotonic() - sent.popleft()) * 1000)

    await asyncio.gather(send(), receive())
    return latencies


async def websocket_session(url, deadline):
    from websockets.asyncio.client import connect

    latencies, sequence = [], 0
    async with connect(url, proxy=None, max_queue=16, compression=None) as socket:
        while time.monotonic() < deadline:
            latencies.extend(await websocket_batch(socket, sequence))
            sequence += 16
    return latencies


async def websocket_clients(args, server, duration):
    return await asyncio.gather(
        *(
            websocket_session(
                f"ws://127.0.0.1:{server.port}/ws", time.monotonic() + duration
            )
            for _ in range(args.ws_connections)
        )
    )


def websocket_sample(args, server, duration):
    before, started = tree_cpu(server.process.pid), time.monotonic()
    client_before = time.process_time()
    samples = asyncio.run(websocket_clients(args, server, duration))
    elapsed = time.monotonic() - started
    client_cpu = (time.process_time() - client_before) / elapsed
    cpu = tree_cpu(server.process.pid) - before
    latencies = sorted(value for sample in samples for value in sample)
    percentile = lambda fraction: latencies[math.ceil(fraction * len(latencies)) - 1]
    return {
        "workload": "websocket",
        "messages_per_second": len(latencies) / elapsed,
        "responses": len(latencies),
        "p50_ms": percentile(0.5),
        "p99_ms": percentile(0.99),
        "server_cpu_cores": cpu / elapsed,
        "client_cpu_cores": client_cpu,
        "client_bound": client_cpu >= 0.85,
        "server_cpu_us_per_response": cpu * 1e6 / len(latencies),
        "elapsed_seconds": elapsed,
        "errors": {},
    }
