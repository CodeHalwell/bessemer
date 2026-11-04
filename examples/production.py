"""A bounded, authenticated read-only service example.

Install bessemer[pydantic], supply BESSEMER_EXAMPLE_API_KEY, then run:
    bessemer run examples.production:app --workers 2

The catalog is illustrative read-only data. Replace it with your actual backend.
Public API documentation is disabled unless BESSEMER_EXAMPLE_DOCS=1.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from bessemer import (
    App,
    Depends,
    HTTPException,
    Path,
    PlainTextResponse,
    Query,
    Router,
    State,
)
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Catalog:
    """Replace with an application-owned database or HTTP client."""

    def find(self, identifier: int) -> dict | None:
        if identifier == 1:
            return {"id": 1, "name": "Example item", "price_cents": 1250}
        return None


class Item(BaseModel):
    id: int
    name: str
    price_cents: int | None = None


class QuoteRequest(BaseModel):
    item_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=100)


class Quote(BaseModel):
    item_id: int
    quantity: int
    total_cents: int


@asynccontextmanager
async def lifespan(app):
    app.state.catalog_ready = False
    yield


async def catalog(state: State):
    # Acquire a real async client here. This generator runs once per worker,
    # after lifespan startup, and closes before lifespan shutdown.
    client = Catalog()
    state.catalog_ready = True
    try:
        yield client
    finally:
        state.catalog_ready = False
        # Await your client's close method here.


public_docs = os.environ.get("BESSEMER_EXAMPLE_DOCS") == "1"
public_paths = ["/healthz", "/readyz"]
if public_docs:
    public_paths += ["/openapi.json", "/docs"]

app = App(
    title="Bessemer service example",
    version="1.0.0",
    lifespan=lifespan,
    openapi_url="/openapi.json" if public_docs else None,
    docs_url="/docs" if public_docs else None,
    allowed_hosts=["localhost", "127.0.0.1", "api.example.com"],
    api_key={"env": "BESSEMER_EXAMPLE_API_KEY", "exclude_paths": public_paths},
    rate_limit={
        "requests": 120,
        "window_seconds": 60,
        "max_keys": 10_000,
        "exclude_paths": ["/healthz", "/readyz"],
    },
    access_log=True,
)
app.health("/healthz")
items = Router(prefix="/items", tags=["items"])


@app.get("/readyz", include_in_schema=False)
async def ready(state: State) -> PlainTextResponse:
    if not state.catalog_ready:
        raise HTTPException(503, "Service is not ready")
    return PlainTextResponse("ready")


@items.get("/{item_id}", response_model=Item)
async def read_item(
    item_id: Annotated[int, Path(ge=1)],
    client: Annotated[Catalog, Depends(catalog, scope="app")],
    include_price: Annotated[bool, Query()] = False,
):
    item = client.find(item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return {
        "id": item["id"],
        "name": item["name"],
        "price_cents": item["price_cents"] if include_price else None,
    }


@items.post("/quote", response_model=Quote)
async def quote_item(
    value: QuoteRequest,
    client: Annotated[Catalog, Depends(catalog, scope="app")],
):
    item = client.find(value.item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return {
        "item_id": item["id"],
        "quantity": value.quantity,
        "total_cents": item["price_cents"] * value.quantity,
    }


app.include_router(items)
