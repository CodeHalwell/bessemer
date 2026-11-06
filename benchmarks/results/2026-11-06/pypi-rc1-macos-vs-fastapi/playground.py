from typing import Annotated

from bessemer import App, Depends
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    quantity: int


app = App(title="Bessemer playground")
app.health("/healthz")


async def marker() -> str:
    return "dependency-ok"


@app.get("/items/{item_id}")
async def read_item(
    item_id: int, dependency: Annotated[str, Depends(marker)], active: bool = False
):
    return {"id": item_id, "active": active, "dependency": dependency}


@app.post("/items")
async def create_item(item: Item) -> Item:
    return item
