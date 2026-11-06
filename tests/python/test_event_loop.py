"""Behaviour tests for bessemer._loop.Loop (run against the installed wheel)."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import http.client
import json
import os
import signal
import socket
import threading
import time
import unittest
import weakref
from contextlib import asynccontextmanager
from typing import ClassVar

from bessemer import App, BackgroundTasks, Request, WebSocket
from bessemer.responses import StreamingResponse
from bessemer.testclient import TestClient
from bessemer.worker import Worker

try:
    from bessemer import _loop as bessemer_loop
except ImportError as error:  # No native extension, or not Linux.
    raise unittest.SkipTest(f"the Rust event loop is unavailable: {error}") from error


def _open_descriptors() -> int:
    """How many descriptors this process has open (Linux and macOS)."""
    directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return len(os.listdir(directory))


class LoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = bessemer_loop.new_event_loop()
        self.addCleanup(self.loop.close)

    def run_soon(self, coro):
        return self.loop.run_until_complete(coro)


class SchedulingTests(LoopTestCase):
    def test_is_an_asyncio_loop(self) -> None:
        self.assertIsInstance(self.loop, asyncio.AbstractEventLoop)
        self.assertIsInstance(self.loop, asyncio.BaseEventLoop)

    def test_is_running_only_while_it_runs(self) -> None:
        seen = []

        async def probe():
            seen.append(self.loop.is_running())
            # An eager task starts at once only on a running loop.
            task = asyncio.Task(
                asyncio.sleep(0, "ran"), loop=self.loop, eager_start=True
            )
            seen.append(task.done())
            return await task

        self.assertFalse(self.loop.is_running())
        self.assertEqual(self.run_soon(probe()), "ran")
        self.assertFalse(self.loop.is_running())
        self.assertEqual(seen, [True, False])
        eager = asyncio.Task(asyncio.sleep(0), loop=self.loop, eager_start=True)
        # Not running: the eager start falls back to being scheduled.
        self.assertFalse(eager.done())
        self.run_soon(eager)
        self.assertTrue(eager.done())

    def test_call_soon_runs_in_fifo_order(self) -> None:
        calls = []
        for i in range(5):
            self.loop.call_soon(calls.append, i)
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(calls, [0, 1, 2, 3, 4])

    def test_callbacks_added_while_running_wait_for_next_iteration(self) -> None:
        calls = []

        def first():
            calls.append("first")
            self.loop.call_soon(calls.append, "nested")
            self.loop.stop()

        self.loop.call_soon(first)
        self.loop.call_soon(calls.append, "second")
        self.loop.run_forever()
        # stop() lets the current iteration finish but not the next one.
        self.assertEqual(calls, ["first", "second"])
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(calls, ["first", "second", "nested"])

    def test_cancelled_handle_does_not_run(self) -> None:
        calls = []
        handle = self.loop.call_soon(calls.append, 1)
        handle.cancel()
        self.assertTrue(handle.cancelled())
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(calls, [])

    def test_timers_fire_in_time_order(self) -> None:
        calls = []
        self.loop.call_later(0.03, calls.append, 3)
        self.loop.call_later(0.01, calls.append, 1)
        self.loop.call_at(self.loop.time() + 0.02, calls.append, 2)
        self.loop.call_later(0.04, self.loop.stop)
        start = time.monotonic()
        self.loop.run_forever()
        self.assertEqual(calls, [1, 2, 3])
        self.assertGreaterEqual(time.monotonic() - start, 0.04)

    def test_timer_handle_reports_when(self) -> None:
        when = self.loop.time() + 10
        handle = self.loop.call_at(when, lambda: None)
        self.assertEqual(handle.when(), when)
        handle.cancel()

    def test_many_cancelled_timers_are_compacted(self) -> None:
        handles = [self.loop.call_later(100, lambda: None) for _ in range(1000)]
        for handle in handles[:900]:
            handle.cancel()
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertFalse(any(h._scheduled for h in handles[:900]))
        self.assertTrue(all(h._scheduled for h in handles[900:]))

    def test_none_delay_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.loop.call_later(None, lambda: None)
        with self.assertRaises(TypeError):
            self.loop.call_at(None, lambda: None)

    def test_callbacks_run_in_their_context(self) -> None:
        var = contextvars.ContextVar("var", default="unset")
        seen = []
        context = contextvars.copy_context()
        context.run(var.set, "given")
        self.loop.call_soon(lambda: seen.append(var.get()), context=context)
        var.set("current")
        self.loop.call_soon(lambda: seen.append(var.get()))
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(seen, ["given", "current"])

    def test_callback_errors_go_to_the_exception_handler(self) -> None:
        reports = []
        self.loop.set_exception_handler(lambda loop, ctx: reports.append(ctx))

        def fail():
            raise ValueError("boom")

        handle = self.loop.call_soon(fail)
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(len(reports), 1)
        self.assertIsInstance(reports[0]["exception"], ValueError)
        self.assertIs(reports[0]["handle"], handle)
        self.assertIn("Exception in callback", reports[0]["message"])

    def test_keyboard_interrupt_propagates(self) -> None:
        def interrupt():
            raise KeyboardInterrupt

        self.loop.call_soon(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.loop.run_forever()
        self.assertFalse(self.loop.is_running())

    def test_call_soon_threadsafe_wakes_the_loop(self) -> None:
        done = []

        def from_thread():
            time.sleep(0.05)
            self.loop.call_soon_threadsafe(done.append, "woken")
            self.loop.call_soon_threadsafe(self.loop.stop)

        thread = threading.Thread(target=from_thread)
        thread.start()
        self.loop.run_forever()
        thread.join()
        self.assertEqual(done, ["woken"])

    def test_closed_loop_rejects_callbacks(self) -> None:
        self.loop.close()
        self.assertTrue(self.loop.is_closed())
        with self.assertRaises(RuntimeError):
            self.loop.call_soon(lambda: None)
        with self.assertRaises(RuntimeError):
            self.loop.call_later(1, lambda: None)

    def test_close_releases_the_epoll_descriptor(self) -> None:
        before = _open_descriptors()
        loops = [bessemer_loop.new_event_loop() for _ in range(20)]
        for loop in loops:
            loop.close()
        # The loops are still referenced; their descriptors must be gone.
        self.assertLessEqual(_open_descriptors(), before)
        del loops

    def test_close_releases_the_embedded_runtime(self) -> None:
        def run_and_close(loop) -> None:
            loop._core_enable_runtime()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

        # Tokio creates a process-wide socket pair once; count after it.
        run_and_close(bessemer_loop.new_event_loop())
        before = _open_descriptors()
        loops = [bessemer_loop.new_event_loop() for _ in range(20)]
        for loop in loops:
            run_and_close(loop)
        # The runtimes' reactors and epoll duplicates must be gone too.
        self.assertLessEqual(_open_descriptors(), before)
        del loops

    def test_embedded_runtime_reruns_a_reader_that_leaves_data(self) -> None:
        loop = bessemer_loop.new_event_loop()
        self.addCleanup(loop.close)
        loop._core_enable_runtime()
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        reader.setblocking(False)
        received = []

        async def main():
            done = loop.create_future()

            def on_readable():
                # One byte per call: the rest stays readable, with no new
                # edge on the loop's epoll descriptor.
                received.append(reader.recv(1))
                if len(received) == 3:
                    loop.remove_reader(reader.fileno())
                    done.set_result(None)

            loop.add_reader(reader.fileno(), on_readable)
            writer.send(b"abc")
            await asyncio.wait_for(done, 5)

        loop.run_until_complete(main())
        self.assertEqual(b"".join(received), b"abc")

    def test_embedded_runtime_serves_1000_sequential_requests_without_a_stall(
        self,
    ) -> None:
        loop = bessemer_loop.new_event_loop()
        self.addCleanup(loop.close)
        loop._core_enable_runtime()
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        client.setblocking(False)
        server.setblocking(False)
        requests = 1000
        replies = []
        started = time.monotonic()

        async def main():
            done = loop.create_future()

            def serve():
                data = server.recv(64)
                # Answered from a callback queued by the first, as a handler
                # that finishes after the loop has polled once more.
                loop.call_soon(server.send, b"R" * len(data))

            def receive():
                replies.append(client.recv(64))
                if len(replies) == requests:
                    done.set_result(None)
                else:
                    client.send(b"Q")

            loop.add_reader(server.fileno(), serve)
            loop.add_reader(client.fileno(), receive)
            client.send(b"Q")
            await asyncio.wait_for(done, 30)

        loop.run_until_complete(main())
        self.assertEqual(len(replies), requests)
        # A lost wake-up would cost a timeout; 1,000 exchanges take milliseconds.
        self.assertLess(time.monotonic() - started, 10)

    def test_embedded_runtime_serves_1000_sequential_requests_on_one_connection(
        self,
    ) -> None:
        loop = bessemer_loop.new_event_loop()
        self.addCleanup(loop.close)
        loop._core_enable_runtime()
        requests = 1000

        async def main():
            async def handle(reader, writer):
                try:
                    while await reader.readline():
                        writer.write(b"pong\n")
                        await writer.drain()
                finally:
                    writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(requests):
                writer.write(b"ping\n")
                self.assertEqual(
                    await asyncio.wait_for(reader.readline(), 5), b"pong\n"
                )
            writer.close()
            server.close()
            await server.wait_closed()

        loop.run_until_complete(main())

    def test_debug_mode_checks_callbacks(self) -> None:
        self.loop.set_debug(True)
        self.assertTrue(self.loop.get_debug())

        async def coro():
            pass

        with self.assertRaises(TypeError):
            self.loop.call_soon(coro)

    def test_handle_cycles_are_collected(self) -> None:
        class Holder:
            pass

        holder = Holder()
        holder.handle = self.loop.call_later(100, lambda h=holder: h)
        ref = weakref.ref(holder)
        holder.handle.cancel()
        del holder
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        gc.collect()
        self.assertIsNone(ref())


class TaskTests(LoopTestCase):
    def test_tasks_futures_and_sleep(self) -> None:
        async def child(i):
            await asyncio.sleep(0.001)
            return i * 2

        async def main():
            self.assertIs(asyncio.get_running_loop(), self.loop)
            future = self.loop.create_future()
            self.loop.call_later(0.001, future.set_result, "ok")
            results = await asyncio.gather(*(child(i) for i in range(10)))
            return await future, results

        self.assertEqual(self.run_soon(main()), ("ok", [i * 2 for i in range(10)]))

    def test_task_factory_is_honoured(self) -> None:
        made = []

        def factory(loop, coro, **kwargs):
            task = asyncio.Task(coro, loop=loop, **kwargs)
            made.append(task)
            return task

        self.loop.set_task_factory(factory)

        async def main():
            return 1

        self.assertEqual(self.run_soon(main()), 1)
        self.assertEqual(len(made), 1)

    def test_cancellation_and_timeouts(self) -> None:
        async def main():
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await asyncio.sleep(10)
            task = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.run_soon(main())

    def test_run_in_executor(self) -> None:
        async def main():
            return await self.loop.run_in_executor(None, threading.get_ident)

        self.assertNotEqual(self.run_soon(main()), threading.get_ident())

    def test_policy_drives_asyncio_run(self) -> None:
        old = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(bessemer_loop.EventLoopPolicy())
        try:

            async def main():
                return type(asyncio.get_running_loop())

            self.assertIs(asyncio.run(main()), bessemer_loop.Loop)
        finally:
            asyncio.set_event_loop_policy(old)


class IoTests(LoopTestCase):
    def socketpair(self):
        a, b = socket.socketpair()
        a.setblocking(False)
        b.setblocking(False)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def test_add_and_remove_reader(self) -> None:
        a, b = self.socketpair()
        got = []

        def readable():
            got.append(b.recv(100))
            self.loop.remove_reader(b)
            self.loop.stop()

        self.loop.add_reader(b, readable)
        a.send(b"ping")
        self.loop.run_forever()
        self.assertEqual(got, [b"ping"])
        self.assertFalse(self.loop.remove_reader(b))

    def test_reader_and_writer_on_one_fd(self) -> None:
        a, b = self.socketpair()
        events = []

        def writable():
            events.append("w")
            self.loop.remove_writer(a)
            a.send(b"x")

        def readable():
            events.append("r")
            a.recv(10)
            self.loop.remove_reader(a)
            self.loop.stop()

        self.loop.add_reader(a, readable)
        self.loop.add_writer(a, writable)
        b.send(b"y")
        self.loop.run_forever()
        self.assertEqual(sorted(events), ["r", "w"])

    def test_replacing_a_reader_cancels_the_old_handle(self) -> None:
        a, _ = self.socketpair()
        first = self.loop._add_reader(a.fileno(), lambda: None)
        second = self.loop._add_reader(a.fileno(), lambda: None)
        self.assertTrue(first.cancelled())
        self.assertFalse(second.cancelled())
        self.assertTrue(self.loop._remove_reader(a.fileno()))

    def test_sock_recv_and_sendall(self) -> None:
        a, b = self.socketpair()
        payload = os.urandom(4 * 1024 * 1024)

        async def main():
            receiver = asyncio.ensure_future(self.read_exactly(b, len(payload)))
            await self.loop.sock_sendall(a, payload)
            return await receiver

        self.assertEqual(self.run_soon(main()), payload)
        self.assertEqual(self.loop._bessemer_io_state(a.fileno()), (False, False))
        self.assertEqual(self.loop._bessemer_io_state(b.fileno()), (False, False))

    def test_sock_sendall_snapshots_writable_buffers(self) -> None:
        async def send_and_mutate(a, b, view, filled):
            backing = bytearray(b"before")
            task = asyncio.create_task(self.loop.sock_sendall(a, view(backing)))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            backing[:] = b"change"
            while filled:
                filled -= len(b.recv(filled))
            await asyncio.wait_for(task, 1)
            return await self.read_exactly(b, len(backing))

        for name, view in (
            ("bytearray", lambda data: data),
            ("memoryview", memoryview),
            ("readonly view", lambda data: memoryview(data).toreadonly()),
        ):
            with self.subTest(name=name):
                a, b = self.socketpair()
                a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                filled = 0
                while True:
                    try:
                        filled += a.send(b"x" * 65536)
                    except BlockingIOError:
                        break
                self.assertEqual(
                    self.run_soon(send_and_mutate(a, b, view, filled)), b"before"
                )

    async def read_exactly(self, sock, n):
        chunks = []
        while n:
            chunk = await self.loop.sock_recv(sock, min(n, 65536))
            self.assertTrue(chunk)
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def test_sock_recv_is_a_coroutine_and_cancellable(self) -> None:
        _, b = self.socketpair()

        async def main():
            task = asyncio.create_task(self.loop.sock_recv(b, 10))
            await asyncio.sleep(0.01)
            self.assertEqual(self.loop._bessemer_io_state(b.fileno()), (True, False))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.loop._bessemer_io_state(b.fileno()), (False, False))

        self.run_soon(main())

    def test_zero_length_recv_returns_at_once(self) -> None:
        _, b = self.socketpair()
        self.assertEqual(self.run_soon(self.loop.sock_recv(b, 0)), b"")

    def test_sock_recv_errors_are_os_errors(self) -> None:
        a, b = self.socketpair()
        a.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
        )

        async def main():
            await self.loop.sock_sendall(b, b"unread")
            await asyncio.sleep(0.01)
            a.close()
            return await self.loop.sock_recv(b, 10)

        # A closed peer reads as EOF or a reset, never as a bare exception.
        try:
            self.assertEqual(self.run_soon(main()), b"")
        except ConnectionResetError as exc:
            self.assertEqual(exc.errno, 104)

    def test_sock_recv_rejects_blocking_socket_in_debug(self) -> None:
        a, _ = self.socketpair()
        a.setblocking(True)
        self.loop.set_debug(True)
        with self.assertRaises(ValueError):
            self.run_soon(self.loop.sock_recv(a, 10))

    def test_streams_server_round_trip(self) -> None:
        async def handle(reader, writer):
            writer.write((await reader.readline()).upper())
            await writer.drain()
            writer.close()

        async def main():
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"hello\n")
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()
            return line

        self.assertEqual(self.run_soon(main()), b"HELLO\n")

    def test_subprocess(self) -> None:
        async def main():
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", "echo hi", stdout=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()
            return out, proc.returncode

        self.assertEqual(self.run_soon(main()), (b"hi\n", 0))

    def test_signal_handler(self) -> None:
        got = []

        def on_signal():
            got.append("usr1")
            self.loop.stop()

        self.loop.add_signal_handler(signal.SIGUSR1, on_signal)
        self.addCleanup(self.loop.remove_signal_handler, signal.SIGUSR1)
        self.loop.call_later(0.01, os.kill, os.getpid(), signal.SIGUSR1)
        self.loop.call_later(5, self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(got, ["usr1"])


class ServerTests(unittest.TestCase):
    """The server's workers on the Rust loop (``event_loop="bessemer"``)."""

    def test_short_timers_rearm_after_cancellation_and_ready_work(self) -> None:
        app = App()

        @app.get("/timers")
        async def timers():
            loop = asyncio.get_running_loop()
            calls = []
            for delay in (0.0001, 0.001, 0.00025, 0.02):
                future = loop.create_future()
                cancelled = loop.call_later(delay, calls.append, "cancelled")
                cancelled.cancel()
                started = loop.time()
                loop.call_later(delay, future.set_result, None)
                loop.call_soon(calls.append, "ready")
                await future
                self.assertGreaterEqual(loop.time() - started, delay)
                calls.append("timer")
            return {"calls": calls}

        with TestClient(app, server_options={"event_loop": "bessemer"}) as client:
            for _ in range(3):
                response = client.get("/timers")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"calls": ["ready", "timer"] * 4})

    def test_requests_streams_sync_handlers_and_background_work(self) -> None:

        app = App()
        done = threading.Event()
        seen = {}

        @app.get("/items/{item_id}")
        async def item(item_id: int, q: str = "x"):
            seen["loop"] = type(asyncio.get_running_loop())
            await asyncio.sleep(0.01)
            return {"id": item_id, "q": q}

        @app.get("/sync")
        def sync():
            return {"thread": threading.current_thread().name}

        @app.get("/stream")
        async def stream():
            async def chunks():
                for part in (b"a", b"b", b"c"):
                    await asyncio.sleep(0)
                    yield part

            return StreamingResponse(chunks())

        @app.get("/later")
        async def later(background: BackgroundTasks):
            background.add_task(done.set)
            return {"ok": True}

        with TestClient(app, server_options={"event_loop": "bessemer"}) as client:
            response = client.get("/items/7?q=y")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"id": 7, "q": "y"})
            self.assertIs(seen["loop"], bessemer_loop.Loop)
            self.assertEqual(client.get("/items/x").status_code, 422)
            self.assertEqual(client.get("/sync").status_code, 200)
            self.assertEqual(client.get("/stream").content, b"abc")
            self.assertEqual(client.get("/later").json(), {"ok": True})
            self.assertTrue(done.wait(5))

    @unittest.skipUnless(
        os.path.isdir("/proc/self/task"), "counts runtime threads through /proc"
    )
    def test_one_loop_serves_connections_on_its_own_thread(self) -> None:
        def runtime_threads() -> int:
            names = []
            for task in os.listdir("/proc/self/task"):
                try:
                    with open(f"/proc/self/task/{task}/comm") as comm:
                        names.append(comm.read().strip())
                except OSError:  # The thread has exited.
                    continue
            return sum(name.startswith("tokio-") for name in names)

        app = App()

        @app.get("/echo/{value}")
        async def echo(value: str):
            return {"value": value}

        before = runtime_threads()
        options = {"event_loop": "bessemer"}
        with TestClient(app, server_options=options) as client:
            for value in ("a", "b", "c"):
                self.assertEqual(client.get(f"/echo/{value}").json(), {"value": value})
            # No server runtime threads: the loop's own thread serves.
            self.assertEqual(runtime_threads(), before)
        with TestClient(app) as client:
            self.assertEqual(client.get("/echo/d").json(), {"value": "d"})
            self.assertGreater(runtime_threads(), before)

    def test_several_loops_and_a_lifespan(self) -> None:

        events = []

        @asynccontextmanager
        async def lifespan(app):
            events.append(type(asyncio.get_running_loop()))
            yield
            events.append("stopped")

        app = App(lifespan=lifespan)

        @app.get("/loop")
        async def which():
            return {"loop": type(asyncio.get_running_loop()).__name__}

        options = {"event_loop": "bessemer", "loops": 2}
        with TestClient(app, server_options=options) as client:
            names = {client.get("/loop").json()["loop"] for _ in range(20)}
        self.assertEqual(names, {"Loop"})
        self.assertEqual(events, [bessemer_loop.Loop, "stopped"])

    def test_the_choice_is_checked(self) -> None:
        with self.assertRaises(ValueError):
            Worker(App(), event_loop="uvloop")
        worker = Worker(App(), event_loop="bessemer")
        try:
            self.assertIs(type(worker._loop), bessemer_loop.Loop)
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()


