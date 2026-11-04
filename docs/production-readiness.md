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
- Per-worker retained request/response payload budgets, separately configurable
  from individual body/response limits and held through cleanup/transport ownership.
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
interpreter. The runtime now uses pinned Debian Trixie after isolated base-change
tests. Exact final-image scans report zero critical matches and 45 high package
findings representing nine unique CVEs, including an unresolved SQLite FTS5
denial-of-service advisory. Component triage and remaining exposure are retained
with the image records; these results do not establish an advisory-free image.

Exploratory same-host framework measurements show a material performance cost:
the a2 one-worker JSON median was 27,508 requests/second against the foundation's
55,392, while the two-worker median was 51,291 with a 28.75% sample spread.
Fixed-rate latency also deteriorated near saturation. These measurements do not
establish accepted scaling, a deployment SLO or formal M0 performance acceptance.

At 12:32 UTC, the rebuilt 0.2.0a3 installed wheel passed **302 tests**:
37 Rust workspace tests, 256 Python tests and 9 original harness tests, plus
formatting, warnings-denied Clippy, Ruff and shell checks. Every installed Python
package file matched the source byte for byte. Its wheel SHA-256 is
`5737cc939d90152e5316a387a21a9b84d8af650d7d1370361ff3cb6fc6cd5790`.
This candidate adds aggregate payload budgets, strict absolute processing
deadlines, cancellation of queued sync calls before user code starts, and
parent-enforced self-drain deadlines even with heartbeat monitoring disabled.
It avoids scheduling a cleanup task for an empty internal exit stack; nonempty
resource cleanup retains its existing cancellation shielding and context.

The same immutable a3 wheel passed 21,000 generated cases across seven seeded
HTTP properties (19,341 distinct inputs) in an isolated dependency-free core
environment. Optional Pydantic behavior is covered separately by the installed
HTTP/API suite and CI. Its exact 66-dependency Cargo lock, optional Python runtime
closures and separate build-tool closure had no known advisories in the recorded
scanner databases. The same wheel completed its two-hour, two-worker extended soak at 14:34 UTC:
720,003 expected responses, zero unexpected HTTP/log errors, verified timeout
cleanup and both normal lifespan finalizers. Server and qualifier exited zero;
shutdown took 0.766 seconds without forced termination. Exact results are
retained in the qualification record; this does not replace the 24-hour gate.

Remote CI for commit `342e0ac` passed **309 tests per Python version**:
37 Rust, 263 Python and 9 original harness tests. The seven additional Python
tests validate full-stream log redaction/error guards and delivered-work checks
in the production-image soak harness. Tested combinations were CPython 3.12.15
with Pydantic 2.10.6 and CPython 3.13.17 with Pydantic 2.14.2. The separate
container workflow passed both builds, all 512/256 MiB runtime scenarios and
the authenticated production example checks. It retained the tested image
binaries and qualification records as downloadable CI artifacts.

The recorded no-init a3 local and remote image build inputs match source digest
`6e1c4f077e4f75238484c1fb0ec2192cb3ca67c0b5904819ec8108029a4f4742`.
Exact IDs, inventories, audit scopes and CI artifacts are linked from
[deployment](deployment.md) and [CI evidence](../deploy/qualification/ci/342e0ac.json).
Those images are historical: a later process test reproduced adopted
grandchildren remaining as zombies when Bessemer itself was container PID 1.
The replacement image uses pinned, archive-verified Tini above Bessemer; its
source digest is `8fa1e5d46b617d851220c61bb4cc001e49cc55f159694f0d972284115dfee80d`.
Its native extension bytes match the preceding a3 image, with Tini the only
installed package addition. Init-aware lifecycle/orphan tests and fresh
exact-image audits are recorded separately; the no-init image's two-hour
demonstration does not qualify its replacement's timed run.
The init-enabled checkpoint [`f4f7eae`](../deploy/qualification/ci/f4f7eae.json)
is green in both remote CI workflows:
**315 tests per Python version** (37 Rust, 269 Python and 9 original harness
tests), plus both image builds and all five runtime scenarios at each of
512/256 MiB. The new orphan test requires complete child absence after a worker
crash while init and the supervisor remain alive; zombie state cannot pass.
The replacement image's planned 90-minute run was stopped early at the user's
request to limit usage. Partial observations are retained separately; the full
90-minute image soak is incomplete and is not an acceptance result.
The final wheel's one-core JSON median was 35,583 requests/second with 2.86%
spread; its single corrected-latency point at 13,847 requests/second had p99
2.605 ms. These short diagnostics leave formal performance acceptance open.

