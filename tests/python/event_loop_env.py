"""Let a whole test run force an event loop with ``BESSEMER_EVENT_LOOP``.

The variable only reaches ``config.settings()`` (so ``bessemer run``). Servers
the suite starts with ``TestClient`` or ``App._run``, and the settings the
supervisor tests build from ``DEFAULTS``, take the library's default instead.
When the variable names a valid loop, ``apply()`` makes that loop the default
for those too, in this process and, through ``DEFAULTS``, in the workers the
supervisor tests start. It changes no library code or public API. Tests that
assert the shipped default use ``shipped_default()``.
"""

from __future__ import annotations

import contextlib
import os

from bessemer import App, config
from bessemer.worker import Worker

SHIPPED = config.SERVER_DEFAULTS["event_loop"]
_DICTS = (config.SERVER_DEFAULTS, config.DEFAULTS)
_FUNCTIONS = (App.run, App._run, Worker.__init__)


def forced() -> str | None:
    value = os.environ.get("BESSEMER_EVENT_LOOP")
    return value if value in config.EVENT_LOOPS else None


def _set(value: str) -> None:
    for defaults in _DICTS:
        defaults["event_loop"] = value
    for function in _FUNCTIONS:
        function.__kwdefaults__["event_loop"] = value


def apply() -> None:
    value = forced()
    if value is not None:
        _set(value)


@contextlib.contextmanager
def shipped_default():
    """Run with the library's own default, whatever the run forces."""
    current = config.SERVER_DEFAULTS["event_loop"]
    _set(SHIPPED)
    try:
        yield
    finally:
        _set(current)
