"""Shared FastAPI-style routes; `build(fw)` returns an app for 'bessemer' or 'fastapi'."""

import asyncio
import os
from typing import Annotated

from pydantic import BaseModel


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str] = []


def build(fw: str, middleware: bool = False):
    if fw == "bessemer":
        from bessemer import (
            BackgroundTasks,
            Cookie,
            Depends,
            FastAPI,
            Form,
            Header,
            HTTPException,
            PlainTextResponse,
            Query,
            Request,
        )
    elif fw == "fastapi":
        from fastapi import (
            BackgroundTasks,
            Cookie,
            Depends,
            FastAPI,
            Form,
            Header,
            HTTPException,
            Query,
            Request,
        )
        from fastapi.responses import PlainTextResponse

    else:
        raise ValueError(f"unknown framework: {fw!r}")

    app = FastAPI()

    if middleware:

        @app.middleware("http")
        async def add_header(request, call_next):
            response = await call_next(request)
            response.headers["x-mw"] = "1"
            return response

    @app.get("/json")
    async def json_route():
        return {"ok": True, "count": 42}

    @app.get("/sync")
    def sync_route():
        return {"ok": True, "count": 42}

    async def get_db():
        return {"db": 1}

    async def get_user(
        db: Annotated[dict, Depends(get_db)], x_token: Annotated[str, Header()] = "t"
    ):
        return {"user": "u", "db": db["db"], "token": x_token}

    @app.get("/dep")
    async def dep(user: Annotated[dict, Depends(get_user)]):
        return user

    async def session():
        yield {"s": 1}

    @app.get("/yield_dep")
    async def yield_dep(s: Annotated[dict, Depends(session)]):
        return s

    @app.get("/header")
    async def header(user_agent: Annotated[str | None, Header()] = None):
        return {"ua": user_agent}

    @app.get("/cookie")
    async def cookie(session_id: Annotated[str | None, Cookie()] = None):
        return {"sid": session_id}

    @app.get("/query")
    async def query(
        a: int = 1,
        b: str = "x",
        c: float = 1.0,
        d: Annotated[int, Query(ge=0, le=100)] = 5,
        e: bool = False,
    ):
        return {"a": a, "b": b, "c": c, "d": d, "e": e}

    @app.get("/models", response_model=list[Item])
    async def models():
        return [
            {"id": i, "name": f"n{i}", "price": 1.5, "tags": ["a"]} for i in range(10)
        ]

    @app.get("/plain", response_class=PlainTextResponse)
    async def plain():
        return "hello world"

    @app.get("/request")
    async def request_route(request: Request):
        return {
            "path": request.url.path,
            "q": request.query_params.get("q"),
            "host": request.headers.get("host"),
        }

    @app.post("/form")
    async def form(name: Annotated[str, Form()], age: Annotated[int, Form()]):
        return {"name": name, "age": age}

    @app.get("/raise")
    async def raise_route():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/bg")
    async def bg(tasks: BackgroundTasks):
        tasks.add_task(lambda: None)
        return {"ok": True}

    @app.post("/dict")
    async def body_dict(body: dict):
        return body

    @app.post("/item")
    async def item(item: Item) -> Item:
        return item

    @app.get("/big")
    async def big():
        return [
            {"id": i, "name": f"name-{i}", "value": i * 1.5, "flag": True}
            for i in range(100)
        ]

    @app.get("/io")
    async def io():
        await asyncio.sleep(0.001)
        return {"ok": True}

    return app


app = build(os.environ.get("FW", "bessemer"), os.environ.get("MW") == "1")
