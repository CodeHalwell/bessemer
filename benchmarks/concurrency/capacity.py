"""Verify sustained concurrency separately from burst connection establishment."""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from server import Server, Variant, resolve_executable


@dataclass
class Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def request(self, path, expected):
        self.writer.write(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n".encode()
        )
        await self.writer.drain()
        async with asyncio.timeout(15):
            head = await self.reader.readuntil(b"\r\n\r\n")
            status, *lines = head.decode("latin-1").split("\r\n")
            headers = {
                key.lower(): value.strip()
                for key, value in (line.split(":", 1) for line in lines if line)
            }
            body = await self.reader.readexactly(int(headers["content-length"]))
        if status.split()[1] != "200" or json.loads(body) != expected:
            raise ValueError(f"unexpected HTTP response: {status}")

    async def run(self, path, expected, deadline):
        count = 0
        while time.monotonic() < deadline:
            await self.request(path, expected)
            count += 1
        return count

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


async def connect(port, limit):
    async with limit:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        peer = Peer(reader, writer)
        try:
            await peer.request("/healthz", {"ok": True})
        except BaseException:
            await peer.close()
            raise
        return peer


async def verify(port, count, duration):
    limit = asyncio.Semaphore(16)
    peers = []
    try:
        connected = await asyncio.gather(
            *(connect(port, limit) for _ in range(count)), return_exceptions=True
        )
        peers = [peer for peer in connected if isinstance(peer, Peer)]
        failures = [str(peer) for peer in connected if isinstance(peer, BaseException)]
        phases = []
        for path, expected in (("/io", {"ok": True}), ("/fanout", {"completed": 16})):
            deadline = time.monotonic() + duration
            results = await asyncio.gather(
                *(peer.run(path, expected, deadline) for peer in peers),
                return_exceptions=True,
            )
            errors = [
                str(result) for result in results if isinstance(result, BaseException)
            ]
            phases.append(
                {
                    "path": path,
                    "connections": len(peers),
                    "completed": sum(
                        value for value in results if isinstance(value, int)
                    ),
                    "errors": errors,
                }
            )
        return {"connected": len(peers), "setup_errors": failures, "phases": phases}
    finally:
        await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)


def main(args):
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    variants = (
        Variant("candidate-default", args.python),
        Variant("candidate-rust", args.python, loop="bessemer"),
        Variant("fastapi", args.python, framework="fastapi"),
    )
    results = {"settings": vars(args), "samples": []}
    for variant in variants:
        with (
            (output.parent / f"{variant.name}.log").open("w") as log,
            Server(variant, log) as server,
        ):
            sample = asyncio.run(verify(server.port, args.connections, args.duration))
        sample.update(variant=variant.name, versions=variant.versions())
        results["samples"].append(sample)
        output.write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(sample), flush=True)
    if any(
        sample["setup_errors"] or any(phase["errors"] for phase in sample["phases"])
        for sample in results["samples"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=resolve_executable)
    parser.add_argument("--connections", type=int, default=1024)
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if min(args.connections, args.duration) < 1:
        parser.error("connections and duration must be positive")
    main(args)
