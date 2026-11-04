# Production readiness — tested checkpoints

Bessemer now has a bounded application runtime and process supervisor. It remains
an alpha: the original specification is broader than this implementation, and
formal performance acceptance and release qualification are still open.

The six-hour engineering push started on 4 November 2026 at 09:48 UTC. This
record tracks tested checkpoints rather than treating newly written code as
production proof. The specification and decisions live in Git so continuation
does not depend on a conversation remaining available.

## Implemented core

- Installable Rust/Python wheel, validated configuration and supervised CLI.
- App/Router registration, startup route/signature compilation, typed path/query,
  header/cookie/body inputs and constraints, dependency DAGs with request caching,
  app-scoped resources, sync/async/yield dependencies and lifespan.
- Explicit JSON/text/HTML/redirect responses, repeated cookies, safe headers,
  optional Pydantic input and response validation, filtered serialization,
  exception handlers, Request/State/context and request-scoped background work.
- OpenAPI 3.1 generated once, native schema delivery and optional bundled docs.
- Native routing/JSON, bounded body/response copying, admission limits,
  request deadlines, cancellation and native health/static replies.
- Explicit HTTP/1 connection/header-read/count/buffer/target bounds, strict Host
  and framing checks, exact JSON integers, and canonical cookie parsing.
- Re-exec workers sharing a parent-owned listener, readiness and loop heartbeats,
  cgroup CPU sizing, bounded crash backoff, rolling reload, request/lifetime/RSS
  recycling, and TERM/drain/KILL shutdown with Linux process-group cleanup.
- Native CORS, env-configured API keys, allowed hosts, trusted proxy client IPs,
  bounded rate limits, request IDs, security headers, protected Prometheus
  metrics and bounded redacted access logging.
- Installed-wheel HTTP tests, actual process failure tests, reproducible Linux
  subreaper tests, CI wheel artifacts, container packaging and qualification tools.

Python application routes remain Tier 2. Native health, static documentation and
native control rejections do not execute a Python handler. The specified Tier 1
batching/native coroutine path and its performance targets are not qualified.

## Verified checkpoints

At 10:45 UTC on CPython 3.12, the rebuilt installed wheel passed **248 tests**:
23 Rust workspace tests, 216 Python tests and 9 original benchmark-harness tests.
The Python set includes actual HTTP framing/limits, dependency and ContextVar
cleanup, public API/example behavior, native controls, real worker crashes,
stalls, recycling, reload and hard shutdown. Rust formatting, warnings-denied
Clippy, Ruff formatting/lint and shell syntax checks passed.

The qualified Pydantic minimum is 2.10.6: 97 source compatibility tests passed
with that version and its pydantic-core 2.27.2. The same source suite passed with
current Pydantic; without Pydantic, 87 passed and 10 optional-feature tests
appropriately skipped. CI explicitly installs the minimum on Python 3.12 and
current supported v2 on Python 3.13 so those tests cannot silently disappear.

Pinned dependency scanners found no known advisories in the then-current
Cargo.lock, qualified minimum/current optional Python runtime closures, and
maturin 1.9.6. Scanner versions, database revision and exact dependency snapshots
are retained with the security reports. This does not replace an application
security review or guarantee absence of unknown vulnerabilities.

Independent generated-input testing and long-running qualification continue.
See [qualification evidence](qualification-results.md) for exact artifacts,
failures, workload provenance, older-wheel versus candidate distinctions and
completed runs. Property testing found a floating-point response-budget boundary issue; its
shrunk input is retained and the correction passed both boundary regressions
and the repeated generated-input campaign.

At 11:25 UTC, the refreshed 0.2.0a1 installed wheel passed **262 tests**:
25 Rust workspace tests, 228 Python tests and 9 original harness tests, with
formatting, Clippy, Ruff and shell checks passing. This includes the subsequent
JSON duplicate-value, MIME/transfer-coding, Latin-1 header, admission-publication
and multiprocess log atomicity corrections. The separate seeded campaign
passed 21,000 generated cases on its recorded immutable wheel; artifact hashes
and regression inputs distinguish that campaign from later revisions.