A first framework-overhead pass removed three per-request costs. Native Tokio
threads now keep one CPython thread state instead of recreating it for each
request. Results made only of exact built-in JSON values are encoded natively
once. Anything else still goes through `jsonable`, with its existing behaviour
and errors. Routes skip resolver, validation and request-view setup they do not
need. A same-host interleaved A/B on a different, slower machine put one-core
`/json` at 18.3k/s for main, 26.4k/s for the foundation and 29.2k/s for this
change. Awaited I/O rose 20% but stayed about 7% below the foundation.
This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/framework-overhead-pass1/`.

## Operational contracts

The CLI process supervisor is POSIX. Linux has the qualified parent-death and
zombie-safe process-group cleanup path. Each worker owns one asyncio loop and a
bounded sync pool. Limits apply per worker unless explicitly documented. A
worker replacement overlaps at most one old draining worker in its slot.
Deliberately detached descendants remain the container/cgroup's responsibility.
The supplied container runs Tini as PID 1 to forward signals and reap adopted
descendants. Bessemer supervises its tracked worker leaders. A custom image that
runs the CLI as PID 1 needs an init/subreaper for application grandchildren.

A request capacity slot remains held through actual sync work and cancellation
cleanup. The supervisor can terminate a non-cooperative process after its drain
deadline. Generator dependency cleanup and BackgroundTasks currently complete
before the response is sent; this favors bounded resource lifetime over the
specification's future post-send optimization. Native pre-handler errors do not
currently invoke Python custom exception handlers.
Yielding cleanup can keep all request slots occupied while the heartbeat stays
healthy. The default recycling limits are disabled, so native health alone does
not guarantee automatic recovery. See [stuck work](supervision.md#request-deadlines-and-stuck-work)
for cooperative cleanup, lifetime recycling and functional-probe policies.

JSON integers preserve precision up to 4096 digits; floats must be finite. JSON
nesting is capped at 64 levels. Route literals must be ASCII URI text, with
explicit percent encoding for non-ASCII literals; parameter values support
Unicode. Cookie parsing uses strict RFC 6265 octets and rejects malformed or
duplicate names. Validation errors omit raw input values and arbitrary validator
messages; reported locations can retain submitted dictionary keys.
Explicit HTTPException details remain application output.

Default application request/response bounds are 1 MiB/4 MiB. Normalization and
native encoding are bounded; application code and external model serialization
can allocate before the framework sees a result. Application response headers
are capped at 64 fields and 16 KiB. The default request transport uses at most
1024 connections, 64 headers, 32 KiB header buffer, a 10-second header deadline
and an 8192-byte normalized request target. The target limit applies to
`http::Uri`, which may strip fragments; the full raw request line/header block
is still bounded by the header-buffer limit. See configuration docs for ranges.

Aggregate retained request and response payload budgets each default to 32 MiB
per worker, are capped at 1 GiB and cannot be smaller than the corresponding
individual size limit. Incoming actual body chunks stay charged through Python
completion/cancellation cleanup; response bytes remain charged through transport
ownership, including native static schema/docs responses. Small native health
and error replies are exempt; metrics retains its separate 4 MiB single-scrape
guard. This is logical payload accounting, not a physical heap guarantee:
application/model allocations, temporary serialization, headers, startup storage
and kernel buffers remain outside the budget. Independent worker/replacement
budgets can overlap. See [application API](application-api.md) for configuration.
Processing deadlines cover body reading, validation, submission, handler/cleanup
and encoding to response readiness, excluding final socket drain. Synchronous
GIL-held work cannot be preempted, and timed-out cleanup can continue while
holding request resources. Admission `503` and incomplete-body `504` close the
connection; a handler `504` after a consumed body preserves keepalive.

API keys and metrics tokens are read from named environment variables; no key
values are exported in route/schema configuration. Host allowlists, proxy trust
and health exemptions are explicit. Rate limits are process-local. Metrics
require one worker until cross-process aggregation exists, use fixed/bounded
labels and a 4 MiB scrape cap, report omitted series, and admit only one scrape
body at a time. Access logs are best-effort through a bounded queue and never
include raw URLs, query strings, request bodies or authorization headers.
Metrics duration/in-flight accounting ends when the response is ready and
excludes socket drain; response byte reservations can persist through that drain.

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
