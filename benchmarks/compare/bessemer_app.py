"""Bessemer side of the head-to-head. Routes match fastapi_app.py and axum_app."""

import asyncio

from bessemer import App
from pydantic import BaseModel


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class Order(BaseModel):
    items: list[Item]


app = App()
app.health("/healthz")


@app.get("/json")
async def json_route():
    return {"ok": True, "count": 42}


@app.get("/items/{item_id}")
async def item(item_id: int, q: str = "x"):
    return {"id": item_id, "q": q}


@app.post("/echo")
async def echo(order: Order) -> Order:
    return order


@app.get("/io")
async def io():
    await asyncio.sleep(0.001)
    return {"ok": True}