At 11:55 UTC, the rebuilt 0.2.0a2 installed wheel passed **270 tests**:
25 Rust workspace tests, 236 Python tests and 9 original harness tests. Remote
CI passed for commit `ac8b397` on Python 3.12 with Pydantic 2.10.6 and Python
3.13 with Pydantic 2.14.2. This checkpoint separates startup and shutdown
budgets and preserves the heartbeat reader during normal application cleanup.
Its immutable container images passed the restricted runtime scenarios at
512 MiB and 256 MiB and the authenticated production example checks. See
[container evidence](deployment.md#qualification) for the exact
image identities and limitations; this does not qualify subsequent source.

The initial OS scan reported installed Debian package advisories, including
SQLite with FTS5. Component-level review found several other flagged components
absent, but that does not make the image advisory-free. Python distribution and
Cargo advisory results have separate scopes from OS packages and the CPython
interpreter. Base-image remediation and exact final-image qualification remain
open.

Exploratory same-host framework measurements show a material performance cost:
the a2 one-worker JSON median was 27,508 requests/second against the foundation's
55,392, while the two-worker median was 51,291 with a 28.75% sample spread.
Fixed-rate latency also deteriorated near saturation. These measurements do not
establish accepted scaling, a deployment SLO or formal M0 performance acceptance.

## Operational contracts

The CLI process supervisor is POSIX. Linux has the qualified parent-death and
zombie-safe process-group cleanup path. Each worker owns one asyncio loop and a
bounded sync pool. Limits apply per worker unless explicitly documented. A
worker replacement overlaps at most one old draining worker in its slot.
Deliberately detached descendants remain the container/cgroup's responsibility.

A request capacity slot remains held through actual sync work and cancellation
cleanup. The supervisor can terminate a non-cooperative process after its drain
deadline. Generator dependency cleanup and BackgroundTasks currently complete
before the response is sent; this favors bounded resource lifetime over the
specification's future post-send optimization. Native pre-handler errors do not
currently invoke Python custom exception handlers.

JSON integers preserve precision up to 4096 digits; floats must be finite. JSON
nesting is capped at 64 levels. Route literals must be ASCII URI text, with
explicit percent encoding for non-ASCII literals; parameter values support
Unicode. Cookie parsing uses strict RFC 6265 octets and rejects malformed or
duplicate names. Error bodies do not echo validation inputs or arbitrary
validator messages. Explicit HTTPException details remain application output.

Default application request/response bounds are 1 MiB/4 MiB. Normalization and
native encoding are bounded; application code and external model serialization
can allocate before the framework sees a result. Application response headers
are capped at 64 fields and 16 KiB. The default request transport uses at most
1024 connections, 64 headers, 32 KiB header buffer, a 10-second header deadline
and an 8192-byte request target. See configuration docs for validated ranges.

API keys and metrics tokens are read from named environment variables; no key
values are exported in route/schema configuration. Host allowlists, proxy trust
and health exemptions are explicit. Rate limits are process-local. Metrics
require one worker until cross-process aggregation exists, use fixed/bounded
labels and a 4 MiB scrape cap, report omitted series, and admit only one scrape
body at a time. Access logs are best-effort through a bounded queue and never
include raw URLs, query strings, request bodies or authorization headers.

Use ingress for TLS, deployment access controls and infrastructure policy.
Health reports process reachability; application readiness must check its own
resources. The included production example demonstrates that separation.

## Remaining release and specification gates

1. **Formal performance acceptance.** Dedicated server/load machines, pinned
   server cores, five 60-second samples after 30-second warmups, <=3% spread,
   fixed-rate latency and representative workload comparisons. The local M0
   run exceeded the spread ceiling and was load-generator constrained. It
   establishes neither the 60% target nor the 40% stop threshold result.
2. **Candidate qualification.** Finish immutable candidate soak/failure/fuzz
   runs, compare resource use and shutdown under load, validate the final Docker
   image and complete clean remote CI for the exact source revision.
3. **Fast path and scaling.** Complete the Rust-driven/batched Tier 1 execution
   path, free-threaded behavior and multicore performance evidence.
4. **Broader framework surface.** Native JWT/JWKS/Entra ID, distributed rate/cache,
   compression/static files, streaming, WebSockets, native I/O clients, extension
   ABI, FastAPI migration/parity and OpenTelemetry remain incomplete.
5. **Broader release assurance.** Protocol suites, FFI sanitizer/concurrency work,
   sustained deployment/failure evidence and Windows/macOS/arm64/free-threaded
   wheel qualification remain open. No stateful production service is deployed.

## Continue from repository evidence

Start with this record, [the application API](application-api.md),
[supervision](supervision.md), [native controls](production-controls.md), Git
history and [the original specification](technical-specification.md). The
specification's embedded architecture/lifecycle/roadmap diagrams were not
recoverable from its plain-text export. Work recalled from 3 November was not
recovered; this is a reconstructed implementation with new evidence.
