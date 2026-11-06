# AG-UI over SSE and WebSockets

Install the official optional SDK and start the deterministic example from the
repository root:

```sh
uv pip install '.[ag-ui]'
AG_UI_DEMO_ORIGIN=http://127.0.0.1:8000 \
  bessemer run examples.ag_ui.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Open [the local demo](http://127.0.0.1:8000/). Start a run and send input while its
text events are arriving, or cancel the run. The example needs no model or secrets.
The example allows that exact browser origin. To change the host or port, set
`AG_UI_DEMO_ORIGIN` to the matching HTTP(S) origin, without a trailing slash,
and open that same URL; for example, port 8772 needs
`AG_UI_DEMO_ORIGIN=http://127.0.0.1:8772` and `--port 8772`.

The adapter lives in `examples/ag_ui/adapter.py`; copy it into an application and
pass an async event-generator function to `create_app`. Its arguments are the SDK's
validated `RunAgentInput` and either a bounded live-input queue (WebSocket) or
`None` (HTTP). Yield SDK `BaseEvent` subclasses. Every emitted event is validated
against the SDK's discriminated event union before serialization. Application
agents own event ordering, run identifiers and their model/tool cleanup.

## Standard HTTP SSE endpoint

`POST /agent` accepts `RunAgentInput` and returns SDK `EventEncoder` SSE frames.
This is the HTTP shape consumed by AG-UI's `HttpAgent`. Invalid inputs receive
422 before streaming starts. Disconnects close the agent iterator and await its
cleanup through Bessemer's streaming response lifecycle.

```sh
curl -N http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"threadId":"demo","runId":"run-1","messages":[],"tools":[],"context":[],"state":{}}'
```

Use `StreamingResponse` around `EventEncoder` output: those strings already
contain `data:` and event delimiters, so wrapping them as event data would encode
them a second time. This example implements SSE, not AG-UI's binary HTTP encoding.

## Duplex WebSocket application framing

`/agent/ws` accepts a JSON `RunAgentInput` object to start a run and sends one
SDK event JSON object per text frame, with camelCase aliases. A receive task runs
concurrently with event emission, including while sending is backpressured. The
same connection can start another run after the previous run's terminal event.

The following inbound control messages are **this example's application
extensions**, not standard AG-UI cancellation or input wire formats:

```json
{"control":"input","runId":"run-1","value":"More user input"}
{"control":"cancel","runId":"run-1"}
```

Controls must identify the active run. Input has one pending queue slot; a full
queue, an invalid control or overlapping run closes the session with code 1008.
An explicit cancel waits for the agent iterator's cleanup, then sends a standard
`RUN_ERROR` event using the application-specific code `CLIENT_CANCELLED`. An agent
failure sends `RUN_ERROR` with `AGENT_ERROR` and a generic message. Disconnects
cancel the run and await cleanup without sending a terminal event to a departed
client. There is no extra event queue: each `send_text` awaits Bessemer's native
bounded transport backpressure before requesting more events.

The demo waits for one live input after its opening text. A real agent can consume
the queue while emitting tokens or awaiting tool work; retain its resources in a
`try/finally` so cancellation closes them. Run IDs are caller-supplied data; add
application authentication and per-user authorization before exposing agent work.

AG-UI's [architecture](https://docs.ag-ui.com/concepts/architecture) describes
transport-independent events and the standard HTTP client. The official
[Python SDK](https://github.com/ag-ui-protocol/ag-ui/tree/main/sdks/python)
supplies the models and SSE encoder used here. The WebSocket control framing above
requires a matching application client; it is not built into `HttpAgent`.
