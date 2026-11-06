"""Payloads and response checks shared by the broad API comparison."""

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request


@dataclass(frozen=True)
class Workload:
    name: str
    path: str
    expected: object
    payload: bytes | None = None
    status: int = 200

    def validate(self, opener, base):
        request = Request(base + self.path, data=self.payload)
        if self.payload is not None:
            request.add_header("content-type", "application/json")
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            actual = body if isinstance(self.expected, bytes) else json.loads(body)
            if response.status != self.status:
                raise ValueError(f"{self.name}: HTTP {response.status}")
            if self.status == 200 and actual != self.expected:
                raise ValueError(f"{self.name}: unexpected response body")


ORDER = {
    "items": [
        {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
        for i in range(20)
    ]
}
COERCED = {"items": [{**item, "id": str(item["id"])} for item in ORDER["items"]]}
EVENTS = "".join(
    f'data: {{"type":"TEXT_MESSAGE_CONTENT","delta":"{index}"}}\n\n'
    for index in range(16)
).encode()
WORKLOADS = (
    Workload("json", "/json", {"ok": True, "count": 42}),
    Workload("echo", "/echo", ORDER, json.dumps(ORDER).encode()),
    Workload("coerced", "/echo", ORDER, json.dumps(COERCED).encode()),
    Workload("invalid", "/echo", {}, b'{"items":[{"id":"wrong"}]}', 422),
    Workload("bytes", "/bytes", b"x" * 262144),
    Workload("io", "/io", {"ok": True}),
    Workload("fanout", "/fanout", {"completed": 16}),
    Workload("dependencies", "/dependencies", {"value": 42}),
    Workload("sync", "/sync", {"ok": True}),
    Workload("stream", "/stream", b"x" * 16384),
    Workload("sync-stream", "/sync-stream", b"x" * 16384),
    Workload("events", "/events", EVENTS),
)