def _io_uring_expected() -> bool:
    """Whether this process should be able to use io_uring: Linux with the
    sysctl allowing it and no seccomp filter (which might block it)."""
    try:
        with open("/proc/sys/kernel/io_uring_disabled") as sysctl:
            if sysctl.read().strip() != "0":
                return False
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("Seccomp:"):
                    return line.split()[1] == "0"
    except OSError:
        return False
    return False


def _io_uring_rings() -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == "anon_inode:[io_uring]":
                count += 1
        except OSError:
            pass
    return count


class IoUringServerTests(unittest.TestCase):
    """The single-thread worker with ``io_backend="io_uring"``. Where io_uring
    is unavailable the server falls back to epoll and these still pass."""

    options: ClassVar[dict[str, str]] = {
        "event_loop": "bessemer",
        "io_backend": "io_uring",
    }

    def make_app(self) -> App:
        app = App()

        @app.get("/items/{item_id}")
        async def item(item_id: int, q: str = "x"):
            await asyncio.sleep(0)
            return {"id": item_id, "q": q}

        @app.get("/big")
        async def big(size: int):
            return StreamingResponse(iter([b"x" * size]))

        @app.post("/length")
        async def length(request: Request):
            body = await request.body()
            return {"length": len(body), "sum": sum(body) % 251}

        @app.get("/stream")
        async def stream():
            async def chunks():
                for part in (b"a", b"b", b"c"):
                    await asyncio.sleep(0)
                    yield part

            return StreamingResponse(chunks())

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            text = await websocket.receive_text()
            await websocket.send_text(text.upper())
            await websocket.close()

        return app

    def test_requests_bodies_streams_and_websockets(self) -> None:
        with TestClient(self.make_app(), server_options=self.options) as client:
            if _io_uring_expected():
                self.assertGreaterEqual(_io_uring_rings(), 1)
            for i in range(50):
                response = client.get(f"/items/{i}?q=y")
                self.assertEqual(response.json(), {"id": i, "q": "y"})
            self.assertEqual(client.get("/items/x").status_code, 422)
            body = bytes(i % 256 for i in range(900_000))
            self.assertEqual(
                client.post("/length", content=body).json(),
                {"length": len(body), "sum": sum(body) % 251},
            )
            self.assertEqual(len(client.get("/big?size=3000000").content), 3_000_000)
            self.assertEqual(client.get("/stream").content, b"abc")
            with client.websocket_connect("/ws") as websocket:
                websocket.send_text("hello")
                self.assertEqual(websocket.receive_text(), "HELLO")

    def test_concurrent_connections_and_abandoned_requests(self) -> None:
        with TestClient(self.make_app(), server_options=self.options) as client:
            # Clients that send half a request, or a request, and vanish.
            for payload in (
                b"GET /items/1 HTTP/1.1\r\nHo",
                b"GET /items/1 HTTP/1.1\r\n\r\n",
            ):
                sock = socket.create_connection(("127.0.0.1", client._port))
                sock.sendall(payload)
                sock.close()

            def call(index: int) -> None:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", client._port, timeout=10
                )
                try:
                    for round_ in range(20):
                        connection.request("GET", f"/items/{index}?q={round_}")
                        response = connection.getresponse()
                        self.assertEqual(
                            json.loads(response.read()), {"id": index, "q": str(round_)}
                        )
                finally:
                    connection.close()

            threads = [threading.Thread(target=call, args=(i,)) for i in range(32)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(client.get("/items/9").json(), {"id": 9, "q": "x"})
