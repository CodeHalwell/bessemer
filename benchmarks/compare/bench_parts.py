"""Decomposition bench: isolates the per-part cost of the matrix routes.

Same Item/Order models and routes as /home/cmux/matrix/common.py, plus
static (prebuilt) variants so a measurement difference isolates one part:
handler construction, response-model validation, native build/encode.
"""

import os

from bessemer import FastAPI
from pydantic import BaseModel


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str] = []


class Item0(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class Order(BaseModel):
    items: list[Item0]


def build(fw: str = "bessemer"):
    app = FastAPI()

    _static_models = [
        {"id": i, "name": f"n{i}", "price": 1.5, "tags": ["a"]} for i in range(10)
    ]
    _static_big = [
        {"id": i, "name": f"name-{i}", "value": i * 1.5, "flag": True}
        for i in range(100)
    ]
    _static_item = Item(id=1, name="x", price=2.5, tags=["a"])

    @app.get("/json")
    async def json_route():
        return {"ok": True, "count": 42}

    @app.get("/models", response_model=list[Item])
    async def models():
        return [
            {"id": i, "name": f"n{i}", "price": 1.5, "tags": ["a"]} for i in range(10)
        ]

    @app.get("/models_static", response_model=list[Item])
    async def models_static():
        return _static_models

    @app.get("/model_item", response_model=Item)
    async def model_item():
        return _static_item

    @app.post("/model_item_body", response_model=Item)
    async def model_item_body(item: Item) -> Item:
        return item

    @app.get("/big")
    async def big():
        return [
            {"id": i, "name": f"name-{i}", "value": i * 1.5, "flag": True}
            for i in range(100)
        ]

    @app.get("/big_static")
    async def big_static():
        return _static_big

    @app.post("/item")
    async def item(item: Item) -> Item:
        return item

    @app.post("/item_static")
    async def item_static(item: Item):
        return item

    @app.post("/echo")
    async def echo(order: Order) -> Order:
        return order

    @app.get("/optional_model", response_model=Order | None)
    async def optional_model():
        return {
            "items": [
                {"id": index, "name": f"item-{index}", "price": 1.5, "tags": ["a"]}
                for index in range(5)
            ]
        }

    @app.get("/optional_model_none", response_model=Order | None)
    async def optional_model_none():
        return None

    @app.get("/nested_lists", response_model=list[list[Item]])
    async def nested_lists():
        return [
            [
                {"id": index, "name": f"item-{index}", "price": 1.5, "tags": ["a"]}
                for index in range(4)
            ],
            [{"id": 9, "name": "tail", "price": 0.5, "tags": []}],
            [],
        ]

    @app.get("/model_dict", response_model=dict[str, Item])
    async def model_dict():
        # Key order deliberately not alphabetical.
        return {
            "zebra": {"id": 1, "name": "a", "price": 1.5, "tags": ["a"]},
            "alpha": {"id": 2, "name": "b", "price": 2.0},
            "middle": {"id": 3, "name": "c", "price": 0.5, "tags": []},
        }

    return app


app = build(os.environ.get("FW", "bessemer"))
