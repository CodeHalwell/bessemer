"""Cold invocation resources coexist without affecting ordinary requests."""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from bessemer import App, BackgroundTasks, Request
from bessemer.testclient import TestClient


class InvocationResourcesTests(unittest.TestCase):
    def test_receive_watch_and_deferred_sync_background_coexist(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._coexist(loop)

    def _coexist(self, loop):
        app = App()
        seen = []
        lock = threading.Lock()

        def record(value):
            with lock:
                seen.append(value)

        @app.post("/read")
        async def read(request: Request, tasks: BackgroundTasks):
            message = await request.receive()
            self.assertFalse(await request.is_disconnected())
            value = int(message["body"])
            tasks.add_task(record, value)
            return {"value": value}

        with TestClient(app, server_options={"event_loop": loop}) as client:

            def send(value):
                response = client.post("/read", content=str(value).encode())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"value": value})

            with ThreadPoolExecutor(max_workers=8) as clients:
                list(clients.map(send, range(32)))
            deadline = time.monotonic() + 3
            while True:
                with lock:
                    count = len(seen)
                if count == 32 or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(sorted(seen), list(range(32)))
