# Security assessment

## Remediation — 2026-11-06

The changes following the assessment add an Entra access-token claim policy,
bound kidless key verification, close stalled HTTP/2 connections, snapshot
mutable Python send buffers, open static files beneath a pinned directory with
no-follow descriptors, and require an explicit origin allowlist for browser
WebSocket handshakes. Security dependencies reject duplicate credential headers.
A scheduled dependency audit and Dependabot cover the Rust, Python and GitHub
Actions dependencies.

Entra delegated tokens must carry a nonempty `scp`; application tokens must
carry `idtyp=app` and at least one role. Applications must configure Entra to
emit `idtyp` for app-only tokens. The HTTP/2 timeout closes the entire connection,
including other streams, and tracks completion of 16 KiB chunks. A client that
receives less than one chunk within the timeout can therefore be disconnected.

Validation: 30 focused Python tests passed against a rebuilt macOS arm64
CPython 3.13 wheel. The earlier ID-token-shaped probe now returns `401`, while
the scoped access-token-shaped probe returns `200`. The HTTP/2 probe again
filled the response budget, then recovered after the two-second send timeout
while the peer remained open. Twelve Rust JWT tests and focused Rust tests for
zero-window HTTP/2, stream progress, symlink replacement and FIFO rejection
passed. The mutable-buffer event-loop regression passed on macOS; the Linux
event-loop crate passed a cross-target check and Clippy, but its new Python
regression was not run on Linux here. The current Cargo graph and
resolved optional Python dependencies returned no known advisories. The current
container image was not rebuilt or scanned.

## Assessment at `e70b096` — 2026-11-05

The statuses below describe the assessed revision before the remediation above.

Scope: the installed 1.0.0rc1 package, Python public API and security helpers,
native HTTP/1 and HTTP/2 transport, TLS, JWT/JWKS, CORS, proxy and host controls,
rate limiting, uploads, static files, WebSockets, compression, Rust event loop,
codec/FFI, process supervisor, container recipe, release workflows and pinned
dependencies. This follow-up reviews the changes since the earlier assessment
at `5df71f4` below. No application code was changed.

| ID | Finding | Severity | Status |
| --- | --- | --- | --- |
| M-2 | Entra ID token can pass an API route that requires only `Principal` when browser and API share an app registration | Medium, conditional | Open; reproduced with a locally signed token |
| M-3 | HTTP/2 streams stalled by flow control retain the response budget indefinitely | Medium | Open; reproduced against the installed wheel |
| M-4 | The opt-in Rust event loop creates immutable slices over writable Python send buffers | Medium | Open; confirmed in code, not exercised on Linux |
| L-7 | Static file containment has a symlink replacement race in writable served trees | Low, conditional | Open; filesystem sequence reproduced |
| L-2 | Cookie-authenticated WebSockets need an Origin check when CORS is absent | Low, conditional | Open; documented below |
| L-5 | Dependency auditing and updates do not run in CI | Low | Open |
| L-6 | A kidless JWKS tries every matching key for an invalid token | Low | Open |

### M-2 Entra ID token accepted as API authentication

