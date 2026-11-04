# Bessemer: Technical Specification

Nov 2, 2026 · @Dan

## Summary

Bessemer is a Python web API framework whose request path is Rust from socket to socket. Its target is throughput and latency within a fixed margin of a native Axum service. Python runs only the handler body, and routes that need no user code never enter the interpreter. It keeps FastAPI-style ergonomics and Pydantic v2 compatibility, and scales across cores in one process on free-threaded CPython 3.14+.

### Problem

On a trivial FastAPI endpoint, most of the request time is interpreted Python: routing, dependency resolution, middleware and response construction. Only validation and serialisation (pydantic-core) are native. Rust servers such as Granian speed up the transport, but they hand the full ASGI protocol to Python, so the framework on top still pays the interpreter on every request.

### Performance model

The gap between a Python framework and native Rust is the Python executed per request. Bessemer classifies every route into one of three tiers at start-up and reports the tier of each route.

| Tier | Python executed per request | Typical routes | Target vs Axum on the same hardware |
| --- | --- | --- | --- |
| 0: Native | None | Static files, cache hits, health checks, redirects, auth and rate-limit rejections, Rust handlers via the extension API | ≥ 95% of throughput |
| 1: Handler only | The handler body; arguments arrive pre-built and the return value is serialised in Rust | JSON CRUD routes | ≥ 60% of throughput per core (stretch 75%); p99 within 1.5× at 50% load |
| 2: Compatibility | Python middleware, ASGI mounts, user code that reads the raw `Request` | Migrated FastAPI apps, Starlette middleware | Faster than FastAPI on Uvicorn; no native target |

These are design targets, not measurements. The M0 spike (see Roadmap) validates them, and its stop criterion is a Tier 1 result below 40% of Axum.

### Goals

| ID | Goal |
| --- | --- |
| G1 | Meet the Tier 0 and Tier 1 targets above, measured in CI against an Axum baseline |
| G2 | Zero interpreter entries for Tier 0 requests; exactly one for Tier 1 |
| G3 | Drive handler coroutines from Rust; use asyncio only when user code awaits a foreign awaitable |
| G4 | Linear multi-core scaling in one process on free-threaded CPython; built-in process supervisor on GIL builds |
| G5 | FastAPI-familiar API: decorators, type-hinted parameters, `Depends`, Pydantic v2 models |
| G6 | Native batteries: CORS, compression, static files, JWT and Entra ID auth, rate limiting, caching, OpenTelemetry |
| G7 | Optional native I/O clients (HTTP, Redis, Postgres) whose futures Tokio resolves without an event-loop hop |
| G8 | A Rust extension API for user-written native middleware and handlers |
| G9 | OpenAPI 3.1 generated once at start-up |
| G10 | Wheels for Linux, macOS and Windows on x86\_64 and arm64, for GIL and free-threaded CPython |

### Non-goals

- Import-level drop-in replacement for FastAPI. Migration is via a guide, a codemod and an ASGI mount for incremental moves.
- ORMs, migrations and templating. These come from the existing ecosystem.
- A durable job queue. Executors cover in-request offload only.
- ASGI as the primary interface. ASGI support exists as Tier 2.
- CPython older than 3.12, PyPy and GraalPy at launch.

## Background and prior art

No existing project combines a native request pipeline with a FastAPI-style, Pydantic-first API. Servers have moved to Rust; frameworks have not.

