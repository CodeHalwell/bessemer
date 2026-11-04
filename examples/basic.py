"""Run: bessemer run examples.basic:app --host 127.0.0.1 --port 8000."""

import asyncio
from contextlib import asynccontextmanager

from bessemer import App, HTTPException, Router


@asynccontextmanager
async def lifespan(app):
    app.state.started = True
    yield
    app.state.started = False


app = App(title="Bessemer example", lifespan=lifespan)
app.health("/healthz")
items = Router(prefix="/items")


@app.get("/")
async def index():
    return {"message": "Hello from Bessemer"}


@items.get("/{item_id}")
async def read_item(item_id: int, include_deleted: bool = False):
    if item_id < 1:
        raise HTTPException(404, "Item not found")
    await asyncio.sleep(0.001)
    return {"id": item_id, "include_deleted": include_deleted}


@items.post("/", status_code=201)
async def create_item(item: dict):
    return {"item": item}


app.include_router(items)
