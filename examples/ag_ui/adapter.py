"""AG-UI SSE and duplex WebSocket example using the official optional SDK."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from ag_ui.core import BaseEvent, Event, RunAgentInput, RunErrorEvent
from ag_ui.encoder import EventEncoder
from bessemer import CORS, App, StreamingResponse, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

Agent = Callable[[RunAgentInput, asyncio.Queue[Any] | None], AsyncIterator[BaseEvent]]
EVENT_ADAPTER = TypeAdapter(Event)
LOGGER = logging.getLogger(__name__)


async def validated_events(run: RunAgentInput, agent: Agent, inputs=None):
    source = agent(run, inputs)
    try:
        async for event in source:
            value = (
                event.model_dump(by_alias=True)
                if isinstance(event, BaseEvent)
                else event
            )
            yield EVENT_ADAPTER.validate_python(value)
    finally:
        await source.aclose()


async def sse_events(run: RunAgentInput, agent: Agent):
    encoder = EventEncoder()
    events = validated_events(run, agent)
    try:
        async for event in events:
            yield encoder.encode(event)
    finally:
        await events.aclose()


async def stop_task(task):
    """Wait for iterator cleanup, even if the session is cancelled again."""
    if task is None:
        return
    caller = asyncio.current_task()
    baseline = caller.cancelling()
    interrupted = False
    if not task.done():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = interrupted or caller.cancelling() > baseline
        except BaseException:  # noqa: BLE001
            break
    if not task.cancelled():
        task.exception()
    if interrupted:
        raise asyncio.CancelledError


async def send_events(websocket, run, agent, inputs):
    events = validated_events(run, agent, inputs)
    terminal = None
    try:
        async for event in events:
            if terminal is not None:
                raise ValueError("agent emitted an event after its terminal event")
            if event.type in ("RUN_FINISHED", "RUN_ERROR"):
                # Publish completion only after the agent iterator has closed.
                terminal = event
                continue
            await websocket.send_text(event.model_dump_json(by_alias=True))
    except (asyncio.CancelledError, WebSocketDisconnect):
        raise
    except BaseException as error:  # noqa: BLE001
        LOGGER.error("AG-UI agent failed (%s)", type(error).__name__)
        terminal = RunErrorEvent(message="Agent execution failed", code="AGENT_ERROR")
    finally:
        await events.aclose()
    if terminal is not None:
        await websocket.send_text(terminal.model_dump_json(by_alias=True))


class AgentSession:
    """Loop-owned state for one active run and its bounded input channel."""

    def __init__(self, websocket, agent):
        self.websocket = websocket
        self.agent = agent
        self.clear()

    def clear(self):
        self.active = self.run = self.inputs = None

    def reap(self, done):
        if self.active is not None and self.active in done:
            self.active.result()
            self.clear()

    def start(self, payload):
        if self.active is not None:
            raise ValueError("only one active run is allowed")
        self.run = RunAgentInput.model_validate(payload)
        self.inputs = asyncio.Queue(maxsize=1)
        self.active = asyncio.create_task(
            send_events(self.websocket, self.run, self.agent, self.inputs)
        )

    async def control(self, payload):
        if self.active is None or payload.get("runId") != self.run.run_id:
            raise ValueError("control must identify the active run")
        if payload["control"] == "cancel" and set(payload) == {"control", "runId"}:
            await stop_task(self.active)
            self.clear()
            event = RunErrorEvent(
                message="Run cancelled by client", code="CLIENT_CANCELLED"
            )
            await self.websocket.send_text(event.model_dump_json(by_alias=True))
        elif payload["control"] == "input" and set(payload) == {
            "control",
            "runId",
            "value",
        }:
            # A full queue must not block cancellation or disconnects.
            self.inputs.put_nowait(payload["value"])
        else:
            raise ValueError("unknown application control")

    async def handle(self, payload):
        if isinstance(payload, dict) and "control" in payload:
            await self.control(payload)
        else:
            self.start(payload)


async def websocket_session(websocket: WebSocket, agent: Agent) -> None:
    """Run one agent at a time while receiving bounded live input/cancel."""
    await websocket.accept()
    receive = asyncio.create_task(websocket.receive_json())
    session = AgentSession(websocket, agent)
    try:
        while True:
            waiting = {receive}
            if session.active is not None:
                waiting.add(session.active)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            session.reap(done)
            if receive in done:
                payload = receive.result()
                receive = asyncio.create_task(websocket.receive_json())
                await session.handle(payload)
    except WebSocketDisconnect:
        pass
    except (ValueError, ValidationError, asyncio.QueueFull):
        await websocket.close(1008, "Invalid input or full input queue")
    finally:
        await stop_task(receive)
        await stop_task(session.active)


def create_app(agent: Agent, *, origin: str | None = None) -> App:
    app = App(
        cors=CORS(
            origins=[origin],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )
        if origin is not None
        else None
    )
    app.health("/healthz")

    @app.post("/agent")
    async def run_agent(run: RunAgentInput):
        return StreamingResponse(
            sse_events(run, agent),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/agent/ws")
    async def agent_socket(websocket: WebSocket):
        await websocket_session(websocket, agent)

    return app
