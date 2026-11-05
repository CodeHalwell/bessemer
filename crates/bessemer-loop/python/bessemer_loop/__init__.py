"""An asyncio event loop with its scheduler and reactor in Rust (a spike).

The Rust base class ``LoopCore`` provides the ready queue, the timer heap,
epoll registration, ``run_forever`` and the socket fast paths. ``Loop``
combines it with asyncio's Unix selector loop, whose high-level APIs
(servers, transports, streams, subprocesses, signals, executors, the
exception handler) then run on the Rust primitives unchanged.
"""

from __future__ import annotations

import asyncio
from asyncio import base_events, format_helpers, unix_events

from ._loop import Handle, LoopCore

__all__ = ["EventLoopPolicy", "Handle", "Loop", "install", "new_event_loop"]


class Loop(LoopCore, unix_events._UnixSelectorEventLoop):
    """An asyncio event loop scheduled by Rust."""

    def __init__(self) -> None:
        unix_events._UnixSelectorEventLoop.__init__(self)
        # Cross-thread wake-ups write to asyncio's self-pipe from Rust.
        self._core_set_wakeup_fd(self._csock.fileno())

    def set_debug(self, enabled: bool) -> None:
        base_events.BaseEventLoop.set_debug(self, enabled)
        self._core_set_debug(bool(enabled))

    def close(self) -> None:
        if self.is_running():
            raise RuntimeError("Cannot close a running event loop")
        if self._core_is_closed():
            return
        self._core_set_wakeup_fd(-1)
        # Close the self-pipe and selector (asyncio's own close) while the
        # Rust registrations still exist, then drop the Rust state.
        try:
            super().close()
        finally:
            self._core_close()

    def is_closed(self) -> bool:
        return self._core_is_closed()

    async def sock_recv(self, sock, n):
        result = self._bessemer_sock_recv(sock, n)
        if type(result) is bytes:
            return result
        return await result

    async def sock_sendall(self, sock, data):
        result = self._bessemer_sock_sendall(sock, data)
        if result is None:
            return None
        return await result

    def _bessemer_report_handle_error(self, handle, exc, callback, args) -> None:
        source = format_helpers._format_callback_source(callback, args)
        self.call_exception_handler(
            {
                "message": f"Exception in callback {source}",
                "exception": exc,
                "handle": handle,
            }
        )

    def _bessemer_create_task_slow(self, coro, **kwargs):
        return base_events.BaseEventLoop.create_task(self, coro, **kwargs)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} running={self.is_running()} "
            f"closed={self.is_closed()} debug={self.get_debug()}>"
        )


def new_event_loop() -> Loop:
    """Return a new Rust-scheduled event loop."""
    return Loop()


class EventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """A policy whose new loops are ``Loop`` instances."""

    def _loop_factory(self) -> Loop:
        return Loop()

    def new_event_loop(self) -> Loop:
        return Loop()


def install() -> None:
    """Make ``Loop`` the default for ``asyncio.run`` and ``new_event_loop``."""
    asyncio.set_event_loop_policy(EventLoopPolicy())
