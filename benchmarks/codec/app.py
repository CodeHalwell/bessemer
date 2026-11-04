"""Endpoints for the codec end-to-end benchmark (benchmarks/codec/http_bench.py)."""

from typing import Annotated

from bessemer import App, Body

app = App()

SMALL = {"ok": True, "count": 42}
ITEMS = [
    {"id": i, "name": f"item-{i}", "score": i * 1.5, "active": i % 2 == 0}
    for i in range(100)
]


@app.get("/json")
async def small():
    return {"ok": True, "count": 42}


@app.get("/items")
async def items():
    return ITEMS


@app.post("/echo")
async def echo(payload: Annotated[list, Body()]):
    return payload
