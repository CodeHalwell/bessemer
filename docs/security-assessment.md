# Security assessment

## Remediation of the remaining findings — 2026-11-19

After the four blockers below, the rest of the assessment's findings were
worked through the same way: each group in its own worktree, with a test
that fails before the fix, reviewed, integrated, and checked again against
the probe that first reproduced it
([production readiness](production-readiness.md#19-november-2026-the-reviews-remaining-findings)).

| Finding | Status | What changed |
| --- | --- | --- |
| M-6 remainder | Open | A peer that vanishes without closing is still noticed only when the handler next sends. |
| M-7 remainder | Open | See below. |
| M-8, M-9 | Fixed | File bodies share one descriptor per file through a table bounded at `max_connections` distinct files; the per-mount slots are gone. 300 descriptors became 1, and slow downloads no longer starve a mount. |
| M-10 | Fixed | A worker runs at most `max_connections` WebSocket sessions however they arrive: 300 handlers over three HTTP/2 connections with a limit of 4 became 4 admitted and 296 answered `503`. |
| M-11 | Fixed | A full rate-limit table degrades to shared per-network records (IPv4 /24, /16; IPv6 /48, /32; then one record) and never refuses for being full. A randomised test holds every client to `requests` per window across moves between records. |
| M-13 | Open | Streamed chunks are still held outside the response byte budget. |
| L-8, L-9, L-10 | Fixed | The loop's state is only locked with the interpreter attached and is always traversed; a stale registration is dropped, not re-armed, and a pending socket operation fails with `EBADF` once its socket is closed; each run begins with one non-blocking poll. |
| L-11 | Fixed | An extended CONNECT to anything but a WebSocket route is answered `400` before a handler runs; controls answer first. |
| L-12 | Fixed | With a client CA there is no session cache and no tickets, so every connection verifies its certificate. After expiry both a full and a would-be resumed handshake fail. |
| L-13 | Open | Inputs are still validated before dependencies run. |
| L-14 | Open | The parity table and the missing start-up warning are unchanged. |
| L-15 | Fixed | A spooled request's file parts share its one descriptor: 1,021 became 22 for the probe that held 1,000. |
| L-16 | Fixed | Urlencoded fields are counted in one scan that stops at the limit, before anything is decoded. |
| L-17 | Fixed | The native encoders refuse a string with a lone surrogate before the conversion that could reach the codec error registry; the two reproductions no longer crash on 3.13. A response dict that changes size while converted is a `500`, not a panic that stopped the worker's loop. |

L-18's items:

- **Fixed.** `Allow` only on the router's own `405`. An inbound trace
  context honoured only from `trusted_proxies` or with
  `trust_inbound_context=True`. A repeated `APIKeyQuery` parameter answered
  `400` (cookies keep the first value, now documented, as is
  `request.headers` returning the last duplicate). A near miss of a
  `BESSEMER_*` setting, or any unknown `BESSEMER_TLS_*` name, refuses
  start-up. A failed send drops its connection, so a stalled TLS reader
  holds its slot for the send timeout (4.0 s measured, from 10 to 12). A
  broken `run_cpu` pool is rebuilt. Server-sent event data is split on
  every line ending. `filename*` may not decode to a control character. A
  compressed resource's `304` and `206` carry `Vary` and the weak `ETag`.
  Route templates are counted once and only with telemetry on. A
  `FileResponse` is opened without waiting on a FIFO and read off the event
  loop. Under the Entra profile a signing key is used only for the issuer
  it publishes, checked against Microsoft's documentation and its live key
  sets: three tokens with a mismatched key, tenant or missing `tid` that
  were accepted are refused.
- **Documented, not changed.** The L-7 fix remains Unix-only: on Windows a
  static file is still opened by path after the containment check.

Open:

- **M-7 remainder.** Requests waiting for bodies are bounded, but answering
  100 streams a connection still costs memory. Heap profiling attributes it
  to the HTTP/2 library's own tables, which grow to a connection's peak
  stream count and are not shrunk while it stays open: about 150 KB a
  connection after 100 complete requests, about 290 KB after 100 body-less
  ones, and 3.4 to 5 KB for each stream while it is open. At the default
  1,024 connections that is of the order of 150 MiB held until idle
  connections time out, which they do after about two header timeouts.
  Send buffers, reset-stream bookkeeping and allocator retention did not
  appear. The settings that would bound it, fewer concurrent streams per
  connection or a worker-wide cap on open streams, change what a
  well-behaved client or an h2c ingress is given, and were not made in a
  release candidate.
- The formal performance gate, an external audit or penetration test, and
  qualification of the container image are outside this work and remain as
  recorded in [production readiness](production-readiness.md).

Found while fixing, and fixed: a request answered before its body was read
had its connection closed on the unread body, which resets it and can cost
the client the answer (Windows discards a response already received); the
server now reads and discards what the client is still sending for up to
five seconds. A `FileResponse` for a file its `yield` dependency removes
during cleanup is opened when the handler returns it, as before this work.

## Remediation — 2026-11-19

The four security findings the assessment below named as blockers are fixed,
with the two event-loop faults from the same review
([production readiness](production-readiness.md#19-november-2026-the-reviews-six-blockers-fixed)).
Each fix was made in its own worktree, reviewed, integrated, and then checked
against the probe that first reproduced the finding.

- **M-5.** The Python multipart parser decodes text parts as UTF-8 whatever
  charset a part declares, as the native parser does, so no codec named by a
  client is looked up. `filename*` must declare UTF-8, part header parameters
  are parsed in one linear pass in place of `email.message`, and a header
  line with more than 64 `;` is refused with `400`. An 80 KB `punycode` part
  went from 1.2 s to 1 ms and six concurrent 150 KB posts from 26 s each to
  under 6 ms. The worst header input found afterwards, an unterminated quote
  in each of 131 parts, costs about 90 ms for a 1 MiB body.
- **M-6.** While it waits for credit or room the WebSocket reader keeps
  reading into its 16 KiB buffer, the one it always had, so it sees the end
  of the stream; a failed write now ends the session as well. Buffered
  messages still reach a handler that keeps receiving, in order and with the
  client's close code. The four sessions that had been live after 45 s are
  released: one was left 10 s after the clients closed, none at 25 s. Still
  open: a peer that fills that buffer and the kernel's and then leaves, or
  that vanishes without FIN or RST, is noticed only when the handler next
  sends. That needs a server ping or an idle timeout, which would change what
  every client sees and was not added.
- **M-7.** A worker allows `max_connections` requests to wait for their
  bodies without an admission slot, the bound HTTP/1.1 has already, and
  answers `503` beyond it; a request whose body arrives with its head never
  takes a place. The 300 connections × 100 streams that had raised the server
  by 211 MiB now raise it by 76 to 86 MiB. What is left is not waiting
  requests but the cost of answering 100 streams a connection to clients that
  do not read, which complete requests incur too. It belongs with M-8 to
  M-10, which are unchanged.
- **M-12.** `add_middleware` with a native middleware class on a mounted app
  raises `ValueError`, and `build()` repeats the mount-time check over the
  whole mount tree, so a server-wide option set on a sub-app by any route
  after `mount()` is refused.

M-8 to M-11, M-13 and the Low findings were open when this was written; the
section above records what has been done about them since.

Validation of the fixed tree (`acc147a`): 369 Rust tests (six new) and 1,231
Python tests (26 new) passed on CPython 3.13.18, 3.14.6 and free-threaded
3.14.6t with the GIL disabled, with formatting, Clippy with warnings denied
and Ruff. The tests that reproduce each finding fail on the unfixed wheel;
the rest pin behaviour that must not change. GitHub's check workflow passed on the branch with the first five fixes (`a9a58cd`).
No container was built and nothing ran on macOS or Windows here.

## Assessment at `91dd18f` — 2026-11-19

Scope: the whole tree at `main` `91dd18f` (1.0.0rc7 plus the nine unreleased
commits since the `v1.0.0rc7` tag), reviewed before the next candidate. The
last full assessment below was at 1.0.0rc1; `src`, `crates` and `python` have
gained about 24,000 lines since. Seven subsystem reviews read the native
transport, the codec and FFI crates, the event loop crate, the native controls
and authentication, uploads/static files/WebSockets/compression, the request
execution core and the Python package, each with black-box probes against the
installed wheel. The findings marked *re-run* were reproduced a second time,
independently, with separate probes. No application code was changed.

This is again an internal review by the maintainers' tooling, not an
independent audit. Performance findings from the same review are in
[production readiness](production-readiness.md#19-november-2026-pre-release-security-and-performance-review).

**Result.** No High finding: no authentication bypass, no file disclosure
outside a static root, and no memory-unsafety reachable from request bytes.
There are several cheap remote denials of service at default settings and one
control that fails open silently. M-5, M-6, M-7 and M-12 should be fixed
before a final candidate; M-8 to M-10 share M-7's cause.

| ID | Finding | Severity | Status |
| --- | --- | --- | --- |
| M-5 | `request.form()` decodes multipart fields with a client-chosen codec; `punycode` is quadratic | Medium | Fixed 19 November |
| M-6 | A WebSocket peer that leaves after 64 unread frames keeps its handler and connection slot for ever | Medium | Fixed 19 November |
| M-7 | HTTP/2 requests waiting for a body hold no admission slot: 100 a connection, about 7 KiB each | Medium | Fixed 19 November |
| M-8 | `FileResponse` holds one descriptor per HTTP/2 stream with no bound | Medium, conditional | Open; re-run |
| M-9 | About 11 HTTP/2 connections can take every slot of a static mount | Medium, conditional | Open |
| M-10 | WebSocket sessions over HTTP/2 are bounded at 100 × `max_connections`, not `max_connections` | Medium, conditional | Open |
| M-11 | A full local rate-limit table refuses every client without a live entry | Medium, conditional | Open; documented fail-closed |
| M-12 | Native middleware added to a sub-app after `mount()` is silently unenforced | Medium, conditional | Fixed 19 November |
| M-13 | Streaming responses hold three to five chunks a stream outside the response byte budget | Medium, conditional | Open |
| L-8 | The event loop's `__traverse__` skips its contents when its state lock is held | Low | Open; forced interleaving only |
| L-9 | The event loop re-arms a stale registration on a reused descriptor number | Low, conditional | Open |
| L-10 | Readiness is lost between consecutive `run_until_complete` calls on the embedded loop | Low | Open |
| L-11 | Extended CONNECT to an ordinary route runs its GET handler and answers 200 | Low | Open |
| L-12 | With mutual TLS, session resumption keeps an expired client certificate working | Low | Open |
| L-13 | Body validation runs before authentication dependencies | Low | Open; re-run |
| L-14 | HTTP and ASGI middleware do not wrap static mounts, OpenAPI, docs, health or WebSockets | Low | Documented; parity table says "Same" |
| L-15 | A spooled upload duplicates one descriptor per file part | Low, opt-in | Open |
| L-16 | The urlencoded field limit is applied after the whole body is decoded | Low | Open; re-run |
| L-17 | GIL-build encoders can run Python code while holding borrowed pointers | Low | Open; re-run |
| L-18 | Smaller items listed below | Low | Open |

### M-5 Multipart fields decoded with a client-chosen codec

`_multipart_form` (`python/bessemer/request.py:843-847`) decodes each text part
with `part.get_content_charset()`. The native form parser accepts UTF-8 only;
this Python one accepts any text codec the client names, and CPython's
`punycode` decoder is pure Python and quadratic. It is reached from
`await request.form()` and from form models (`Annotated[Model, Form()]`);
scalar `Form()` and `File()` parameters use the native parser and are not
affected. Re-run on a default server with a route that only calls
`request.form()`: one unauthenticated part took 0.10 s at 20 KB, 0.30 s at
40 KB and 1.2 s at 80 KB, and six concurrent 150 KB posts took 26 s each. The
default body limit is 1 MiB, which the first review measured at over 100 s of
CPU for 500 KB. The work runs on the sync pool, is not stopped by the request
timeout, and starves `def` routes while async routes and the heartbeat stay
healthy, so the supervisor does not replace the worker. `email.message`
parameter parsing in the same function is a second, smaller cost (128 parts
with 8,000 `;` each: 2.7 s). Decode fields as UTF-8 only and parse
`Content-Disposition` with a bounded linear parser, or route `request.form()`
through the native parser.

### M-6 WebSocket session leak after a peer exhausts inbound credit

The reader task waits for inbound credit before it reads the next frame
(`src/websocket.rs:615-620`), and the session's `select!` (`:844-861`) has no
arm that notices the peer going away while the reader is parked. A handler
that accepts and does not call `receive` (a push endpoint waiting on its own
events) therefore never learns that a client which sent 64 frames has closed.
Re-run with `--max-connections 6`: two sessions closed without sending were
cleaned up within 8 s; four sessions closed after 70 two-byte frames were all
still live 45 s later, with their sockets still open in the worker. Each one
costs the client nothing once it has left, so about `max_connections` such
connections stop a worker accepting until it is restarted; over HTTP/2 one
TCP connection leaves 100. A handler that sends periodically recovers when
its send fails. Keep watching the socket for EOF while parked, and add a
server ping with a pong deadline or an idle timeout.

### M-7 to M-10 HTTP/2 multiplies per-connection bounds by 100

Several bounds are sized by `max_connections` and were reasoned about per
connection; an HTTP/2 connection carries 100 streams (`src/transport.rs:62`),
and plaintext prior-knowledge HTTP/2 is on by default.

- **M-7.** A request whose body has not arrived waits in `buffer_body`
  (`src/budget.rs:190-212`) without an admission slot, so `max_in_flight`
  never refuses it. Sending HEADERS without END_STREAM to any Python route,
  including a body-less GET, parks the stream until the request timeout.
  Re-run with `--max-in-flight 8`: 300 connections × 100 streams, 391 KiB sent
  in all, raised the server from 72 to 283 MiB and it stayed there after the
  clients left. The first review measured 728 MiB at the default 1,024
  connections for 1.45 MB sent. Bound requests waiting for a body with a
  worker-wide permit pool and refuse beyond it.
- **M-8.** `serve_response_file` (`src/static_files.rs:465-481`) gives a
  `FileResponse` body no slot, although static mounts have one for exactly
  this reason (`:36-40`). Re-run: three connections × 100 streams with no
  `WINDOW_UPDATE` held 300 descriptors for the file. Ten connections exhaust
  a 1,024 descriptor limit.
- **M-9.** A static mount's slots equal `max_connections` and are taken one a
  response before the path is resolved (`src/static_files.rs:79-81`). Eleven
  connections holding 1,024 stalled streams gave every other client `503`
  for any file in the mount for as long as the attacker took 16 KiB a stream
  inside each send timeout.
- **M-10.** Each connection admits 100 WebSocket handlers
  (`src/transport.rs:977`, `:997-1016`) while the documentation says sessions
  count against `max_connections`. Three connections ran 300 handlers with
  `--max-connections 4`.

`--http 1.1` avoids all four until they are bounded worker-wide.

### M-11 A full rate-limit table refuses everyone else

`admit_at` (`src/controls.rs:750-770`) answers `503` for any client without a
live entry once `max_keys` entries are active. That is the documented
fail-closed behaviour, and IPv6 clients are keyed by /64, but a routed /48
holds 65,536 /64s against a default of 10,000 keys: one request a window
from each keeps the table full, and every other client is refused before
authentication for as long as that continues. Reproduced at small scale
through a trusted proxy. When the table is full, fall back to a coarser
shared key or evict the least recently used entry rather than refusing
unknown clients outright.

### M-12 Native middleware on an already mounted app is dropped

`add_middleware` returns for native middleware classes before it checks
`_mounted` (`python/bessemer/application.py:1800-1805`), and `mount()` checks
a sub-app's server-wide settings only at mount time. Re-run:
`parent.mount("/sub", sub)` followed by
`sub.add_middleware(TrustedHostMiddleware, allowed_hosts=["api.good.test"])`
raises nothing, and `GET /sub/data` with `Host: evil.test` returns `200`. The
same call before `mount()` is refused with "a mounted app cannot set
server-wide options". Raise in `add_middleware` when the app is mounted, and
repeat the settings check over the mount tree in `build()`.

### M-13 Streamed chunks outside the response byte budget

A response stream keeps a chunk in Python's read-ahead, one in the pump, one
in the channel and up to two in the HTTP/1 output buffer; only the channel
and the pump's send are charged (`python/bessemer/worker.py:753-760`,
`src/streaming.rs:257-331`, `src/http1.rs:1772-1838`). The execution-core
review measured 150 clients that never read a stream of 1 MiB chunks holding
713 MiB against the default 32 MiB budget, and 1.3 GiB with 4 MiB chunks. It
needs a route that streams large chunks. Reserve before pulling a chunk and
keep the permit until the socket write completes. Not re-run.

### Low findings

- **L-8.** `LoopCore.__traverse__` returns without visiting anything when
  `state` is locked (`crates/bessemer-loop/src/core.rs:1171-1174`), and
  `wake_reader` and `runs_here` take that lock from threads that do not hold
  the interpreter. A collection whose first pass sees the lock free and whose
  second does not treats everything reachable only through the loop as
  garbage: finalizers run on live objects. Reproduced with the interleaving
  forced under a debugger (a pending task received `GeneratorExit`); not hit
  in 13,050 free-running collections. Keep `woken` and `driven` out of
  `State` so it is only locked with the interpreter attached.
- **L-9.** A registration left on a closed descriptor is retried as an add
  with its stale mask (`crates/bessemer-loop/src/poller.rs:59-81`), and the
  `sock_recv`/`sock_sendall` callbacks hold the raw number
  (`sock.rs:157-207`). An application that closes a socket with such an
  operation pending can have it complete on the next socket to get that
  number: the probe read another connection's bytes. asyncio raises instead.
- **L-10.** `drive` starts each run with `pending = false`
  (`core.rs:561`), so a descriptor still readable when one
  `run_until_complete` ends is not seen by the next until another event
  arrives. The worker makes four such calls around shutdown.
- **L-11.** Any `CONNECT` with `:protocol=websocket` is rewritten to `GET`
  (`src/transport.rs:997-1013`); only WebSocket routes then check for it. An
  ordinary GET route runs and answers `200`, which RFC 8441 defines as an
  open tunnel, without the WebSocket origin allowlist. Refuse it unless the
  route is a WebSocket route.
- **L-12.** The mutual-TLS configuration keeps rustls's session cache and
  tickets (`src/tls.rs:243-251`). A resumed session restores the client
  chain without verifying it again: after the certificate expired a full
  handshake failed and a resumed one returned `200`, and each resumption
  issues new tickets. A changed CRL file is unaffected, because a rebuilt
  configuration has a fresh cache. Disable resumption when `client_ca` is
  set.
- **L-13.** Arguments are validated before `before` and security
  dependencies run (`python/bessemer/worker.py:2946-2972`). Re-run with
  `App(dependencies=[Depends(auth)])` and no credentials: a valid body gets
  `401`, a body of the wrong shape gets `422` naming the model's fields.
  FastAPI resolves dependencies first and would answer `401` (from its
  design; not run here).
- **L-14.** `@app.middleware("http")` and ASGI middleware wrap Python routes
  only. [The application API](application-api.md#http-middleware) says so,
  but the parity table lists the middleware as "Same". An application ported
  from FastAPI that authenticates in middleware serves its static mounts,
  `/openapi.json`, docs and WebSockets without it (reproduced). Correct the
  table and warn at startup when both are present.
- **L-15.** With `max_upload_size` set, each file part of a spooled request
  gets its own duplicated descriptor (`src/uploads.rs:121-135`): one 71 kB
  request with 1,000 parts held 1,000.
- **L-16.** `src/forms.rs:90-97` decodes every pair before counting them.
  Re-run: a 1 MiB body of `a&` answered `400` after 67 ms and raised the
  worker's peak RSS from 48 to 96 MiB.
- **L-17.** `PyUnicode_AsUTF8AndSize` (`crates/bessemer-codec/src/lib.rs:367`)
  consults the codec error registry for a string holding a lone surrogate. An
  application that has replaced the `"strict"` handler, and shrinks the
  container being encoded from inside it, makes the GIL-build encoders read
  freed items. Re-run: a segmentation fault on 3.13 and a correct result on
  free-threaded 3.14t, which holds strong references. Scan for surrogates
  before the call.
- **L-18, smaller items.** Control rejections carry the path's `Allow`
  header, so `401` responses enumerate routes (`src/router.rs:554-560`). An
  inbound `traceparent` from any client forces sampling and chooses the
  trace id (`src/otel.rs:241-288`). `APIKeyQuery` and `APIKeyCookie` accept
  a repeated credential, and `request.headers.get` returns the last of
  several values where Starlette returns the first. A mistyped setting such
  as `BESSEMER_TLS_CERTFILE` is ignored and the server starts in plaintext.
  A stalled reader on TLS holds its connection slot for 2.5 to 3 times the
  send timeout. One dead `run_cpu` child leaves the process pool broken for
  the life of the worker (re-run). An `EventSourceResponse` item containing
  `\r` ends the stream. `filename*` admits NUL, CR and LF into
  `UploadFile.filename`. A dynamically compressed `304` carries the identity
  ETag without `Vary`. The route-template limit of 1,024 counts each method
  and applies with telemetry off, so an app with 520 two-method paths does
  not start (re-run). `FileResponse` opens its file on the loop thread. The
  L-7 fix is Unix-only; Windows still opens by path. The multi-tenant Entra
  preset does not bind a signing key to its issuer (not checked against
  Microsoft's documentation).

### Earlier findings

M-3, M-4, L-2 and L-7 were checked again and their remediations hold on
Linux: an HTTP/2 connection with a zero window closes at the send timeout,
only exact `bytes` is aliased by `sock_sendall`, browser WebSocket handshakes
need an allowed origin on HTTP/1.1 and HTTP/2, and 200,000 requests against
an actively swapped static tree leaked nothing. The duplicate-credential
remediation is incomplete for the two helpers in L-18.

### Reviewed and found sound

- **HTTP/1 framing and limits.** 81 framing cases behaved as intended:
  conflicting, signed or overflowing lengths, non-final transfer codings,
  obs-fold and bare CR are refused, including with `Upgrade` added. An unread
  or partly read body is never parsed as the next request; header, target
  and count limits hold on both parsers; application headers cannot split a
  response; all connection slots were usable after 18 abuse patterns. HTTP/2 CONTINUATION, rapid-reset, PING and SETTINGS floods
  are closed or absorbed without growth.
- **Codec.** 315,712 documents parsed identically to `json.loads`, 120,000
  model bodies agreed with Pydantic on validity and value, and 140,000
  encoded values matched `json.dumps` and `TypeAdapter.dump_json` byte for
  byte. Hostile `__eq__`, `__hash__` and `__index__` on subclasses are never
  called. The free-threaded critical sections were read and found consistent.
- **Controls.** Every transport path reaches the controls before routing;
  exemptions are exact (doubled slashes, dot segments, percent-encoded and
  case variants all need credentials); JWT, JWKS,
  API-key, CORS, Host, proxy and Redis behaviour matched the documentation.
- **Execution core.** No persistent `503` after any disconnect, reset,
  deadline or exception pattern tried, on either loop, with two loops, with
  HTTP middleware and over HTTP/2; no Tokio thread waits on a Python future
  result.
- **Python package and supervisor.** Redirects are relative and cannot leave
  the host, response headers and cookies reject CR, LF and NUL, the docs page
  loads nothing from a CDN, `500` bodies and logs omit exception messages,
  and the control channel is an anonymous pipe with no temporary files.

### Validation

Built wheels from this tree on Linux x86_64 for CPython 3.13.18, 3.14.6 and
free-threaded 3.14.6t and ran the installed-package suite on each: 1,205
tests passed on 3.14 and on 3.14t with the GIL disabled; on 3.13 the only
error was a test that needs Starlette, whose module passed once Starlette
was installed. Formatting, Clippy with warnings denied, 363 Rust tests (two
ignored), Ruff, the 83 harness tests and the shell checks passed. All eight
workflows were green on GitHub for `91dd18f`.

`cargo-audit` 0.22.2 found no advisory in 128 crates, and `pip-audit` 2.10.1
none in the production and installer lockfiles. The HTTP/1 parser fuzzer ran
about 200 million iterations over four seeds without a failure. Six of the seven
Hypothesis transport properties passed at 2,000 examples each.

The seventh cannot pass, and the harness cannot run as documented:
`tests/security/fuzz_transport.py` still asserts that a duplicate cookie is
refused with `400`, which the cookie contract no longer does, and its
`--wheel` check rejects every build since `py.typed` was added because it
hashes only `.py` and `.so` files. Its last recorded results are from
4 November.

Not covered: the container image (not built or scanned), macOS and Windows,
sanitizer runs outside CI, coverage-guided fuzzing, an external audit, and
free-threaded behaviour beyond the test suite. The sanitizer workflow builds
only CPython 3.13, so the `Py_GIL_DISABLED` paths of the codec are never
compiled under AddressSanitizer or ThreadSanitizer.

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

Follow-up, 10 November 2026: that deadline is an idle timeout, so a client
that took a little inside each timeout could still hold a response and its
share of the budget. Writes are now also held to a floor, `min_send_rate`
bytes a second (default 240; `0` for none): from the first write that blocks
until the HTTP/1 response ends, the time writes spend blocked may
not exceed the send timeout plus the time the bytes taken would need at the
floor. An immediately accepted frame adds byte credit without resetting the
response's accumulated blocked time. HTTP/2 retains its socket write-burst
floor and per-stream body floor; a stream's clock keeps running between chunks
while its body has more ready for the peer.
The default floor is deliberately low; a deployment that serves large
responses to untrusted clients should raise it. Tests:
`writes_taken_too_slowly_run_out_of_time_inside_the_idle_timeout`,
`immediately_taken_frames_keep_the_response_send_floor`,
`large_response_frames_keep_the_floor_across_producer_polls`, and
`a_reader_below_the_floor_is_disconnected_and_one_above_it_is_not` in
`src/http1.rs`; `an_http2_stream_taken_a_little_at_a_time_runs_out_of_time`
and `a_window_that_ends_on_a_chunk_boundary_does_not_escape_the_send_timeout`
in `src/transport.rs`; `SendFloorTests` in
`tests/python/test_resource_bounds.py`.

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