| Project | What it is | Native parts | What Bessemer takes | Gap Bessemer fills |
| --- | --- | --- | --- | --- |
| FastAPI + Starlette + Uvicorn | The reference Python API framework | Validation via pydantic-core (Rust); Uvicorn uses C extensions for the loop and HTTP parsing | API design, `Depends` semantics, OpenAPI conventions, 422 error shape | Routing, DI, middleware and response building are interpreted Python on every request |
| [Granian](https://github.com/emmett-framework/granian) | Rust HTTP server on Hyper and Tokio, serving ASGI, RSGI and WSGI apps | Server, HTTP/1 and HTTP/2, TLS, static files, worker management, Prometheus metrics | Per-worker backpressure, worker respawn on lifetime or memory limits, GIL-wait metric, threads-as-workers on free-threaded builds, a Rust task implementation for Python coroutines (\`--task-impl rust\`) | A server only; the framework above it stays Python |
| [Robyn](https://github.com/sparckles/Robyn) | Python web framework with a Rust runtime | Runtime, routing, multi-process and multi-worker scaling | Evidence that the model works; direct Rust integration | Its own request-object API rather than FastAPI-style typed parameters; Pydantic is optional |
| Litestar, BlackSheep | Performance-focused Python frameworks | Selected compiled components | DI and plugin design ideas | Still optimising within the interpreter |

Granian's documentation states that its free-threaded support is still experimental and discouraged in production. Its workers become threads sharing one interpreter, and it refuses to start if the GIL is re-enabled. Bessemer follows the same model and should expect the same maturity curve.

## Design principles

Python is the exception, not the platform: every request feature defaults to Rust, and Python runs only code the user wrote.

1. **Zero or one entry.** Tier 0 requests never acquire the interpreter. Tier 1 requests enter Python once, run dependencies and the handler, and leave with a return value that Rust serialises.
2. **Compile at start-up, not per request.** Signatures, type hints, dependency graphs and schemas are inspected once and frozen into execution plans. Nothing is introspected on the hot path.
3. **Drive coroutines from Rust.** Bessemer steps handler coroutines itself. A coroutine that finishes without suspending never touches an event loop. One that awaits a Bessemer-native future is resumed by Tokio directly. asyncio runs only for foreign awaitables.
4. **No object churn.** Arguments are built straight from parsed buffers. Return values (dicts, lists, Pydantic models, dataclasses) are serialised to bytes in Rust without an intermediate `model_dump`. Headers and the `Request` object are materialised only when asked for.
5. **Compatibility where it is cheap, opt-in where it is not.** Pydantic models and `Depends` work unchanged. Python middleware and ASGI work but drop the routes they touch to Tier 2, and start-up output says so.
6. **Fail fast at start-up.** Unsupported types, ambiguous parameters and dependency cycles are errors when the app is built, not on the first request.
7. **No hidden blocking.** Sync handlers run on a bounded thread pool. Debug mode reports event-loop stalls with the offending route.
8. **Measured claims only.** Every performance claim is a benchmark in CI, run against an Axum baseline on the same machine.

## Architecture overview

Bessemer is one Rust binary with a thin Python API layer. Each worker runs the whole request path in Rust and enters the interpreter only to run user code.

&#91;embedded content: worker architecture · Rust runtime, Python interpreter, shared services\]

A request moves down the Rust column. Only Tier 1 requests cross into Python, once, and the handler's result comes straight back to Rust for serialisation.

## Request lifecycle

Every exit that does not need user code comes before the interpreter, so rejected, cached and invalid requests cost no Python.

&#91;embedded content: request lifecycle · 8 stages, 3 native exits\]

A Tier 1 request enters Python once before the response is sent. Generator-dependency cleanup and background tasks run in a second entry after sending, off the latency path.

## Public Python API

The API mirrors FastAPI closely enough that most route modules port with import changes only. Native features (timeouts, caching, rate limits, auth) are declared on the route, not written as Python middleware.

```python
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

from asyncpg import Connection, create_pool
from pydantic import BaseModel, Field

from bessemer import App, BackgroundTasks, Depends, HTTPException, Path, Query, Router, State
from bessemer.auth import Principal, require_scope
from bessemer.cache import CachePolicy

DATABASE_URL = os.environ["DATABASE_URL"]


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(gt=0)


class ItemOut(ItemIn):
    id: int


@asynccontextmanager
async def lifespan(app: App) -> AsyncIterator[None]:
    app.state.pool = await create_pool(DATABASE_URL)  # once per worker
    yield
    await app.state.pool.close()


app = App(title="Inventory API", version="1.0.0", lifespan=lifespan)
items = Router(prefix="/items", tags=["items"])


async def get_db(state: State) -> AsyncIterator[Connection]:
    async with state.pool.acquire() as conn:
        yield conn  # cleanup runs after the response is sent


# Tier 1: Python runs only the function bodies.
@items.get(
    "/{item_id}",
    response_model=ItemOut,
    cache=CachePolicy(ttl=30, vary=["Accept-Language"]),  # cache hits are Tier 0
    timeout=2.0,
)
async def read_item(
    item_id: Annotated[int, Path(ge=1)],
    db: Annotated[Connection, Depends(get_db)],
    include_deleted: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    row = await db.fetchrow("SELECT ...", item_id, include_deleted)
    if row is None:
        raise HTTPException(404, "Item not found")
    return dict(row)  # validated and serialised in Rust against ItemOut; no model instance built


@items.post("/", status_code=201, rate_limit="60/minute")  # rejections are Tier 0
async def create_item(
    item: ItemIn,
    user: Annotated[Principal, Depends(require_scope("items.write"))],
    tasks: BackgroundTasks,
) -> ItemOut:
    ...


# Tier 0: these never enter Python.
app.static("/assets", directory="public")
app.health("/healthz")
app.include_router(items)
```

### API surface

| Area | Bessemer | Behaviour notes |
| --- | --- | --- |
| Application and routing | `App`, `Router`, `get`/`post`/`put`/`patch`/`delete`/`head`/`options`, `include_router`, `mount` | Routers carry prefix, tags, dependencies and native route options |
| Parameters | `Path`, `Query`, `Header`, `Cookie`, `Body`, `Form`, `File`, `UploadFile` | Scalars parse in Rust; multipart parses in Rust and spools large files to disk |
| Dependencies | `Depends`, `Security`, generator dependencies with `yield` | Per-request caching, router and app-level dependencies, cleanup after send |
| Responses | `JSONResponse`, `Response`, `PlainTextResponse`, `HTMLResponse`, `RedirectResponse`, `StreamingResponse`, `FileResponse`, `EventSourceResponse` | `FileResponse` is served from Rust; Python never touches the bytes |
| Errors | `HTTPException`, `RequestValidationError`, `@app.exception_handler` | 422 body matches FastAPI's shape; unhandled errors return 500 with the request ID |
| Lifespan | `lifespan=` async context manager | Runs once per worker process or worker thread |
| Background work | `BackgroundTasks` | Runs on the same event loop after the response is sent |
| WebSockets | `@app.websocket` with `receive_json`/`send_json`/`iter_text` | Framing, ping/pong and close handling in Rust |
| Request object | `Request` | Built lazily, only when a handler or dependency asks for it; reading the raw body or the ASGI scope drops the route to Tier 2 |
| Python middleware | `@app.middleware("http")` | Supported; every route it wraps drops to Tier 2 |
| Testing | `TestClient`, `AsyncTestClient` | In-process, no sockets; runs the real native pipeline |

## Route compilation and execution plans

Each route compiles at start-up into an immutable `RoutePlan` held in Rust. The hot path reads the plan and never introspects a Python object.

Compilation runs once in `app.build()`, which `run` calls implicitly:

1. Read the handler with `inspect.signature` and `typing.get_type_hints(include_extras=True)`.
2. Classify each parameter as path, query, header, cookie, body, form or file, dependency, or framework-injected (`State`, `BackgroundTasks`, `Request`, `Principal`).
3. Build a native extractor per parameter. Scalars (`int`, `float`, `bool`, `str`, `UUID`, `datetime`, `Enum`, `Literal`, constrained types) parse in Rust with no Python call. Models and complex types get a pydantic-core validator.
4. Resolve the dependency graph, de-duplicate shared dependencies, sort it topologically and reject cycles.
5. Build the response serialiser from `response_model` or the return annotation.
6. Assign the tier and record the reason, such as `Tier 2: wrapped by Python middleware 'audit'`.
7. Emit the route's OpenAPI fragment.

```rust
pub struct RoutePlan {
    pub method: Method,
    pub path: Arc<str>,
    pub tier: Tier,                        // Native | HandlerOnly | Compat
    pub native_stages: Box<[NativeStage]>, // rate limit, auth, cache lookup, CORS
    pub extractors: Box<[Extractor]>,      // one per handler argument, in call order
    pub deps: Box<[DepNode]>,              // topologically sorted
    pub handler: HandlerKind,              // AsyncFn | SyncFn | SyncInline | Native(Arc<dyn NativeHandler>)
    pub body: BodySpec,                    // size limit, content types, validator
    pub response: ResponseSpec,            // status, serialiser, headers, cache policy
    pub timeout: Option<Duration>,
}
```

### Hot-path rules

- Handler arguments are passed positionally with vectorcall, never as a keyword dict.
- Common header names, media types and small integers are pre-interned Python objects.
- Built-in dependencies (`State`, `Principal`, `require_scope`) resolve in Rust and arrive as ready-made objects.
- `Depends(..., scope="app")` runs once at start-up and is reused for every request.
- Generator dependencies need a second interpreter entry after the response is sent, for cleanup. It is off the latency path and counted separately in metrics.

## Validation and serialisation

Validation and serialisation run in pydantic-core (Rust), so Pydantic v2 models work unchanged and no Python executes unless the user wrote a custom validator.

| Concern | Mechanism | Notes |
| --- | --- | --- |
| JSON body | `SchemaValidator.validate_json` on the raw request buffer | No intermediate `dict`; one call builds the model |
| Path, query, header, cookie scalars | Native Rust parsers | Error `loc`, `type` and messages match Pydantic's |
| Forms and multipart | Native streaming parser; files over a threshold spool to disk | `UploadFile` wraps the spooled file |
| Returned model instance | `SchemaSerializer.to_json` straight into the response buffer | No `model_dump` round trip |
| Returned `dict` or `list` | A typed-dict schema derived from `response_model` at start-up, serialised with mismatches raised as errors | Checked and serialised in one call; no model instance built |
| Returned primitives, dataclasses, `datetime`, `UUID`, `Decimal`, `Enum` | Native serialiser | Used when no `response_model` is declared |
| Validation failure | 422 with FastAPI's error body shape | Override with an exception handler |
| `field_validator`, `model_validator` | Executed as written | Route stays Tier 1 but pays for the Python |

### Fast path: `bessemer.Struct`

Pydantic stays the default. For the hottest routes, `bessemer.Struct` is an optional slots-based model type validated and serialised by Bessemer's own schema engine. It avoids per-instance `__dict__` and Pydantic bookkeeping, in the manner of msgspec. Structs and Pydantic models can be mixed in one app, and both generate the same JSON Schema shape for OpenAPI.

```python
from bessemer import Struct

class Point(Struct, frozen=True):
    x: float
    y: float
```

## Async execution

Bessemer steps handler coroutines from Rust instead of scheduling them as asyncio tasks. This removes the per-request event-loop round trip that dominates trivial async handlers.

Each worker thread owns one Python thread, one asyncio-compatible event loop that is always running, and a share of the Tokio I/O runtime.

### How a handler runs

1. Rust calls the handler with its pre-built arguments, inside a copied `contextvars.Context`, and receives a coroutine.
2. Rust calls `coro.send(None)` immediately (eager start). A `StopIteration` means the handler finished without suspending, and its value goes straight to the serialiser.
3. If the coroutine yields a Bessemer-native future (native I/O clients, `bessemer.sleep`, `app.run_cpu`), Rust registers a Tokio waker and resumes the coroutine with `send(result)` when it completes. The event loop is not involved.
4. If it yields an asyncio future (asyncpg, httpx, redis-py), Rust attaches a done-callback and resumes the coroutine from the loop when the future resolves.
5. On completion, the return value is serialised in Rust and handed back to Tokio for sending.

### Event loop, in two stages

| Stage | Loop | Request hand-off | Trade-off |
| --- | --- | --- | --- |
| M1 | Stdlib asyncio or uvloop on the worker thread, running Bessemer's Rust task implementation | Requests queue in a lock-free channel; one eventfd wake-up drains a batch per loop iteration | Works with every asyncio library on day one; one wake-up per batch, not per request |
| M3 | `bessemer.loop`: an asyncio-compatible loop written in Rust and driven by Tokio | Direct, no cross-thread wake-up | Removes the hand-off entirely; must implement the loop APIs that common libraries use |

The M3 loop follows the precedent of rloop, an asyncio event loop written in Rust and offered as a Granian extra. It starts by covering the loop APIs that asyncpg, httpx, redis-py and aiohttp's client use. `--loop asyncio|uvloop|bessemer` selects the loop, with fallback when an app needs something unimplemented.

### Sync handlers

- Default: run on a bounded per-worker thread pool. On free-threaded builds they run in parallel; on GIL builds they contend for the GIL.
- `inline=True` on the route: run directly on the worker thread with no thread hop, for trivial CPU-light handlers. Debug mode warns when an inline handler exceeds 1 ms.

### Cancellation and backpressure

- Client disconnect or route timeout throws `CancelledError` into the coroutine; generator dependencies still run their cleanup.
- Each worker has an in-flight limit. At the limit the accept loop pauses. Beyond a queue limit, Rust answers 503 with `Retry-After` at Tier 0.

## Concurrency and process model

The Bessemer binary supervises its own workers, so `bessemer run app:app` is the whole production story with no Gunicorn.

| Build | Default topology | Notes |
| --- | --- | --- |
| Free-threaded CPython 3.14t+ | 1 process × N worker threads, N = CPU quota | One interpreter and one copy of the app; each worker thread owns its event loop; Tokio runtime shared |
| GIL CPython 3.12–3.15 | N processes × 1 worker thread | `SO_REUSEPORT` on Linux; shared listening socket elsewhere |
| Container scale-out, either build | 1 process × 1 worker | Supervisor only forwards signals; scale with replicas |

Hybrid layouts are allowed, for example `--workers 2 --threads 8`.

### Supervisor

- Binds sockets first, then starts workers by re-executing the binary rather than forking. Forking after Tokio or Python threads exist is unsafe, and Windows has no fork.
- Respawns crashed workers with backoff, and recycles workers on `max_requests`, `max_lifetime` or `max_rss`.
- SIGTERM: stop accepting, drain in-flight requests up to `graceful_timeout`, run lifespan shutdown, exit. SIGHUP: rolling worker reload.
- Workers send heartbeats. A worker whose event loop stalls past a threshold is recycled and the stall is logged with the route.
- Development mode watches files and restarts workers on change.

### Sizing

- Worker count defaults to the cgroup CPU quota (`cpu.max` on cgroup v2, `cpu.cfs_quota_us` on v1), not the host core count. This matters on AKS and Azure Container Apps, where `os.cpu_count()` reports the host.
- Memory per worker is reported at start-up, so process mode on GIL builds can be sized against the container limit.

### State and free-threading

- `app.state` is per worker (process or thread), and lifespan runs once per worker. Loop-bound resources such as asyncpg pools therefore stay on their own loop.
- `app.shared` is process-wide and must be thread-safe; Bessemer provides `bessemer.sync.Lock` and an atomic counter for it.
- If importing the app re-enables the GIL (a C extension without free-threading support), Bessemer falls back to process mode and names the module. `--require-free-threading` makes this an error instead. Detection uses `sys._is_gil_enabled()`.

## Built-in native pipeline

Every component below runs in Rust before or after the handler and is configured declaratively, so none of them changes a route's tier.

| Component | Configuration | Behaviour |
| --- | --- | --- |
| CORS | `App(cors=CORS(origins=[...]))` | Preflights answered at Tier 0 |
| Compression | `Compression(gzip=True, br=True, zstd=True, min_size=1024)` | Negotiated from `Accept-Encoding`; works with streaming responses |
| Static files | `app.static(path, directory=...)` | `sendfile`, ETag, range requests, precompressed `.br` and `.gz` siblings |
| JWT and JWKS | `JWTAuth(issuer, audience, jwks_url)` | Signature, `exp`, `nbf`, `aud` and `iss` checked in Rust; JWKS cached and refreshed; 401 and 403 at Tier 0 |
| Entra ID | `EntraIDAuth(tenant_id, audience)` | Preset over `JWTAuth` for v1 and v2 tokens, app roles and scopes |
| API keys | `APIKeyAuth(header, verifier)` | Hashed key store; constant-time comparison |
| Rate limiting | `rate_limit="60/minute"` on a route or router; keyed by IP, principal or header | Sliding window in memory; Redis backend for multiple replicas |
| Response cache | `cache=CachePolicy(ttl, vary, key)` | In-memory LRU or Redis; hits at Tier 0; conditional requests handled |
| Timeouts | `timeout=` per route, plus a global default | Cancels the handler and returns 504 |
| Body limits | `max_body_size`, global and per route | 413 before any parsing |
| Request IDs | Accept or generate `X-Request-ID` | Propagated to logs and traces |
| Proxy headers | `trusted_proxies=["10.0.0.0/8"]` | `Forwarded` and `X-Forwarded-*` applied only from trusted sources |
| Security headers | `SecurityHeaders()` preset | HSTS, `X-Content-Type-Options`, frame options; CSP passed through |

The order is fixed: request ID, proxy headers, body limit, CORS, IP-keyed rate limit, auth, principal-keyed rate limit, cache lookup, handler, cache store, compression, security headers, access log.

## Executors and CPU offload

CPU-bound work inside a handler goes to a pluggable executor. Request concurrency never goes through one: that is the supervisor's job.

```python
from typing import Callable, ParamSpec, Protocol, TypeVar

from bessemer import App
from bessemer.executors import ThreadPoolExecutor

P = ParamSpec("P")
R = TypeVar("R")


class Executor(Protocol):
    async def submit(self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R: ...
    async def shutdown(self, *, wait: bool = True) -> None: ...


app = App(executor=ThreadPoolExecutor(max_workers=8))


@app.post("/score")
async def score(batch: Batch) -> Scores:
    return await app.run_cpu(model_score, batch, timeout=5.0)
```

| Backend | Default when | Use for |
| --- | --- | --- |
| Native thread pool | Free-threaded build | CPU work with no pickling and shared memory |
| Process pool | GIL build | CPU work on one machine |
| Dask (`bessemer[dask]`) | Never | Dataframe and array work; existing Dask clusters |
| Ray (`bessemer[ray]`) | Never | Stateful actors holding loaded models |

- `app.run_cpu` returns a Bessemer-native future, so the handler resumes from Tokio without an event-loop hop.
- Each executor has a bounded queue. When it is full, the route returns 503 with `Retry-After` instead of queueing without limit.
- Timeouts are per call; cancellation propagates where the backend supports it.
- Work that outlives a request belongs in a queue such as Service Bus, Celery or Arq. Bessemer provides a `202 Accepted` with status-URL helper, but no durable queue.

## Native I/O and the extension API

Two escape hatches take more work out of Python: native clients for the hottest I/O, and Rust middleware and handlers that users write themselves.

### Native I/O clients

Awaiting a native client yields a Bessemer-native future, which Tokio resolves and resumes directly. Results still become Python objects, so the saving is the event-loop hop and the driver's Python-side protocol code, not the object conversion.

| Client | Built on | Python surface |
| --- | --- | --- |
| `bessemer.http.Client` | hyper with connection pooling | httpx-like `get`/`post`/`stream` |
| `bessemer.redis.Client` | redis-rs (async) | Commands, pipelines, pub/sub |
| `bessemer.pg.Pool` | tokio-postgres with a native pool | `fetch`, `fetchrow`, `execute`, transactions; rows as tuples, dicts or `Struct` |

These ship in M3 and are optional. asyncpg, httpx and redis-py keep working through the asyncio path.

### Rust extension API

The `bessemer-ext` crate exposes stable traits for middleware and handlers. Users build them into their own PyO3 module and register them from Python; they run at Tier 0.

```rust
use bessemer_ext::{async_trait, export, HeaderName, NativeMiddleware, Next, Request, Response};

pub struct TenantResolver {
    header: HeaderName,
}

#[async_trait]
impl NativeMiddleware for TenantResolver {
    async fn call(&self, mut req: Request, next: Next<'_>) -> Response {
        match req.headers().get(&self.header).and_then(|v| v.to_str().ok()) {
            Some(tenant) => {
                req.extensions_mut().insert(TenantId(tenant.to_owned()));
                next.run(req).await
            }
            None => Response::problem(400, "Missing tenant header"),
        }
    }
}

export!(TenantResolver);
```

```python
from bessemer import Native
from my_ext import PriceQuote, TenantId, TenantResolver  # the user's PyO3 crate

app.add_native_middleware(TenantResolver(header="X-Tenant-Id"))
app.add_native_route("GET", "/quote/{sku}", PriceQuote())


@app.get("/orders")
async def orders(tenant: Native[TenantId]) -> list[Order]:  # value set by the middleware
    ...
```

Rust has no stable ABI, so `export!` publishes a versioned `#[repr(C)]` table of function pointers in a `PyCapsule`. Bessemer checks the version at registration and refuses an incompatible extension with a clear error. `abi_stable` is the candidate for generating the table.

## Observability

All telemetry is produced in Rust, so it adds no interpreter time per request.

- **Traces:** an OpenTelemetry span per request, exported over OTLP, with W3C `traceparent` propagation. The active span context is exposed to Python through a contextvar, so spans created with `opentelemetry-api` inside handlers nest correctly.
- **Metrics:** Prometheus `/metrics` on a separate port, plus OTLP metrics. These cover request count and latency histograms by route template and status, in-flight requests, queue depth, event-loop lag, GIL wait on GIL builds, worker restarts, cache hit ratio, rate-limit rejections and requests per tier.
- **Logs:** a structured JSON access log written from Rust through a non-blocking writer. Python `logging` records go through the same sink with the request ID and trace ID attached; authorisation, cookie and API-key headers are redacted.
- **Azure:** Application Insights through the OpenTelemetry Collector's Azure Monitor exporter. A documented preset covers Container Apps and AKS.
- **Diagnostics:** `bessemer routes` lists every route with its tier and the reason for it. `--profile` samples requests and reports time per stage, split into native and Python.

## Configuration and CLI

Settings resolve in this order: CLI flag, then `BESSEMER_*` environment variable, then `[tool.bessemer]` in `pyproject.toml`, then the default.

| Command | Purpose |
| --- | --- |
| `bessemer run app:app` | Production server with supervisor |
| `bessemer dev app:app` | One worker, reload, debug errors, stall detection, docs UI on |
| `bessemer routes app:app` | Route table with tier and reason |
| `bessemer openapi app:app -o openapi.json` | Export the schema without serving; `--check` fails on drift |
| `bessemer migrate path/` | FastAPI codemod (see Compatibility) |

| Option | Default |
| --- | --- |
| `--host`, `--port`, `--uds` | `127.0.0.1`, `8000`, none |
| `--workers` | CPU quota on GIL builds; 1 on free-threaded builds |
| `--threads` | 1 on GIL builds; CPU quota on free-threaded builds |
| `--loop` | `auto`: stdlib or uvloop until M3, then `bessemer` |
| `--http` | `auto` (HTTP/1.1 and HTTP/2) |
| `--tls-cert`, `--tls-key`, `--tls-min` | none, none, `1.3` |
| `--max-requests`, `--max-lifetime`, `--max-rss` | Off |
| `--graceful-timeout` | 30 s |
| `--backpressure` | 1,024 in-flight requests per worker |
| `--access-log` | On in `dev`, off in `run` |
| `--require-free-threading` | Off |

## OpenAPI and docs UI

OpenAPI 3.1 is generated once from the route plans and served as pre-built bytes at Tier 0.

- Model schemas come from Pydantic's own JSON Schema generation, so output matches FastAPI for the same models. `Struct` emits the same shape.
- Security schemes are generated from configured auth: bearer JWT, OAuth2 with Entra ID authorisation and token URLs, and API keys.
- Swagger UI and ReDoc are served from bundled assets with no CDN, at `/docs` and `/redoc`. They are on in `dev` and off by default in `run`.
- `operation_id` is deterministic (`<router>_<function>`) and can be overridden per route.
- `bessemer openapi --check openapi.json` fails CI when the committed schema drifts from the code.

## Compatibility and migration from FastAPI

A typical FastAPI service should port in an afternoon, with most routes landing in Tier 1 unchanged.

| FastAPI feature | Bessemer | Tier impact |
| --- | --- | --- |
| `FastAPI`, `APIRouter` | `App`, `Router`; `bessemer.compat` re-exports both under FastAPI names | None |
| Typed parameters, `Depends`, `Security`, `yield` dependencies | Same semantics | None |
| `response_model`, `status_code`, `responses` | Same | None |
| `HTTPException`, `BackgroundTasks`, `jsonable_encoder` | Same | None |
| Lifespan; `on_event` | Lifespan supported; `on_event` as a deprecated shim | None |
| `Request` and `Response` objects | Lazy, API-compatible subset | Tier 2 only for raw body or scope access |
| `@app.middleware("http")` | Supported | Tier 2 for wrapped routes |
| Starlette middleware via `add_middleware` | Runs through the ASGI adapter | Tier 2 for wrapped routes |
| Mounted ASGI apps | `app.mount_asgi(path, asgi_app)` | Tier 2 for the mount |
| `TestClient` | `bessemer.testing.TestClient`, same interface | Not applicable |

### Migration paths

1. **Codemod:** `bessemer migrate` (built on libcst) rewrites imports, swaps known middleware for native equivalents and prints every construct that would land in Tier 2.
2. **Strangler:** mount the existing FastAPI app with `app.mount_asgi("/", legacy_app)` and move routes across one router at a time.

| Common add-on | Native replacement |
| --- | --- |
| `CORSMiddleware` | `cors=` |
| `GZipMiddleware` | `compression=` |
| slowapi | `rate_limit=` |
| fastapi-cache | `cache=` |
| Custom JWT dependency | `JWTAuth` or `EntraIDAuth` |
| `StaticFiles` mount | `app.static()` |

## Security requirements

A framework that parses untrusted input in native code has to be held to a native-code security bar.

| Area | Requirement |
| --- | --- |
| HTTP parsing | hyper only, with no custom parser. Limits on header count and size, request line length and header read time. HTTP/2 stream and flow-control limits on by default, including rapid-reset protection |
| Unsafe code | `unsafe` confined to the FFI crate, each block justified in a comment; `#![forbid(unsafe_code)]` everywhere else; Miri on the unsafe crate |
| Fuzzing | `cargo-fuzz` targets for the router, query, form, multipart, JWT and header parsers; continuous fuzzing via OSS-Fuzz once public |
| TLS | rustls; TLS 1.3 minimum by default, 1.2 opt-in; mTLS supported |
| Auth | Constant-time comparisons; JWT algorithm allow-list bound to each key; `alg: none` rejected; configurable clock skew |
| Errors | No tracebacks or internal detail in responses outside `dev` |
| Secrets | Never logged; `Authorization`, `Cookie` and API-key headers redacted in access logs |
| Supply chain | `cargo-deny` and `cargo-audit` for Rust, `pip-audit` for Python; PyPI Trusted Publishing with attestations; CycloneDX SBOM per release |
| Free-threading | Every `#[pyclass]` is `Sync`; ThreadSanitizer CI on free-threaded builds |
| Disclosure | `SECURITY.md` with private reporting; fixes for the latest minor release |

## Performance targets and benchmarking

The benchmark suite is the acceptance test. A release that regresses any tracked scenario by more than 5% does not ship.

Every scenario runs against five servers on the same hardware: an Axum baseline, Bessemer, FastAPI on Uvicorn, Starlette on Granian, and Robyn. The Axum baseline is built on `bessemer-core` with a Rust handler, so the difference isolates the cost of Python.

| ID | Scenario | Tier | Target vs Axum |
| --- | --- | --- | --- |
| B1 | Plaintext `GET /`, 13-byte body | 0 (static) and 1 (Python handler) | ≥ 95% and ≥ 70% of throughput |
| B2 | `GET /json` returning a small dict | 1 | ≥ 60% |
| B3 | Path and query parameters; 1 KB JSON from a Pydantic model | 1 | ≥ 55% |
| B4 | `POST` of a 2 KB JSON body into a nested model; 201 | 1 | ≥ 50% |
| B5 | JWT-authenticated `GET`, 20% invalid tokens | 0 and 1 | Rejections ≥ 95% |
| B6 | Cached route, 90% hit ratio | 0 and 1 | ≥ 90% |
| B7 | Handler awaiting 1 ms of simulated I/O: native sleep and `asyncio.sleep` | 1 | p99 within 1.5× at equal concurrency |
| B8 | Single-row Postgres fetch: asyncpg and the native client | 1 | Reported; database-bound |
| B9 | WebSocket echo, 10,000 connections | 1 | Memory per connection within 2× |
| B10 | Scaling from 1 to 16 cores: free-threaded threads and GIL processes | 1 | ≥ 0.8 scaling efficiency |

The M0 spike sets the initial Python-side cost budget per Tier 1 request for B2, and each later milestone must stay within it.

### Methodology

- The server and load generator run on separate machines of a fixed size, with the server pinned to dedicated cores and frequency scaling off.
- Saturation throughput is measured with `oha`. Latency (p50, p99, p99.9) is measured with `wrk2` at a constant rate of 25%, 50% and 75% of Axum's saturation rate, which corrects for coordinated omission.
- HTTP/1.1 runs with keep-alive and without pipelining, since pipelining flatters plaintext results. HTTP/2 runs separately.
- Each result is the median of five 60-second runs after a 30-second warm-up. A set of runs whose spread exceeds 3% is discarded and rerun.
- CPU time is split into kernel, Rust and Python using `perf` with CPython's perf trampoline (`-X perf`).
- Pull requests are gated on instruction counts from Callgrind-based microbenchmarks, which are deterministic. Nightly runs on fixed hardware gate releases, and results are published in the repository.

## Testing strategy

The FFI boundary is where bugs hide, so the suite weights reference counting, concurrency and cancellation as heavily as API behaviour.

| Layer | Tooling | Scope |
| --- | --- | --- |
| Rust unit | `cargo nextest` | Router, extractors, parsers, plan compiler, supervisor state machine |
| Python API | `pytest` with `bessemer.testing` clients | Public behaviour through the real native pipeline |
| FastAPI parity | A ported subset of FastAPI's own test suite, run against `bessemer.compat` | Parameter, dependency and error semantics; byte-level OpenAPI diff |
| Protocol conformance | h2spec for HTTP/2; Autobahn for WebSockets | Protocol correctness |
| Property and fuzz | `cargo-fuzz`; Hypothesis for Python-level properties | Parsers, validators, router |
| Concurrency | ThreadSanitizer on free-threaded builds; Loom for Rust synchronisation; stress tests with random cancellation | Races, deadlocks, cleanup on cancel |
| Memory | AddressSanitizer and Valgrind nightly; 24-hour soak with RSS tracking | Leaks and refcount errors across FFI |
| Performance | See Performance targets | Regression gate |
| Platforms | Linux x86\_64 and arm64, macOS arm64, Windows x86\_64; CPython 3.12–3.15, 3.14t and 3.15t | CI matrix |

## Packaging, build and release

Version-specific wheels are the primary artefacts because the full C API allows faster paths than the limited API. Stable-ABI wheels are published as a fallback for newer interpreters.

| Wheel | Interpreters | Role |
| --- | --- | --- |
| `cp312`, `cp313`, `cp314`, `cp315` | GIL builds | Primary; full C API fast paths |
| `cp314t`, `cp315t` | Free-threaded builds | Primary for thread mode |
| `abi3` (3.12+) and `abi3t` (3.15+) | Future CPython versions | Fallback until a version-specific wheel ships |

This extends the [PyO3 0.29 recommendation](https://github.com/PyO3/pyo3/releases/tag/v0.29.0), which proposes an `abi3` wheel for the minimum supported version, a `3.14t` wheel and an `abi3t` wheel for 3.15 and later. PyO3 0.29 has dropped 3.13t, so free-threaded support starts at 3.14t, the first release where CPython calls the build supported ([PEP 779](https://peps.python.org/pep-0779/)).

- **Targets:** manylinux\_2\_28 x86\_64 and aarch64, musllinux\_1\_2, macOS arm64 and x86\_64, and Windows x86\_64 and arm64.
- **Build:** PyO3 0.29+ and maturin, on a pinned Rust MSRV. Release builds use fat LTO, `codegen-units = 1` and profile-guided optimisation trained on the benchmark suite. Panics unwind so that PyO3 can turn them into Python exceptions.
- **Type stubs:** generated with `pyo3-introspection` and maturin's stub support, alongside a hand-written, `py.typed` Python layer.
- **Release:** GitHub Actions with `maturin-action`; PyPI Trusted Publishing with attestations; `bessemer-core` and `bessemer-ext` published to crates.io.
- **Extras:** `bessemer[dask]`, `bessemer[ray]`, `bessemer[otel]`. The native I/O clients need no Python dependencies.
- **Versioning:** SemVer, 0.x until the 1.0 API freeze; security fixes for the latest minor release; a CPython version is dropped when it reaches end of life.

## Repository layout and tooling

`bessemer-core` has no PyO3 dependency. That keeps the request path testable as pure Rust and makes it the Axum-comparable baseline in benchmarks.

```
bessemer/
├── Cargo.toml                 # workspace
├── pyproject.toml             # maturin build backend
├── crates/
│   ├── bessemer-core/         # server, router, route plans, native pipeline
│   ├── bessemer-py/           # PyO3 bindings, coroutine driver, event loop
│   ├── bessemer-supervisor/   # process and thread management
│   ├── bessemer-auth/         # JWT, JWKS, Entra ID, API keys
│   ├── bessemer-io/           # native HTTP, Redis and Postgres clients
│   ├── bessemer-ext/          # stable extension API (published)
│   └── bessemer-bench/        # Axum baseline and scenario servers
├── python/bessemer/           # Python API layer: decorators, params, compat, testing
│   └── py.typed
├── tests/
│   ├── rust/
│   ├── python/
│   ├── parity/                # ported FastAPI tests
│   └── conformance/
├── fuzz/
├── benchmarks/
└── docs/
```

| Concern | Tool |
| --- | --- |
| Python environment | uv |
| Python lint and types | ruff; mypy in strict mode |
| Rust lint and format | clippy (pedantic), rustfmt |
| Tasks | `just` |
| Hooks | pre-commit |
| Docs | MkDocs Material, with examples tested in CI |

## Roadmap and milestones

The riskiest assumption is tested first: M0 proves or disproves the performance thesis before any framework surface is built.

&#91;embedded content: roadmap · 5 phases, 4 gates\]

Phases are sequential and gated rather than dated, because durations depend on team size.

## Risks and open questions

The largest risk is that CPython's own call and allocation costs cap Tier 1 below target, which is why M0 can stop the project.

| Risk | Impact | Likelihood | Mitigation |
| --- | --- | --- | --- |
| CPython call and allocation overhead caps Tier 1 | Core positioning fails | Medium | M0 spike with a stop criterion; `Struct` fast path; free-threaded scaling as a second source of value |
| The Rust event loop is a large surface (transports, DNS, subprocesses, signals) | Native loop slips and async targets are missed | High | Ship M1 on stdlib or uvloop with batched wake-ups; implement only the APIs common libraries use; per-app fallback |
| C extensions re-enable the GIL on free-threaded builds | Thread mode unavailable for some apps | Medium | Automatic fallback to process mode; start-up report naming the module |
| pydantic-core behaviour changes between releases | Validation or serialisation breaks | Medium | Pinned compatible range; parity tests against each Pydantic release |
| FastAPI semantic edge cases | Migration friction | High | Ported parity suite; documented differences |
| Extension ABI breakage | User extensions fail after upgrades | Medium | Versioned C-ABI table; refusal with a clear error |
| Wheel matrix maintenance | Slow releases | Medium | Fully automated matrix; versions dropped at CPython end of life |
| "Bessemer" is also used by Bessemer Venture Partners | Confusion or a trademark query | Low | Check before any commercial use |

### Open questions

- [ ] Should `bessemer.Struct` be first-class from M1, or stay optional until Pydantic's limits are measured in M0?
- [ ] Should the native I/O clients live in core or in separate packages such as `bessemer-redis`?
- [ ] When should HTTP/3 (QUIC) arrive, and on which stack?
- [ ] Licence: MIT and Apache-2.0 dual licence, as is usual for Rust, or BSD-3-Clause?
- [ ] Should the GIL-build process mode share one Tokio accept loop through a master process, or rely on `SO_REUSEPORT` alone?

## Sources

- [Granian README](https://github.com/emmett-framework/granian/blob/master/README.md): worker, threading, backpressure, free-threading and task-implementation options
- [Robyn README](https://github.com/sparckles/Robyn/blob/main/README.md): features, process model and Python support
- [PEP 779: Criteria for supported status for free-threaded Python](https://peps.python.org/pep-0779/)
- [PyO3 0.29.0 release notes](https://github.com/PyO3/pyo3/releases/tag/v0.29.0): wheel recommendations, 3.13t removal, stub generation
- Name availability checked against the PyPI and crates.io registries on 2 November 2026
