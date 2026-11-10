"""Equivalent API workloads for installed Bessemer and FastAPI wheels."""

import asyncio
import os
import time

from pydantic import BaseModel

if os.environ.get("BENCHMARK_FRAMEWORK") == "fastapi":
    from fastapi import Depends, FastAPI, WebSocket
    from fastapi.responses import Response, StreamingResponse

    app = FastAPI()
else:
    from bessemer import App, Depends, Response, StreamingResponse, WebSocket

    app = App()
    app.health("/native-healthz")


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class Order(BaseModel):
    items: list[Item]


PAYLOAD = b"x" * 262144


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/json")
async def small_json():
    return {"ok": True, "count": 42}


@app.post("/echo")
async def echo(order: Order) -> Order:
    return order


@app.get("/bytes")
async def large_bytes():
    return Response(PAYLOAD, media_type="application/octet-stream")


@app.get("/io")
async def awaited_io():
    await asyncio.sleep(0.001)
    return {"ok": True}


@app.get("/fanout")
async def fanout():
    await asyncio.gather(*(asyncio.sleep(0.001) for _ in range(16)))
    return {"completed": 16}


async def resource():
    await asyncio.sleep(0)
    try:
        yield 42
    finally:
        await asyncio.sleep(0)


@app.get("/dependencies")
async def dependency(value: int = Depends(resource)):
    return {"value": value}


@app.get("/sync")
def blocking_io():
    time.sleep(0.001)
    return {"ok": True}


@app.get("/stream")
async def stream():
    async def chunks():
        for _ in range(16):
            yield b"x" * 1024
            await asyncio.sleep(0)

    return StreamingResponse(chunks(), media_type="application/octet-stream")


@app.get("/sync-stream")
async def sync_stream():
    return StreamingResponse(
        (b"x" * 1024 for _ in range(16)), media_type="application/octet-stream"
    )


@app.get("/events")
async def events():
    async def chunks():
        for index in range(16):
            yield f'data: {{"type":"TEXT_MESSAGE_CONTENT","delta":"{index}"}}\n\n'
            await asyncio.sleep(0)

    return StreamingResponse(chunks(), media_type="text/event-stream")


@app.websocket("/ws")
async def websocket(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            value = await socket.receive_text()
            await socket.send_text(value)
    except Exception as error:
        if type(error).__name__ != "WebSocketDisconnect":
            raise
