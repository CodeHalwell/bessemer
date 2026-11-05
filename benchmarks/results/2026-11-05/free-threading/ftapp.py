from bessemer import App

app = App()


@app.get("/cpu")
def cpu():
    total = 0
    for value in range(20000):
        total += value * value
    return {"total": total}


@app.get("/json")
async def json_handler():
    return {"ok": True, "count": 42}
