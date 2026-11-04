"""Check cancellation after the client abandons an in-flight HTTP request."""

import importlib.util
import socket
import time
import unittest
from unittest.mock import patch

import test_native_integration

DISCONNECT_ROUTE = """
@app.get("/disconnect", timeout=2)
async def disconnect():
    Path("disconnect_started").touch()
    try:
        await asyncio.sleep(30)
    finally:
        Path("disconnect_cleaned").touch()
"""


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class DisconnectTests(unittest.TestCase):
    def test_disconnected_client_cancels_async_handler(self):
        with patch.object(
            test_native_integration,
            "APPLICATION",
            test_native_integration.APPLICATION + DISCONNECT_ROUTE,
        ):
            server = test_native_integration.Server()
        try:
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=3
            ) as connection:
                connection.sendall(
                    b"GET /disconnect HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                started = server.root / "disconnect_started"
                deadline = time.monotonic() + 3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(started.exists())
                connection.shutdown(socket.SHUT_RDWR)
            cleaned = server.root / "disconnect_cleaned"
            # This is substantially shorter than the two-second route deadline,
            # so a timeout cannot masquerade as disconnect cancellation.
            deadline = time.monotonic() + 0.75
            while not cleaned.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(
                cleaned.exists(),
                "disconnect did not cancel the handler before its route deadline",
            )
            self.assertEqual(server.request("GET", "/items/1")[0], 200)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
