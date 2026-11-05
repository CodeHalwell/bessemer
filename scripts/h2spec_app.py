"""A minimal application for h2spec HTTP/2 conformance runs.

Usage: python scripts/h2spec_app.py PORT [CERT KEY]
"""

import sys

from bessemer import App, Request

app = App()


@app.get("/")
async def index():
    return {"ok": True}


@app.post("/")
async def receive(request: Request):
    return {"length": len(await request.body())}


if __name__ == "__main__":
    port = int(sys.argv[1])
    tls = {}
    if len(sys.argv) == 4:
        tls = {"tls_cert": sys.argv[2], "tls_key": sys.argv[3]}
    app.run(host="127.0.0.1", port=port, **tls)