`JWTAuth.entra` describes access-token verification but supplies the RS256 key
set, issuer and audience without an access-token-specific rule
([`auth.py`](../python/bessemer/auth.py#L78),
[`jwt.rs`](../src/jwt.rs#L633)). Microsoft documents that a v2 access token for
an API and an ID token for an interactive client can both have that app's client
ID as `aud` when the same registration serves both roles. The verifier accepts
a correctly signed token carrying valid `iss`, `aud` and expiry even if it has
no `scp`, `roles`, `azp` or `appid` claim.

An installed-wheel probe used `JWTAuth.entra(tenant, client_id)` with a local
JWKS substituted for network access. A locally signed token with ID-token-shaped
claims, including `nonce` and no API permission claim, returned `200` from a
`Principal`-only route. The same token returned `403` on a route requiring
`items.read`; a token with `scp=items.read` returned `200`; a wrong-audience
token returned `401`. This proves the validator's behaviour, not issuance of a
token by a live Entra tenant. Use separate browser and API app registrations
and explicit route permission checks until the preset enforces an access-token
policy. Microsoft distinguishes [ID token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
from [access token claims](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference)
and says [APIs must validate access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens).

### M-3 HTTP/2 flow-control denial of service

Buffered Python responses reserve their entire body before transport
([`lib.rs`](../src/lib.rs#L715), [`budget.rs`](../src/budget.rs#L39)). A client
that reads socket bytes but sends no `WINDOW_UPDATE` leaves HTTP/2 streams
active, so the socket send deadline does not fire and the connection idle check
does not close it ([`transport.rs`](../src/transport.rs#L460),
[`transport.rs`](../src/transport.rs#L583)). Against the installed wheel, one
HTTP/2 connection opened eight sequential 4 MiB responses, withheld window
updates and held the default 32 MiB response budget. A separate request got
`503 {"detail":"Response byte capacity is full"}` immediately and again after
three seconds. It returned `200` after the stalled peer closed. Direct HTTP/2
exposure therefore lets one peer deny buffered responses for a worker until
it disconnects. Add a per-stream send-progress deadline and reset stalled
streams; `--http 1.1` avoids this path in the meantime.

### M-4 Writable Python buffer aliased as immutable Rust memory

`sock_sendall` accepts a writable Python buffer and creates `&[u8]` directly
from `PyBuffer::buf_ptr()` in both its first send and pending callback
([`sock.rs`](../crates/bessemer-loop/src/sock.rs#L265),
[`sock.rs`](../crates/bessemer-loop/src/sock.rs#L150)). PyO3's buffer API warns
that the memory may change and exposes it safely through read-only cells.
`send_once` also calls Python signal handlers on `EINTR` while retaining the
slice ([`sock.rs`](../crates/bessemer-loop/src/sock.rs#L75)). A handler can
mutate the `bytearray` then; free-threaded Python permits another thread to do
so concurrently. This violates Rust's immutable-reference guarantee and may
crash a worker or corrupt memory. The path requires the opt-in Linux Rust
event loop and a mutable send buffer. Copy to owned bytes before sending.
This was a code-level finding; this macOS host could not execute the Linux loop.

### L-7 Static file symlink replacement race

Static serving canonicalises and checks the path, then asynchronously reopens
it by pathname ([`static_files.rs`](../src/static_files.rs#L200),
[`static_files.rs`](../src/static_files.rs#L294)). A lower-trust actor with
write access to the served tree can replace the checked file or parent with a
symlink before `open`; the opened file may be outside the root. Precompressed
siblings have the same check/open gap. A deterministic local filesystem probe
read an outside file after the swap; no end-to-end HTTP race was attempted.
Serve only directories that untrusted actors cannot modify until this path
opens and verifies a pinned inode beneath the root.

### Other open items and supply chain

The earlier L-1 is M-3 above, raised to Medium after the live probe. L-2,
L-5 and L-6 remain open. Python's `HTTPBearer` and `APIKeyHeader` helpers use
the last duplicate request header; native JWT and API-key controls reject
duplicates. This matters if an upstream hop applies a different
duplicate-header rule; it was not a standalone authentication bypass in the
review.

Fresh `cargo deny --locked check advisories` checked 126 crates against RustSec
with zero advisories. `pip-audit 2.10.1` reported no known vulnerabilities in
the pinned production (five distributions) and installer (two distributions)
Python lockfiles. The container was not rebuilt or scanned in this follow-up.
The last recorded exact-image scan, in the earlier qualification record, had
zero critical and 45 high OS-package matches across nine CVEs, including an
unresolved SQLite FTS5 denial-of-service advisory; those are historical image
results, not a clean bill for a current image.

Validation: built and installed the macOS arm64 CPython 3.13 wheel from
`e70b096`; 26 focused JWT, static-file and HTTP/2 tests passed; ran the
ID-token and HTTP/2 probes described above. No full suite, Linux event-loop
run, fresh image scan, independent penetration test or coverage-guided fuzz
run was performed in this follow-up.

---

# Earlier pre-release security assessment

Assessed: 2026-11-05, against `main` at `5df71f4` (1.0.0rc1 plus the Rust
event loop and io_uring work). Scope: the native extension (HTTP/1 and HTTP/2
transport, controls, uploads, static files, WebSockets, JWT), the opt-in Rust
event loop and io_uring backend, the Python application layer (mounts, ASGI
bridging, middleware), dependencies, and the CI and release workflows.

This is an internal review by the maintainers' tooling, not an independent
audit. It is a code review backed by tests, a parser fuzzer and black-box
probes of a running server; it found no high-severity issue.

## Summary

| ID | Finding | Severity | Status |
| --- | --- | --- | --- |
| M-1 | A client that stops reading holds its connection and response bytes indefinitely | Medium | Fixed (HTTP/1.1, upgrades, HTTP/2 socket stalls) |
| L-1 | HTTP/2 streams stalled by flow control are not timed out | Low | Open, follow-up |
| L-2 | Cookie-authenticated WebSockets are open to cross-site hijacking unless the app checks `Origin` | Low | Documented |
| L-3 | Control characters in chunk extensions and trailers were ignored | Low | Fixed |
| L-4 | `opentelemetry-api==1.45.0` pinned in dev/CI (CVE-2026-114603) | Low | Fixed (1.45.1) |
| L-5 | No dependency audit or update automation in CI | Low | Open, recommendation |
| L-6 | A JWKS without `kid`s makes each bad token try every key | Low | Open, recommendation |
| I-1 | Any request with `Upgrade` is handed to hyper for the rest of the connection | Info | Accepted |
| I-2 | io_uring widens the kernel attack surface | Info | Documented (opt-in) |
| I-3 | `{path:path}` values are percent-decoded and may contain `..` | Info | Application responsibility |
| I-4 | Release workflow checkouts kept the job token in `.git/config` | Info | Fixed |

Severity follows impact on a default deployment: High is remote compromise or
an authentication bypass, Medium a cheap remote denial of service or a
security control that fails open, Low a hardening gap or an issue that needs
an unusual configuration, Info a note with no direct impact.

## Findings

### M-1 Slow readers held connections and response bytes (Medium, fixed)

Neither the lean HTTP/1 connection (`src/http1.rs`) nor the hyper paths used
for upgrades and HTTP/2 put any bound on a response write. The header
deadline only covers reading a request head, and `request_timeout` only
covers the application producing a response. A client that sent requests and
then stopped reading parked the write for ever: the connection kept one of
`max_connections` slots, and the response bytes kept their share of
`max_response_bytes_in_flight` (32 MiB by default), because frames are held
until written.

Impact: a handful of connections each asking for a large response exhausts
the response byte budget, after which every other client gets `503`; a few
hundred idle readers exhaust the connection slots. Ingress proxies that
buffer responses (nginx's default) hide this; direct exposure, which native
TLS invites, does not.

Reproduced against the unpatched build: 40 pipelined 2 MB responses on one
connection whose client slept for 8 seconds before reading. The server waited
for as long as the client liked and then sent all 80 MB.

Fix: `http1::SendDeadline` wraps every accepted connection (HTTP/1.1, the
hyper upgrade hand-off and therefore WebSockets, and HTTP/2). A write, flush
or shutdown that stays blocked for the send timeout fails with `TimedOut` and
the connection closes. The clock only runs while the socket refuses bytes,
and any wake from the underlying stream restarts it, so slow but moving
downloads, buffered transports (io_uring, TLS) draining a large flush to a
slow reader, and server-sent events waiting for their next event are
unaffected. The send timeout is the longest request
timeout in force (`request_timeout` or a longer per-route `timeout`), so no
new setting was added. The same probe against the fixed build received 3.9 MB
and then a close, three seconds after the stall began.

Tests: `a_peer_that_stops_reading_is_disconnected`,
`the_send_deadline_only_counts_blocked_time` and
`a_flush_that_keeps_draining_is_not_a_stall` in `src/http1.rs`.

### L-1 HTTP/2 flow-control stalls (Low, open)

An HTTP/2 client can keep reading the socket (so M-1's deadline never fires)
while never sending `WINDOW_UPDATE` for a stream. hyper then stops polling
that stream's body; the stream stays open, the connection's idle check sees
an open stream, and the body's bytes stay charged to the response budget.
Exposure is bounded by `HTTP2_MAX_STREAMS` per connection and by
`max_connections`. hyper has no per-stream send-progress timeout; a
follow-up could record in `TrackedBody` when a stream last yielded data that
was not then sent, and reset streams stalled past the send timeout. Until
then, deployments that face untrusted clients directly can serve HTTP/1.1
only (`--http 1.1`) or terminate HTTP/2 at an ingress proxy.

### L-2 Cross-site WebSocket hijacking (Low, documented)

Browsers send cookies with cross-site WebSocket handshakes, and CORS does not
apply to them. Without a `cors` configuration Bessemer does not check
`Origin` on the handshake, as Starlette and FastAPI do not. An application
that authenticates WebSockets with a session cookie, which the docs suggest
for browser clients, is open to any page the user visits. The application
API guide now says to check `websocket.headers["origin"]` (or configure
`cors`) whenever a WebSocket relies on cookies. Making the check automatic
would change behaviour for existing non-browser clients, so it is left to the
application.

### L-3 Control characters in chunk metadata (Low, fixed)

The chunked decoder ignored everything after `;` on a chunk-size line and
discarded trailer fields after checking only for a colon. A bare CR inside an
extension or trailer is a line end to some intermediaries and not to others,
the ingredient of several published smuggling chains. Bessemer only accepts
CRLF-terminated lines, so it was not exploitable on its own, but it now
refuses control characters (other than tab) in both places with an error, as
a front proxy and Bessemer can then never disagree about chunk boundaries.

### L-4 `opentelemetry-api` 1.45.0 (Low, fixed)

`pip-audit` reports CVE-2026-114603 (GHSA-xcq7-p3pf-p88h): quadratic CPU in
`TraceState.from_header` for long whitespace runs. Bessemer only hands the
API `tracestate` values it has already limited to 512 bytes
(`MAX_TRACESTATE` in `src/otel.rs`), so its own propagation is not exposed,
but the dev extra and CI pinned the affected version. All three pins now use
1.45.1. The `otel` extra keeps its `>=1.27` floor; applications that also run
OpenTelemetry's ASGI instrumentation should upgrade it themselves.

### L-5 No dependency auditing in CI (Low, open)

`cargo audit` (126 crates, RustSec database of 2026-11-05) found no
advisories, and `pip-audit` found none in the runtime or test dependencies
beyond L-4. Neither runs in CI and there is no Dependabot configuration, so
the next advisory would go unnoticed until someone looks. Recommended: a
weekly scheduled job running `cargo audit` and `pip-audit` against the
pinned dev and CI requirements, and Dependabot for `github-actions`, `cargo`
and `pip`.

### L-6 Key search for tokens without a matching `kid` (Low, open)

When none of the configured keys carries a `kid`, a token is verified
against every key bound to its algorithm, up to the 256-key limit. With RS256
at roughly 21 µs per check, a bogus token can cost about 5 ms of CPU. Key sets
from identity providers always carry `kid`s, so this only affects hand-made
sets. A smaller limit for keys without `kid`s, or a warning at startup, would
close it.

### I-1 `Upgrade` hands the connection to hyper

Any HTTP/1.1 request with an `Upgrade` header, or `CONNECT`, is replayed into
hyper, which serves the rest of that connection. hyper is configured with the
same header deadline, header count and buffer limits, and Bessemer is a
single hop, so the change of parser cannot desynchronise anything. It does
mean the hyper path is reachable by any client, so it stays in scope for
review and dependency updates.

### I-2 io_uring backend

`io_backend="io_uring"` is opt-in and needs a seccomp profile that allows the
io_uring system calls (`deploy/seccomp-io-uring.json`). io_uring has had a
steady run of kernel privilege-escalation bugs, which is why container
runtimes and several distributions block it by default. The Rust code was
reviewed for buffer lifetime: send buffers stay in their connection until the
completion arrives, slots are reused only with nothing in flight, stale
completions are rejected by a 24-bit generation, and the provided-buffer ring
and in-flight send buffers are deliberately leaked rather than freed when the
ring is torn down. No issue was found. Deployments should enable it only on
kernels they patch promptly.

### I-3 Path parameters

`{path:path}` and `{*rest}` values are percent-decoded, so `..%2f..%2f`
arrives as `../../`. This matches Starlette. Native static file serving
rejects `..`, hidden segments, backslashes and NULs, canonicalises, and
re-checks containment and hidden components after following symlinks, so it
is not affected. Handlers that turn a path parameter into a filesystem path
must do the same.

### I-4 Release workflow

The release workflow pins every action by commit SHA, publishes through PyPI
Trusted Publishing from a protected environment, generates attestations,
grants `id-token` and `contents: write` only to the jobs that need them, and
verifies that the tag matches the package version. `actions/checkout` keeps the
job's token in `.git/config` by default, so every checkout now sets
`persist-credentials: false` to keep it away from later build steps (fixed in
#70).

## What was reviewed and found sound

- **HTTP/1 framing.** Requests carrying both `Content-Length` and
  `Transfer-Encoding`, a non-final or unknown transfer coding,
  `Transfer-Encoding` on HTTP/1.0, signed, oversized or conflicting lengths,
  obs-fold and whitespace before a colon are all refused with `400` (black-box
  probes against a running server, all seventeen cases as expected). Bodies a
  handler did not read in full close the connection instead of being skipped,
  so a short read cannot leave body bytes to be parsed as the next request.
  The head deadline covers idle keep-alive time and endless blank lines, and
  pipelined read-ahead is limited to `max_header_size`.
- **Fuzzing.** `src/http1_fuzz.rs` mutates seed requests (bit flips,
  inserted framing tokens, deletions, duplications) and checks that parsing
  never panics, that no prefix of a head parses as a different request, that
  an accepted head never has both framings, and that body decoding gives the
  same bytes, verdict and leftover however the input is split, down to one
  byte at a time. Over 100 million iterations across 15 seeds found nothing
  (after one bug in the harness itself). `BESSEMER_FUZZ_ITERATIONS` and `BESSEMER_FUZZ_SEED`
  rerun it at scale; CI runs 20 000 iterations.
- **Unsafe code.** The main crate is `#![forbid(unsafe_code)]`. Unsafe code
  is confined to `bessemer-ffi` (descriptor duplication, `prctl`, GIL state),
  `bessemer-codec` (CPython object construction) and `bessemer-loop` (epoll,
  io_uring, contexts); each block carries a safety argument and none relies
  on peer-controlled lengths without bounds.
- **Request limits.** Body, header, target and upload limits are enforced in
  Rust before Python runs; reads of the body sit inside the request deadline;
  multipart parsing is linear (memchr), capped at 1000 parts, 16 headers and
  8 KiB of headers per part; spooled uploads are created `0600` with
  `O_EXCL` and unlinked immediately. WebSocket messages are limited before
  and after permessage-deflate decompression.
- **Response headers.** Application headers are validated by `http`'s
  `HeaderName` and `HeaderValue` (no CR, LF or NUL), framing and hop-by-hop
  headers are refused, and ASGI-mounted apps cannot set framing headers.
- **Mounts.** A mounted `App` cannot set server-wide options such as CORS,
  JWT, API keys or rate limits, so a sub-app cannot silently lose its own
  authentication; the outer app's controls cover mounted routes.
- **JWT.** `alg: none` is never accepted, keys are bound to algorithms so a
  public key can never be an HMAC secret, `crit` is refused, base64url is
  strict, `exp`, `nbf`, `iss` and `aud` are checked, and the verified-token
  cache is invalidated when the key set changes.
- **Defaults.** Bind address `127.0.0.1`; CORS, authentication, docs UI and
  metrics off; `X-Content-Type-Options`, `X-Frame-Options` and
  `Referrer-Policy` on; no tracebacks in responses; forwarding headers ignored
  unless `trusted_proxies` is set; API-key comparisons in constant time.

## Not covered

- An external penetration test or audit.
- Coverage-guided fuzzing (`cargo fuzz`) of the parser; the fuzzer here is
  randomised but not coverage-guided.
- TLS configuration beyond the code paths (rustls carries the protocol).
- The container image's operating-system packages, covered by
  `deploy/scan_image.py` in the container workflow.
