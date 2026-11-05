"""Echo round trips: loop.sock_* over a socketpair, streams and Protocols over TCP.

Every case runs client and server in the same loop, so one round trip is one
client send, one server receive + send and one client receive. Ops count
completed round trips.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from _harness import Case, main

SMALL = 64
LARGE = 16 * 1024


async def _sock_recv_exactly(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, size: int
) -> bytes:
    data = await loop.sock_recv(sock, size)
    if len(data) == size or not data:
        return data
    buf = bytearray(data)
    while len(buf) < size:
        chunk = await loop.sock_recv(sock, size - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


async def _sock_echo(n: int, size: int) -> int:
    loop = asyncio.get_running_loop()
    client, server = socket.socketpair()
    for s in (client, server):
        s.setblocking(False)
    payload = b"x" * size

    async def serve() -> None:
        while True:
            data = await _sock_recv_exactly(loop, server, size)
            if not data:
                return
            await loop.sock_sendall(server, data)

    server_task = loop.create_task(serve())
    try:
        for _ in range(n):
            await loop.sock_sendall(client, payload)
            reply = await _sock_recv_exactly(loop, client, size)
            if len(reply) != size:
                raise RuntimeError(f"short echo: {len(reply)} of {size} bytes")
        client.shutdown(socket.SHUT_WR)
        await server_task
    finally:
        server_task.cancel()
        client.close()
        server.close()
    return n


async def sock_echo_small(n: int) -> int:
    return await _sock_echo(n, SMALL)


async def sock_echo_large(n: int) -> int:
    return await _sock_echo(n, LARGE)


async def _streams_echo(n: int, size: int) -> int:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    data = await reader.readexactly(size)
                except asyncio.IncompleteReadError:
                    return
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    payload = b"x" * size
    try:
        for _ in range(n):
            writer.write(payload)
            await writer.drain()
            await reader.readexactly(size)
    finally:
        writer.close()
        await writer.wait_closed()
        srv.close()
        await srv.wait_closed()
    return n


async def streams_echo_small(n: int) -> int:
    return await _streams_echo(n, SMALL)


async def streams_echo_large(n: int) -> int:
    return await _streams_echo(n, LARGE)


class _EchoServer(asyncio.Protocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def data_received(self, data: bytes) -> None:
        self.transport.write(data)


class _EchoClient(asyncio.Protocol):
    def __init__(self, size: int) -> None:
        self.size = size
        self.pending = 0
        self.waiter: asyncio.Future | None = None
        self.closed: asyncio.Future = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def data_received(self, data: bytes) -> None:
        self.pending -= len(data)
        if self.pending <= 0 and self.waiter is not None and not self.waiter.done():
            self.waiter.set_result(None)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


async def _protocol_echo(n: int, size: int) -> int:
    loop = asyncio.get_running_loop()
    srv = await loop.create_server(_EchoServer, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    transport, proto = await loop.create_connection(
        lambda: _EchoClient(size), "127.0.0.1", port
    )
    payload = b"x" * size
    try:
        for _ in range(n):
            proto.pending = size
            proto.waiter = loop.create_future()
            transport.write(payload)
            await proto.waiter
    finally:
        transport.close()
        await proto.closed
        srv.close()
        await srv.wait_closed()
    return n


async def protocol_echo_small(n: int) -> int:
    return await _protocol_echo(n, SMALL)


async def protocol_echo_large(n: int) -> int:
    return await _protocol_echo(n, LARGE)


CASES = [
    Case("sock_echo_64B", 20_000, sock_echo_small, "rtt"),
    Case("sock_echo_16KiB", 12_000, sock_echo_large, "rtt"),
    Case("streams_echo_64B", 12_000, streams_echo_small, "rtt"),
    Case("streams_echo_16KiB", 8_000, streams_echo_large, "rtt"),
    Case("protocol_echo_64B", 20_000, protocol_echo_small, "rtt"),
    Case("protocol_echo_16KiB", 10_000, protocol_echo_large, "rtt"),
]

if __name__ == "__main__":
    sys.exit(main("sockets", CASES))
