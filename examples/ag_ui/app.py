"""Run with ``bessemer run examples.ag_ui.app:app --workers 1``."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ag_ui.core import (
    CustomEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from bessemer import HTMLResponse

from .adapter import create_app


async def demo_agent(run, inputs):
    """A deterministic agent with visible streaming and optional live input."""
    message_id = f"{run.run_id}-reply"
    yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
    yield TextMessageStartEvent(message_id=message_id, role="assistant")
    try:
        for delta in ("Hello ", "from ", "Bessemer. "):
            yield TextMessageContentEvent(message_id=message_id, delta=delta)
            await asyncio.sleep(0.05)
        if inputs is not None:
            yield CustomEvent(name="awaiting_input", value={"runId": run.run_id})
            value = await inputs.get()
            yield TextMessageContentEvent(message_id=message_id, delta=str(value))
        yield TextMessageEndEvent(message_id=message_id)
        yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)
    finally:
        # Real agents close model/tool streams before cancellation completes.
        await asyncio.sleep(0)


app = create_app(
    demo_agent,
    origin=os.environ.get("AG_UI_DEMO_ORIGIN", "http://127.0.0.1:8000"),
)
PAGE = Path(__file__).with_name("client.html").read_text(encoding="utf-8")


@app.get("/")
async def client():
    return HTMLResponse(PAGE)
