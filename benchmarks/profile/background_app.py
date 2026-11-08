"""Matched response shapes to attribute post-delivery background work costs."""

from bessemer import App, BackgroundTasks

app = App()


def sync_task():
    pass


async def async_task():
    pass


@app.get("/json")
async def json():
    return {"ok": True, "count": 42}


@app.get("/empty")
async def empty(tasks: BackgroundTasks):
    return {"ok": True, "count": 42}


@app.get("/async")
async def with_async(tasks: BackgroundTasks):
    tasks.add_task(async_task)
    return {"ok": True, "count": 42}


@app.get("/sync")
async def with_sync(tasks: BackgroundTasks):
    tasks.add_task(sync_task)
    return {"ok": True, "count": 42}
