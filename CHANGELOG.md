# Changelog

All notable changes to Bessemer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (PEP 440 spelling for the Python
package). Detailed test evidence for each candidate lives in
[production readiness](docs/production-readiness.md) and
[qualification results](docs/qualification-results.md).

## [Unreleased]

### Performance

- On free-threaded Python, building a model from a typed JSON body no longer
  locks its new `__dict__` once for each field, or the new instance once for
  each Pydantic slot: the dict is made in one call and the slots are written
  directly while nothing else can reach the instance.
- Type identity checks on the request path no longer take a reference to the
  type, which a free-threaded build counts in a field every thread shares.

## [1.0.0rc10] - 2026-11-21

### Fixed

- Adaptive admission gives a CPU-busy event loop a bounded chance to recover
  before returning a 503 when connections run on separate I/O threads.
  Requests waiting for recovery use the existing in-flight capacity and
  retain their original deadline.

### Performance

- Typed JSON decoding reuses the parser's ASCII check when constructing
  Python strings and avoids repeating list and nullable value lookahead.
- Reduced JSON encoding work for ordered response models, including
  short string fields on free-threaded Python.

## [1.0.0rc9] - 2026-11-20

### Fixed

- Static files and `FileResponse` now choose HEAD headers and conditional
  validators using the result of compression for files that can be buffered.
  When compression would enlarge the file, HEAD keeps the identity metadata
  and a matching identity ETag returns `304`.
- HTTP/2 file responses reserve space before reading each streamed chunk.
  The reservation follows the bytes through transport, and a stream waits
  for a chunk to be released when its allowance is full.

## [1.0.0rc8] - 2026-11-19

### Changed

- On Linux and macOS with the default `event_loop="auto"`, a worker whose
  application has synchronous work serves its connections on two I/O threads
  beside Bessemer's loop, as a worker with `loops` above 1 or asyncio's loop
  does, instead of on the loop's own thread. Synchronous work is a `def`
  route handler, a request-scoped `def`, generator or class dependency at any
  depth (mounted apps and WebSocket routes included) or a `def` exception
  handler. The loop's thread takes the GIL for every callback, so while a
  handler thread held the GIL nothing was accepted, read or written: one
  `def` handler inside a 0.9 s C call delayed native `/healthz` by up to
  700 ms, long enough for a liveness probe to restart a healthy worker. It
  now waits under 3 ms. These applications pay for it in CPU per request.
  With the worker on one core, an async route of such an application used
  about 20% more at one connection and 30% more at 64 (89,000 to 70,000
  requests a second), and a sync route 20 to 40% more at one connection and
  no more at 64. `event_loop="bessemer"` named explicitly keeps the
  single-thread layout and its figures. All-async applications,
  `loops` above 1, asyncio's loop, `io_backend="io_uring"` and free-threaded
  builds with the GIL disabled are unchanged. Each worker writes the layout
  it chose to standard error when it starts, for example
  `Bessemer worker pid=4242 event_loop=bessemer loops=1 connections=io-threads (sync handler GET /report)`.
  An all-async application that holds the GIL in threads the worker cannot
  see at start (`asyncio.to_thread`, an executor, a C extension) still
  delays native routes on the single-thread layout; `event_loop="asyncio"`
  avoids it.
- A worker refuses to start when a `BESSEMER_*` environment variable is a
  near miss of a real setting (`BESSEMER_MAX_CONECTIONS`), or is any unknown
  `BESSEMER_TLS_*` name, and names the setting it resembles. Such a variable
  was ignored with at most a warning, so a limit or TLS option an operator
  believed was set was not. Other `BESSEMER_*` names still belong to the
  application.
- `APIKeyQuery` answers `400` when its parameter is repeated, as the header
  schemes do, with or without `auto_error`. Cookies keep the first value.
- Static mounts and `FileResponse` share one descriptor for each file being
  sent, read by position, and a worker holds at most `max_connections`
  distinct files open for bodies; a body for a further file gets the existing
  `503` "Static file capacity is full". The per-mount slots are gone: eleven
  slow downloads could take all of a mount's slots and leave every other
  client of that mount with `503`, `304`s included, and three HTTP/2
  connections that never opened their windows held 300 descriptors. `304`,
  `404`, `416` and `HEAD` take nothing. Resolving, opening and the first
  read of a static file are one trip to the blocking pool, which cut the
  server's CPU per small static response to between a third and a quarter.
  A file replaced while it is being sent finishes as the file its headers
  describe.
- A `FileResponse` whose path has become something other than a regular
  file is answered `500` at once: the open no longer waits for a FIFO's
  writer, which froze the worker. The file is still opened when the handler
  returns the response, so one removed by a `yield` dependency's cleanup is
  sent as before, and it is now read off the event loop.
- Response bodies and stream frames of 16 KiB or more are compressed on the
  blocking pool (it was more than 64 KiB, which a file's 64 KiB chunks never
  were), a stream that has compressed 64 KiB inline sends its later frames
  there too, and at most one compression per CPU runs at a time; the rest
  wait without holding a pool thread. Eight compressed downloads of a 48 MB
  file held native `/healthz` at a median of 50 to 150 ms; it is now about
  1 ms.
  Small bodies such as typical JSON are compressed inline as before.
- A `304` or `206` for a resource that compression applies to carries
  `Vary: Accept-Encoding`, and a `304` answering the weak `ETag` given with
  a compressed `200` carries that tag, as RFC 9110 requires, so a cache can
  match the revalidation to the representation it holds.
- A multipart upload to a route that spools to disk is refused with `413`
  before any of it is read when its `Content-Length` already exceeds
  `max_upload_size`, and a client that sent `Expect: 100-continue` is not
  invited to send it. It was read and written to disk up to the limit first.
- An HTTP/1.1 request answered before its body was read (a `413` for its
  declared length, a `401`) has the rest of that body read and discarded
  before the connection closes: until the client closes, sends nothing for
  half a second, or five seconds have passed. Closing on unread input resets
  the connection, and a client still sending then lost the answer; on
  Windows a response it had already received was discarded. Nothing read is
  kept and the request's admission slot has been returned by then.
- An incoming W3C trace context (`traceparent`, `tracestate`) is honoured
  only from a peer inside `trusted_proxies`, or from any peer with the new
  `OpenTelemetry(trust_inbound_context=True)`. Any client could choose the
  trace id and force its spans to be exported whatever `sample_ratio` was:
  with a ratio of 0, twenty unauthenticated requests were all exported with
  the client's trace id, `401`s included, and a flood displaced real spans
  from the queue. Other clients now start a new trace sampled by
  `sample_ratio`. **A service called directly by other services, with no
  proxy in `trusted_proxies`, must set `trust_inbound_context=True` to go
  on joining its callers' traces.**
- A full local rate-limit table no longer refuses clients. A client that
  cannot be given its own record shares one with its network (IPv4 /24 then
  /16, IPv6 /48 then /32, then a single record for everyone left), so the
  limit still applies, more strictly, and nobody is answered `503` "Rate
  limiter is at capacity" because the table is full: one routed IPv6 /48, or
  one request a window from each of 10,000 addresses, was enough to refuse
  every other client on every limited path. Records in use are never
  evicted, a client that moves from a shared record to its own keeps the
  history it had, and memory stays bounded. `Retry-After` from a shared
  record can be up to nine eighths of a window.
- The router adds `Allow` only to its own `405`. A control's `401`, `403`
  or `429` for a path whose method did not match carried the path's
  methods, which told an unauthenticated client which paths and methods
  exist.
- The limit of 1,024 route templates counts each path once, whatever its
  methods, and applies only when metrics, the access log or tracing uses
  them. An application with 520 paths that each took `GET` and `POST` and
  no telemetry failed to start.
- `access_log=True` costs about a tenth of what it did. Each record is
  encoded straight into a buffer and the writer takes whatever is queued
  in batches of whole records of at most 4,096 bytes, woken only when it is
  idle. On a trivial route the server's CPU per request with the log on
  fell from 26 to 30 µs to 10 to 12 µs, against about 9 µs without it, and
  `futex` and `write` calls for 40,000 requests from about 65,000 and
  40,000 to 83 and 3,100. The output is byte for byte what it was; a record
  can be written up to about a millisecond later.
- A worker runs at most `max_connections` WebSocket sessions, however they
  arrive; a handshake beyond that is answered `503` "Too many WebSocket
  sessions". Each HTTP/2 connection could carry 100 sessions, so the bound
  was 100 times `max_connections`: with `--max-connections 4`, three
  connections ran 300 handlers. HTTP/1.1 sessions, one a connection, cannot
  reach the bound on their own, and one HTTP/2 connection still carries at
  most 100.
- An HTTP/2 extended CONNECT to anything but a WebSocket route is refused
  with `400` "Not a WebSocket route" before any handler runs. It ran the
  route's `GET` handler, discarded the body and answered `200` without
  ending the stream, which a client reads as an accepted handshake, and the
  WebSocket origin allowlist was never consulted. Authentication and rate
  limiting still answer first.
- With `tls_client_ca` set, TLS sessions are never resumed: no session
  cache and no tickets, so every connection makes a full handshake and has
  its client certificate checked against the current time and CRLs. A
  resumed session restored the certificate chain without verifying it
  again, so an expired client certificate went on working for as long as
  its sessions kept being resumed. Servers without client authentication
  resume sessions as before.
- `JWTAuth.entra` with several tenants uses a signing key only for the issuer
  that key publishes. Microsoft's shared `common` key set holds keys for
  every tenant beside keys for one issuer only, each naming its issuer, and
  Microsoft requires an API reading that set to check it: a key's `issuer`,
  with the token's `tid` in place of `{tenantid}`, must equal the token's
  `iss`. Any key in the set was accepted for any listed tenant. A token whose
  `tid` disagrees with its `iss`, or that a one-issuer key signed for another
  issuer, is now answered `401`. Keys without the member (v1.0 key sets,
  static keys, other providers) and single-tenant configurations behave as
  before.
- `require_scope(...)` runs on the event loop instead of the handler pool
  and does not count as synchronous work, so an all-async application that
  uses it keeps the single-thread connection layout. A scope-checked route
  cost 124 to 215 µs a request in a probe on a busy host and now costs 37 to
  41 µs, the same as the check written `async def`. Calling the returned
  dependency directly still returns the principal.
- The verified-token cache hashes a bearer token before it takes its lock,
  and a route taking `Principal` is handed the verified claims without a
  copy of the claims tree for each request.
- Typed request models with more than 16 fields find a body key's field
  through an index built once per model, so keys out of order or unknown no
  longer cost a scan of every field: parsing a 1 MiB body for a 256-field
  model took 382 million instructions with its keys reversed and 686 million
  with unknown keys, and now takes 79 and 62 million. Narrower models are
  unchanged.
- Generic requests, those with dependencies, Request injection or HTTP
  middleware, do less work per call. The sync pool borrows fields from its
  work tuple and calls the usual keyword bindings by vectorcall, and the
  worker no longer wraps ordinary sync results or flattened async handlers in
  an extra coroutine, which saves two coroutine allocations for a generic
  dependency request and one for a generic sync Request handler. At 64
  connections, with the worker on one core, generic `Depends` ran 5.3%, 4.4%,
  5.7% and 16.6% faster on 3.13, standard 3.14, 3.14t with the GIL and 3.14t
  without it, using 8.1%, 9.1%, 11.0% and 18.0% less CPU per reply (paired
  medians against the previous build). A GIL-disabled `Request` route was
  7.9% faster with 9.5% less CPU. Other workloads were mixed: 3.13 inline
  CPU (0.971x), 3.13 byte responses (0.962x) and GIL-disabled JSON (0.987x)
  were slightly slower in paired medians, and GIL-disabled 3.14t still
  reached only 0.880x of standard 3.14 on generic `Depends`. The host was
  shared and unpinned.
- Adaptive admission charges the event loop's CPU from the instant a request
  joins the queue instead of from before it queued, and takes the queue
  timestamp once instead of reading the clock twice per handoff. A request
  that waits in the queue no longer has its wait counted as loop work. The
  measured effect was mixed and is not attributed: in one 395-sample run
  standard 3.13 JSON and 256 KiB bytes responses were 5.4% and 14.3% slower
  with 9.8% and 14.3% more CPU per reply, GIL-disabled CPU workers were 14.3%
  faster, and after the second clock read was removed the paired medians were
  +3.7% (JSON) and +2.8% (bytes) with ranges that crossed parity. The shared
  host does not establish that the change caused any of them.
- Synchronous routes that take no bindings are called through the pool's
  fixed-argument path, so they no longer build an unused argument vector,
  clone references or create a bound `Context.run`. `Request.url` and
  `base_url` of a native request read only the `Host` header and leave the
  rest of the headers unconverted, and typed `float` fields skip repeated
  generic type dispatch while keeping their depth, finiteness, byte-budget
  and signed-zero behaviour. In a 72-sample comparison `Request` throughput
  was 6.5% higher with 6.8% less CPU per reply and 256-model responses used
  3.2% less CPU; CPU-bound sync and typed echo ranges crossed parity. In the
  main 395-sample matrix four configurations were slower than the previous
  build in every round, including typed echo on 3.13, 3.14 and GIL-disabled
  3.14t, and a later study did not reproduce the typed-echo loss.
- The native sync pool signals a thread only when one is waiting, the invoker
  reuses interned names for its dispatch methods, and the typed model parser
  matches an expected plain field name and its colon together, leaving
  escaped keys, reordered fields and whitespace before the colon to the
  general parser. At 64 connections sync CPU throughput was 1.029x and CPU per
  reply 0.988x of the previous build, both ranges crossing parity. Typed echo
  varied between runs (0.957x to 0.960x in one matrix, 1.010x to 1.037x in the
  recheck), so no typed-echo gain is claimed.
- Typed JSON bodies decode matching ordered fields straight into the model
  without rebuilding the accepted prefix, short JSON numbers are accumulated
  while they are scanned, bound body arguments are moved to the call instead
  of copied, a complete single-frame body skips an extra allocation and
  poll, and free-threaded model responses write bounded runs of exact
  scalars under the container's critical section, rolling back if the
  expected shape fails. The free-threaded path needs CPython 3.14 or later
  for borrowed ASCII. On GIL-disabled 3.14t a 256-model response was 1.261x as
  fast with 0.806x the CPU per reply (all six pairs improved), raising its
  throughput relative to standard 3.14 from 0.759x to 0.968x. Typed echo
  improved 5.3% on 3.13 and 6.8% on GIL-disabled 3.14t, but all four echo
  ranges crossed parity and standard 3.14 and GIL-enabled 3.14t medians were
  below the previous build, so repeatable typed-echo throughput is not
  established.
- On standard CPython 3.14, model responses are encoded in one walk over
  compatible ordered dictionaries that matches the expected key identities as
  it writes, rewinding to the general path when the order differs. HTTP/1 now
  writes a reply larger than 8 KiB as headers plus body without copying the
  body into the connection buffer. At 64 connections a 256-model response ran
  18,110 requests a second, 1.120x the previous build (range 0.931 to 1.272x)
  with 0.901x the CPU per reply and 0.860x the p99 latency; at 16 connections
  it was 1.109x and the direct encoder used 0.892x the CPU. Small JSON was
  0.961x, and the model route's CPU and high-concurrency throughput
  qualification remains open. These measurements come from a shared host and
  do not establish the formal M0 performance gate.

### Fixed

- `request.form()` and form models no longer decode a multipart text field
  with the codec its part names. A part declaring `charset=punycode` was
  decoded by a quadratic pure-Python codec: 80 KB took 1.2 s and six
  concurrent 150 KB posts 26 s each, on the sync pool and past the request
  timeout. Text fields are now always read as UTF-8, and part headers are
  read with exactly the grammar of the native parser behind `Form()` and
  `File()`, in one linear pass, so the two cannot be made to see different
  requests: text inside a quoted value cannot rename a part or turn a field
  into a file. A part the native parser refuses (no `name`, a disposition
  other than `form-data`, a repeated parameter, a trailing `;`, a malformed
  `filename*`) is refused with the same `400` where it was skipped or
  accepted. `UploadFile.content_type` from `request.form()` is the part's
  `Content-Type` as sent, or `None`, rather than its lower-cased type
  defaulting to `text/plain`; a `name` on `Content-Type` no longer supplies
  a filename; `filename*` must declare UTF-8 and wins over `filename`; and
  RFC 2231 continuations are not joined. A generated test holds the two
  parsers to the same answer for thousands of part headers.
- A WebSocket session now ends when its client disconnects while the handler
  is behind on receiving. The reader stopped reading the socket once 64
  messages were waiting, so a handler that never receives, such as a push
  endpoint, was never told that a client which had sent 64 frames was gone:
  the handler, its socket and its connection slot were held until the worker
  restarted. The reader now keeps watching the socket through its existing
  16 KiB buffer while it waits, and a failed send ends the session too.
  Messages already buffered are still delivered in order to a handler that
  keeps receiving; one that does not sees the disconnect and is cancelled
  within about ten seconds.
- Requests waiting for their bodies without an admission slot are bounded on
  HTTP/2. A client could park 100 streams a connection by sending headers
  and no body: 391 KiB over 300 connections held about 210 MiB until the
  request timeout, whatever `max_in_flight` was. Each HTTP/2 connection now
  has 8 places of its own for streams waiting for a body and beyond them
  draws on a worker-wide pool of `max_connections`; a stream that can get
  neither is answered `503` with `Retry-After`, and only that stream. One
  client's parked streams cannot take another connection's places, so they
  cannot make other clients' uploads fail. HTTP/1.1 requests, one to a
  connection, take no place and are never refused, and a request whose body
  arrives with its head never waits. The same 300 connections now hold
  about 95 MiB.
- Adding `TrustedHostMiddleware`, `CORSMiddleware` or `GZipMiddleware` to an
  app that is already mounted raises `ValueError` instead of being accepted
  and then not enforced, and `build()` repeats the check over every mounted
  app, so a server-wide option set on a sub-app after `mount()` is refused
  rather than dropped. Set these on the outermost app.
- Bessemer's event loop polls for I/O at least every 200 µs while callbacks
  keep its ready queue busy. With one loop per worker it polled only about
  every 61st iteration, so one handler doing work in slices separated by
  `await asyncio.sleep(0)` delayed new connections, native health checks and
  asyncio readers by about 61 slices: 57 ms behind 1 ms slices and nearly a
  second behind 20 ms ones. They now wait one to three slices. Instructions
  per request are unchanged within 0.3%.
- Queued sync handlers using dependencies, Request injection or HTTP middleware
  no longer start after their request has timed out or its client has left.
  Both sync invocation paths check the closed reply channel before starting.
- Informational CI throughput summaries drain outstanding load, retain warmup
  and measured errors, and withhold comparisons for errored, client-limited or
  unavailable measurements. Successful throughput is recorded separately.
- `run_cpu` recovers when a process-pool child dies. A killed child broke
  the pool for good and every later job failed with `BrokenProcessPool`. The
  broken pool is discarded and a new one of the same size is built for the
  next job; only the jobs that were in it fail. The discarded pool is shut
  down outside the executor's lock, where a job submitted while its failures
  were being delivered could otherwise deadlock the worker.
- A server-sent event whose data holds a carriage return is sent as separate
  `data:` lines, as a line feed already was, instead of aborting the stream.
  `event`, `id` and `retry` still refuse line breaks.
- An in-process `TestClient` inside a supervised worker's child no longer
  has its listening socket closed underneath it. Whether the native server
  owns the listener was read from an environment variable every child of a
  worker inherits; the worker's entry point now says so explicitly.
- An urlencoded body with more than 1,000 fields is refused before it is
  decoded. A 1 MiB body of `a&` pairs allocated half a million pairs on the
  I/O thread, about 40 ms and 40 to 50 MiB, before its `400`.
- A multipart `filename*` that decodes to a control character is refused
  with `400` by the native and Python parsers. The RFC 5987 form could carry
  a NUL, CR or LF that a quoted `filename` cannot.
  `UploadFile.filename` is client-supplied and is never a path.
- The file parts of a spooled upload share their request's one descriptor.
  Each part held its own, so one 71 kB request with 1,000 one-byte files
  held 1,000 descriptors for as long as its handler ran, and with a limit of
  512 open files failed with `500`. A request now holds one however many
  parts it has. Reads, seeks, per-part positions and `close()` are
  unchanged.
- Refused uploads no longer force a full garbage collection about once a
  second. The collection that reclaims spool space from unreferenced uploads
  counts only what it freed, and backs off from one second up to 64 when it
  frees nothing.
- Bessemer's event loop keeps its ready queue, timers and registered readers
  visible to the garbage collector at all times. A collection that ran while
  an I/O thread held the loop's lock for an instant could take everything
  reachable only through the loop for garbage: scheduled callbacks were
  finalised, a pending task had `GeneratorExit` thrown into it and a watched
  socket was closed.
- The loop drops a reader or writer still registered for a descriptor that
  was closed, instead of re-arming it for the descriptor number's next
  owner. A pending `sock_recv` could read another connection's data and a
  pending `sock_sendall` write to it; those operations now fail with `EBADF`
  once their socket has been closed, or are never run.
- A descriptor left readable when `run_forever` stopped is seen when the
  loop runs again. With one loop per worker it was missed until some other
  event arrived, so a reader could wait two seconds and a transfer stall
  around shutdown.
- A reader or writer whose handle was cancelled directly is unregistered
  when its event arrives rather than polled for again and again, which spun
  the loop at up to a full core. A burst of `call_soon_threadsafe` calls
  writes one wake-up rather than one each (51 writes instead of 9,005 for
  50 bursts of 200), and a timer due within 10 ms no longer costs two
  `timerfd_settime` calls on every wake-up.
- With `io_backend="io_uring"`, a connection dropped off its ring's thread
  is left for the ring to close instead of having its descriptor closed
  under a receive still armed on it, completions held back by a full
  completion queue are collected before the ring sleeps, and a submission
  queue that stays full fails the ring instead of spinning the worker.
- `tests/security/fuzz_transport.py --wheel` runs against the current wheel
  again: its provenance check failed on `py.typed`, and its cookie property
  still expected the `400` for duplicate cookies that 1.0.0rc4 replaced. The
  sanitizer workflow also builds and runs the unsafe crates on free-threaded
  CPython 3.14t, so their `Py_GIL_DISABLED` paths are compiled and exercised
  under AddressSanitizer; ThreadSanitizer on that interpreter is reported
  but does not gate, because CPython itself is not instrumented.
- The native JSON encoders refuse a string holding a lone surrogate before
  converting it, and leave it to the general path. The conversion consults
  Python's codec error registry, where an application may have replaced the
  `strict` handler with Python code; run in the middle of an encode on
  CPython 3.13, that code could free what the encoder was reading and crash
  the worker. ASCII strings are now encoded in place on CPython 3.12 and
  3.13, about a fifth fewer instructions per string.
- A response `dict` that changes size while it is converted (by such a
  handler, or by another thread on free-threaded Python) is answered `500`.
  It panicked inside the dict iterator, which stopped the worker's event
  loop: every later request on that worker went unanswered until the
  supervisor replaced it.
- On CPython 3.13 the native model builder sets `__dict__` through the
  descriptor for a class with inline values, or for an object of another
  type returned by a model's `__new__`. Installing the dict directly left
  such an instance inconsistent and could crash. Pydantic models, which
  never have inline values there, keep the direct path.
- The free-threaded response encoder checks that a string-keyed `dict` can
  fit in the remaining response allowance before it copies it.
- A connection whose response could not be sent, or ran out of time being
  sent, is dropped at once. The server first tried to close it gracefully,
  which over TLS waited on the same stalled peer again, and the deadline's
  own wake-up counted as progress and restarted the clock: a client that
  stopped reading held its connection for 10 to 12 seconds against a 4
  second timeout, where a plaintext client held it for 4. Both now take 4.
- The Redis rate limiter remembers a client the store has told to wait and
  answers it `429` itself until the wait ends. It asked the store for every
  request, so one already limited client sending about 1,100 requests a
  second through a 5 ms store took every pooled connection and turned other
  clients' requests into `503` "Rate limiter is unavailable". Each worker
  remembers at most 4,096 such clients. A Redis error written to standard
  error has its control characters escaped.

## [1.0.0rc7] - 2026-11-15

Seventh release candidate for 1.0. It is 1.0.0rc6 with its adaptive admission
corrected and two older faults fixed: a sync handler that could run after its
request had been answered, and a refused request head whose answer Windows
could lose. CPU-bound sync handlers are no longer refused on a busy or a
paused host. A loop blocked in its handlers is refused whether or not other
threads of the process are at work, and stays marked for as long as it is
inside what made it late. A fixed `max_in_flight` no longer costs four system
calls a request. Two things change for an application that sets
nothing. A loop blocked by handlers that each run for longer than 20 ms now
refuses new requests throughout, where it refused them in gaps or, on the
Rust event loop, not at all. And a blocked loop beside threads that use
between a quarter and a half as much CPU as it spends blocked is no longer
refused, where 1.0.0rc6 refused it. The 1.x API freeze holds and nothing is
added to it. The evidence is in
[production readiness](docs/production-readiness.md).

### Fixed

- Adaptive admission no longer requires CPU-bound handler threads to receive
  half a host core before their work can explain a delayed loop. It compares
  their recorded CPU work with the loop's own work, avoiding false overload
  refusals when a busy host stretches GIL waits. Invalid clock samples still
  count as delay, and genuine loop overload, fixed limits and request deadlines
  remain bounded.
- A loop blocked in its handlers is refused while other threads of the
  process work. With the work of other threads judged by its amount alone,
  which is the entry above before it was released, any late request was
  excused once they had used 5 ms of CPU and twice the loop's: a thread of
  the application using a twentieth of a core was enough for a loop asleep in
  every handler never to be refused. The loop now also keeps the wall-clock
  time it spends running request handlers, and other threads excuse a wait
  only if they used at least a quarter as much CPU time as that, over the
  request's own wait and over the whole run of late requests it would end.
  CPU work added up across short samples must also have gone on through the
  request's own wait, not only before it. CPU-bound sync handlers are still not
  refused, on a busy or a paused host. What this does not see is listed under
  [adaptive request admission](docs/production-controls.md#adaptive-request-admission).
- A loop marked congested stays marked while it is inside the batch in which
  it last started a request late, and for four target intervals after that
  batch ends. The four intervals were counted from the late request itself,
  so under handlers that each block the loop for longer than they last
  (20 ms at the default timeout) the mark lapsed before the next request was
  started. Such a loop was refused only in the gaps, and on the Rust event
  loop a burst to it was not refused at all. Every release since 1.0.0rc2
  has this.
- A request started on time no longer ends a run of late requests when a
  late one was counted earlier in the same batch. A batch takes requests in
  the order they came, so its last ones may be on time only because they came
  just before it. With handlers that block after their first `await`, every
  batch could end that way, and a loop blocked nearly all the time was never
  refused.
- A sync handler waiting for a pool thread no longer starts after its request
  has been answered, by its deadline or because its client went. The event
  loop tells it of the cancellation when it next takes its batch, and a pool
  thread freed before then started the call anyway, so its work was done for
  nobody. The pool thread now asks whether anyone is still waiting before it
  starts the call. 1.0.0rc5 and 1.0.0rc6 both have this, on a busy host.
- A request head refused before it had ended, for more headers than allowed,
  is answered while the rest of it arrives. The connection closed straight
  after its error response, so what the client sent next reset it; on
  Windows the client could lose the `431` and see a reset instead. After an
  error response a connection now keeps reading what its client sends, for at
  most 500 ms and 64 KiB, until the client closes.
- A worker with an explicit `max_in_flight` no longer reads two CPU clocks
  for every request. A fixed cap has no delay target, so since 1.0.0rc6 each
  of its requests was late the moment it was queued, and took the readings a
  late request takes before being ignored: four `clock_gettime` system calls
  a request in 1.0.0rc6. Which requests were admitted was not affected.
- Requests queued before CPU tracking starts can use subsequent CPU readings
  to explain continued contention. Batch mark numbers are no longer reused
  after a healthy period.
- Queue delay is sampled when each request starts, without counting its own
  batch's handler execution again after the batch finishes.

## [1.0.0rc6] - 2026-11-13

Sixth release candidate for 1.0. CPU-bound sync handlers no longer make a
worker refuse requests as overloaded. On free-threaded Python `run_cpu` runs
its work in processes, as it does with the GIL, and model responses cost
less. The allowance 1.0.0rc5 makes for large request bodies now ends when a
collection runs. Two things change for an application that sets nothing: on
free-threaded Python `run_cpu` no longer uses threads, and under the default
admission CPU-bound work in sync handlers is no longer refused early. The
1.x API freeze holds and nothing is added to it. The evidence is in
[production readiness](docs/production-readiness.md).

### Changed

- **`run_cpu` runs its work in processes on free-threaded Python too.** An
  application that passes no `executor` got threads there up to 1.0.0rc5. In
  threads without the GIL a function is fast only while it leaves alone every
  object the threads share, and that is not something its code shows: a loop
  reading a module-level list ran at a quarter of the rate it reached in
  processes, and so did a loop using only local values and one integer
  constant when its module had been compiled from source instead of loaded
  from a bytecode cache. In processes both ran as they do with the GIL. An
  application that relied on threads there, for a function or argument that
  cannot be pickled, a context variable read in the job, or objects shared
  with the worker, passes `App(executor=ThreadPoolExecutor())`. Interpreters
  with the GIL already used processes. See
  [threads or processes](docs/cpu-and-gpu.md#threads-or-processes).
- Model responses cost less on free-threaded Python. The encoder took a
  reference to each field of a model through a separate dict lookup, and
  copied every dict returned in place of a model. It now reads either in one
  pass under the dict's lock when the fields are in their declared order,
  as they are in a model Pydantic built or a dict written in that order. A
  response of 256 four-field models took 14% fewer instructions on CPython
  3.14.6t, and the same response returned as dicts 28% fewer. Interpreters
  with the GIL are unchanged.
- With `gc_allowance="body"`, a server logs a warning at start-up when no
  allowance can be made because functions of the `gc` module were replaced
  before it started. A free-threaded build, where the setting is documented
  to have no effect, logs nothing.

### Fixed

- CPU-bound sync handlers no longer make a worker refuse requests as
  overloaded. Under the default `max_in_flight="auto"`, a request its event
  loop started late counted towards congestion whatever had delayed it. On an
  interpreter with the GIL, every thread running Python holds it for a switch
  interval, so a few CPU-bound sync handlers, `ThreadPoolExecutor` jobs or
  threads of the application kept an idle loop waiting, and every route of
  the worker answered `503` with "Server event loop is overloaded". Eight
  connections to one CPU-bound sync route were enough. On Linux and macOS a
  late request now counts only when the loop thread was itself busy, or the
  rest of the process was not; a loop behind on its own work, or blocked in a
  call, sheds as before. Work in sync handlers is bounded by `sync_workers`,
  the 4,096-request ceiling and `request_timeout`. Windows is unchanged. See
  [adaptive request admission](docs/production-controls.md#adaptive-request-admission).
- A large body's allowance ends when a collection runs, not only when its
  request does. 1.0.0rc5 kept `gc.get_threshold()[0]` raised until the request
  ended, although a collection had already promoted the body's objects and
  reset the count they were in, so unrelated cyclic garbage was collected
  later than the application's threshold asked for as long as such a request
  stayed open. Bessemer now sees collections through an entry it adds to
  `gc.callbacks`, after those already there; leave it in place.
- No allowance is made on a free-threaded build of Python whose GIL has been
  switched on (`PYTHON_GIL=1`, or an extension that re-enables it). 1.0.0rc5
  made one there. That build keeps its own collector, which does not start a
  collection on the young count alone, so the raised threshold did not do
  what it was for. With the GIL off nothing changes: no allowance was made.

## [1.0.0rc5] - 2026-11-11

Fifth release candidate for 1.0. Large typed request bodies cost less: the
objects a body is parsed into no longer start a garbage collection by
themselves, and the parser builds each model in fewer steps. On the
1,000-item typed echo used for comparisons, a request took about a quarter
fewer instructions than under 1.0.0rc4 (CPython 3.13, one worker; see
[production readiness](docs/production-readiness.md)). Nothing else in the
server has changed since 1.0.0rc4. The 1.x API freeze holds; `gc_allowance`
is an addition. The change to default behaviour is listed first.

### Changed

- **A large request body no longer starts a garbage collection by itself.**
  Parsing a body into models allocates thousands of container objects at
  once, enough to pass Python's young collection threshold, and the
  collection that followed walked all of them and could free none. While a
  request holds objects its body was parsed into, `gc.get_threshold()[0]` is
  now higher by their number, and it comes back down when the request ends.
  Everything else the application allocates meets the same budget as before.
  This applies to bodies of 4 KiB or more that Bessemer parses for a route's
  Pydantic model, on interpreters running with the GIL; free-threaded
  Python is unchanged. A threshold the application sets is kept as the base,
  and a collector switched off is left alone. See
  [large request bodies and the garbage collector](docs/application-api.md#large-request-bodies-and-the-garbage-collector);
  `gc_allowance="none"` restores the old behaviour.
- Request bodies parse with less work per model. The three Pydantic
  attributes of each instance are written through slot members found once
  per model, lists are created at their final length, one-character strings
  are the interpreter's own, and a decimal of up to fifteen digits is
  converted in the pass that reads it.

### Added

- `gc_allowance` (`--gc-allowance`, `BESSEMER_GC_ALLOWANCE`,
  `[tool.bessemer] gc_allowance`, `App.run(gc_allowance=)`): `body`, the
  default, or `none`. Process-wide.

## [1.0.0rc4] - 2026-11-10

Fourth release candidate for 1.0. Fixes from a whole-project review of
1.0.0rc3 and from two reviews of those fixes, limits on what a slow client
can hold, and a request path that does less work per request. The 1.x API
freeze holds; `App(strict_content_type=)` and `min_send_rate` are additions.
The changes to default behaviour are listed first.

### Changed

- A JSON body must say that it is JSON. A request body sent with no
  `Content-Type` to a route with a body parameter is refused with `415`, as
  one with the wrong type already was, instead of being read as JSON. A
  browser can post such a body to another site without a CORS preflight,
  and FastAPI refuses it by default for the same reason. Pass
  `App(strict_content_type=False)` to keep reading it. Routes that read
  `await request.body()` themselves are not affected.
- Cookies never refuse a request. A pair that is not well-formed RFC 6265 is
  left out and the first cookie with a name wins; before, one malformed or
  repeated cookie answered `400` on every route that reads cookies or takes
  a `Request`, and on every WebSocket handshake. A browser sends every
  cookie in scope, including those other applications set on the domain.
- Rate limits count an IPv6 client by its /64 rather than its full address.
- A WebSocket session keeps at most about `max_body_size` bytes of received
  messages, plus one message, waiting for its handler (and at most 64
  messages, as before); it then stops reading the socket until the handler
  consumes.
- A static mount serves at most `max_connections` responses at once per
  worker and answers `503` with `Retry-After` beyond that. HTTP/1.1 clients
  cannot reach the bound; it stops HTTP/2 streams from each holding a file
  open.
- A request that declares a `Content-Length` over `max_body_size` is refused
  with `413` at once, before `100 Continue`, instead of when the excess
  arrives.
- During shutdown new connections are refused immediately instead of
  waiting in the listen backlog until the drain ends.
- A request holds its `max_in_flight` slot for its work, not while the
  server waits for its body. A request that finds no slot free is still
  refused at once with `503`, before any of its body is read. One that is
  admitted gives the slot up while its body is outstanding and takes one
  again when the body is in, so it can now be answered `503` after sending
  its body if every slot has been taken meanwhile. A spooled upload does the
  same each time it waits for its client, but waits its turn for a slot
  rather than being refused part way. Before, a client that declared a body
  and sent none held a slot until the request timeout, and a handful of them
  made every Python route answer `503`.
- Uploads spooled to disk have a limit of their own. Each takes one of
  `max_in_flight` upload slots, separate from the slots requests run in,
  from its first byte until the last reader of its file is closed, and its
  disk bytes stay charged for as long. That includes a client that has
  paused and an `UploadFile` the application keeps after the request. A
  further upload is answered `503 Upload capacity is full`; other routes
  are not affected. Close an upload when you have finished with it. One
  that is dropped without being closed is freed with its last reference,
  and a refusal for capacity asks Python for a garbage collection (at most
  once a second, and less often while collections free nothing) so that
  readers left in a reference cycle do not hold capacity until an unrelated
  collection.
- A client must take a response it is behind on at a minimum rate, 240
  bytes a second by default (`min_send_rate`, below). The send deadline was
  only an idle timeout: a client that took a little, just inside each
  timeout, could hold a response and its share of the response byte budget
  for as long as it liked. Now, within an HTTP/1 response, the time writes
  spend blocked may not exceed the longest request timeout plus the time
  the bytes taken would need at that rate; past it the connection is
  closed. On HTTP/2 the same holds for each stream, and a WebSocket session
  over HTTP/1.1 is counted as one long response. Time the application
  spends producing a response is not counted. A consumer that deliberately
  reads a stream more slowly than that for longer than the timeout is now
  disconnected; set the rate to `0` for the old behaviour.
- A request that asks for a protocol upgrade the server does not perform is
  answered with `Connection: close`. Such a connection used to stay open
  for further requests.
- With the default sync handler pool, a burst of blocking calls can grow
  the pool after its last call has been submitted: a backlog that persists
  is rechecked on the pool's own clock, under the same CPU gate and thread
  ceiling. Before, the pool was only reconsidered when another call
  arrived, so a burst of twelve calls stayed at eight threads.
- A static or `FileResponse` file small enough to be compressed whole (up to
  1 MiB) is charged to the response byte budget while it is in memory, as
  other buffered responses are. When the budget cannot take it, it is
  compressed a chunk at a time and sent with chunked framing, as larger
  files already were, instead of being refused. Before, each such response
  held up to 1 MiB that no budget counted.
- Generated `X-Request-ID` values start with a prefix chosen at random per
  worker instead of the worker's start time and process id.
- A `SIGHUP` received during a rolling reload starts one more cycle when the
  current one ends instead of being dropped.
- `HTTPException` and request-validation errors keep their own responses
  when the application registers a handler for a base class such as
  `Exception` or `ValueError`: the 404 stays a 404 and an invalid body stays
  a 422, as in FastAPI. A handler for the exception's own class or status
  code applies as before, and a handler for `Exception` (or status 500)
  remains the catch-all for everything else.
- An exception that no handler takes is raised from `call_next` in
  `@app.middleware("http")`, and into ASGI middleware, after the route's
  dependencies have been torn down, as in Starlette; it used to arrive as a
  500 response. Code after `await call_next(request)` therefore no longer
  runs for such an error unless it is in `finally`, and a middleware that
  commits after `call_next` no longer commits a failed request. If no
  middleware answers it, the catch-all handler or the built-in 500 does.
- Error logging. A handled exception no longer writes an ERROR line (it
  wrote "Unhandled ..." even when a handler answered). An unhandled one
  writes the same line followed by where it was raised: each frame's file,
  line and function and the exception class names, including chained
  causes. Exception messages and source lines are still never logged.
- `Depends(obj.method)` written in several places is one dependency: it
  runs once per request and makes one `scope="app"` resource, as in
  FastAPI. Each occurrence used to be a separate dependency because a
  bound method is a new object every time it is read.
- `include_router()` refuses an `App` that carries exception handlers,
  middleware, mounted apps, dependency overrides or server options, which
  it used to drop silently, and points to `mount()`. An `App` with only
  routes and a lifespan is still included like a router.
- Assigning `JSONResponse.body` raises `AttributeError` (it was ignored),
  and `TestClient.cookies.set()` rejects a `domain` or `path` it cannot
  honour and unknown keywords (they were swallowed).
- `request.scope` no longer contains the internal `"bessemer.call"` entry
  once a request under ASGI middleware has finished.

- Requests cost less. Under Callgrind, against 1.0.0rc3 and including the
  fixes below, a request takes 8.5% fewer user-space instructions on `/json`
  and 7.7% fewer on `/items/{id}` with the default Rust event loop (32,505
  to 29,751 and 35,566 to 32,830), about 5% fewer with asyncio's loop, and
  about 5% fewer for a 20-item Pydantic body on either. Nothing changes on
  the wire. The savings come from resolving common header names in any
  letter case (clients and proxies send them Title-Case), remembering a
  connection's validated `Host`, borrowing rather than moving the request
  through dispatch, one stop-signal wait per connection, smaller queued
  jobs, and matching a model's next expected key in place. These are
  instruction counts, not throughput; the formal performance gate is still
  open.
- A response model returned as a `dict` whose keys are the model's fields
  in declaration order (up to sixteen) is serialised from one pass over the
  dict on GIL builds. Any other dict takes the checked path as before.

### Added

- `App(strict_content_type=True)`.
- `min_send_rate` (`--min-send-rate`, `BESSEMER_MIN_SEND_RATE`,
  `[tool.bessemer] min_send_rate`, `App.run(min_send_rate=)`): the bytes a
  second a client must take of a response it is behind on, once it has been
  behind for the longest request timeout. Default 240; `0` sets no floor.
- Linux x86_64 wheels for the regular CPython 3.14 build, tested with the
  same suite as the others.
- `py.typed`: the package's annotations are visible to type checkers.
- A warning at startup for a `BESSEMER_*` environment variable that sets
  nothing but is a slip from a setting (`BESSEMER_WORKER`), naming the
  setting it resembles.

### Fixed

- A sync handler, sync dependency or executor call that raises no longer
  leaves its error, its future and the waiting frame referring to each
  other. On GIL builds everything the failed call had in scope, an upload
  included, is freed at once instead of at the next garbage collection.
- Reading `Supervisor.bound_address` from another thread while the
  supervisor shut down could raise `OSError: Bad file descriptor`. It now
  reads `None` once the listener is closed.
- **Unbounded memory growth on CPython 3.12 and free-threaded 3.14.** Object
  keys parsed by `await request.json()` and `WebSocket.receive_json()` were
  interned, and interned strings are immortal on those interpreters, so a
  client sending fresh keys grew the worker for good (about 3.75 MiB per
  659 KiB request on 3.12). Keys are no longer interned.
- **A worker exited when it ran out of file descriptors.** Any `accept`
  error ended the server loop and dropped the requests in flight. The
  worker now keeps serving, retries with a growing pause and logs once.
- **An error in a lifespan's teardown stopped the whole service.** Exit
  status 78 was treated as a permanent configuration failure even from a
  worker that had been serving, so the first routine recycle of a worker
  whose teardown raised `AttributeError`, `TypeError`, `ValueError` or
  `ImportError` shut every worker down.
- **An open redirect on a static mount at `/`.** `GET //evil.example/%ff`
  was answered `307 Location: //evil.example/%ff/`. An undecodable path is
  now `404`, and no redirect is sent for a path a browser would read as
  protocol-relative (`//host` or `/\host`).
- A replacement worker that could not be spawned, or a killed worker that
  was slow to reap, no longer shuts the other workers down.
- `http.HTTPStatus` and other `IntEnum` status codes are accepted by
  `HTTPException`, the response classes, `RedirectResponse`, route
  `status_code=` and `exception_handler()`; they raised and so answered 500.
- A response built from another response's headers keeps repeated fields:
  `Response(headers=other.headers)` kept one of several `Set-Cookie`
  values, and so did an `HTTPException` with two `WWW-Authenticate` headers
  that went through the default handler.
- A `JSONResponse` subclass that overrides `render()` has its override
  called, and `JSONResponse(...).body` returns the encoded bytes rather
  than `b""`. (`render()` on other response classes is still not called.)
- An exception handler that returns a `StreamingResponse` has it delivered;
  it answered 500.
- A request under ASGI middleware is freed when it ends instead of waiting
  for the cycle collector.
- Windows: `bessemer run` from a virtual environment starts workers on the
  real interpreter rather than through the environment's launcher, whose
  child was not the supervisor's child. Not yet confirmed on Windows; the
  portability workflow now runs the Windows supervisor tests from a
  virtual environment.
- HTTP/1: a request refused for conflicting framing always gets its `400`
  (it was closed silently when its body contained two spaces); an HTTP/1.0
  keep-alive request for a body of unknown length is no longer told
  `connection: keep-alive` on a response that ends by closing; a streamed
  body that fails is reported to after-response work as not delivered.
- One unusable key in a fetched JWKS no longer rejects the whole set and
  stalls key rotation.
- The access-log writer no longer stops for good after one failed write.
- On the Rust event loop, `sock_recv` with an enormous size returns what is
  available instead of aborting the process, and a socket left in blocking
  mode no longer blocks the loop.
- Source installs and CI now use the same maturin (1.16.0).

## [1.0.0rc3] - 2026-11-09

Third release candidate for 1.0. The 1.x API freeze still holds. One default
changes: the size of the sync handler pool on Linux and macOS.

### Changed

- With `sync_workers` left at its default (`0`), a GIL build of CPython on
  Linux or macOS now starts up to eight sync handler threads as before and can
  grow the pool to sixteen. A thread is added only after four or more jobs have
  been queued for 10 ms and the process used less than 0.75 of a CPU core over
  the last 20 ms, at most one thread per 20 ms; extra threads stay parked until
  shutdown. Blocking handlers gain the most: a 1 ms blocking handler on one
  Linux worker now serves an offered 10,000 requests/s (p99 9 to 94 ms)
  instead of about 7,000/s (p99 about 1.15 s). The cost is about 8 to 10%
  more process CPU per request on that workload. CPU-bound `def` handlers do
  not grow the pool on an idle host and were within noise of the fixed pool
  (at most one extra thread and about 2% fewer requests under host CPU
  contention). Set `sync_workers=8` (`--sync-workers 8`,
  `BESSEMER_SYNC_WORKERS=8`) to keep the previous fixed pool. Windows,
  free-threaded builds and any explicit `sync_workers` value are unchanged.
- An HTTP/1 connection's read buffer starts at 2 KiB instead of 8 KiB and
  reuses its consumed space before growing, which cut idle keep-alive memory
  by about 31% per connection (18.3 to 12.5 KiB) in the PR #112 measurement.
  Large heads and bodies still grow the buffer in 8 KiB steps.
- Releases are cut by merging a version bump: a push to `main` whose package
  version has no tag yet builds, tests and (after approval) publishes it, then
  tags the commit and creates the GitHub release. See `docs/releasing.md`.

### Fixed

- The Callgrind CPU-clock probe in the performance workflow no longer writes
  a profile into the uploaded results artifact.
- The adaptive pool growth test allows five seconds instead of two, so a busy
  CI runner does not fail it.

## [1.0.0rc2] - 2026-11-09

Second release candidate for 1.0. The 1.x API freeze from 1.0.0rc1 still
holds; this candidate only adds to it. The default-behaviour changes below
are listed first because they can change what an existing app sees.

### Changed

- `max_in_flight` defaults to `"auto"` (`App.run`, `bessemer run
  --max-in-flight`, `BESSEMER_MAX_IN_FLIGHT`, `[tool.bessemer]`). Each worker
  admits up to 4,096 requests and sheds load (503) only while event-loop
  queueing delay stays above a target derived from `request_timeout`, instead
  of refusing everything above a fixed 128. Suspended handlers count against
  memory, not loop time, so a slow-upstream workload can hold many more open
  requests than before. An integer keeps the previous fixed-cap meaning.
- `redirect_slashes` is on by default, as in FastAPI: a request that differs
  from a route only by a trailing slash gets a `307` to the other spelling
  (keeping `root_path` and the query string) instead of a `404`. Set
  `redirect_slashes=False` on the app or router to keep the old answer.
- `BackgroundTasks` run after the response has been delivered, as in FastAPI,
  and the request keeps its admission slot until they finish.
- JSON responses keep `dict` insertion order and Pydantic field declaration
  order, and validation errors list `type`, `loc`, `msg` in FastAPI's order.
- Once a handler has called `Request.receive()` or `is_disconnected()`, a
  client disconnect sets the disconnected flag and the request is cancelled
  one second later, so the handler can react first. Handlers that never look
  are cancelled at once, as before.
- Plain `def` handlers run on a native thread pool, with arguments bound and
  results encoded natively. ContextVars, `current_request`, cancellation
  before start, capacity accounting and exception handlers behave as before.
  Pool threads start as work needs them and are named from the executor's
  prefix (`bessemer-handler-N`, `bessemer-cpu-N`); they are not
  `threading.Thread` objects.
- The default event loop is now `auto`. On Linux and macOS, with one loop per
  worker (`loops=1`) and the native loop (`bessemer._loop`) importable, workers run
  Bessemer's Rust event loop and serve connections on its thread; in every
  other case (Windows, `loops` above 1, no native loop) they run asyncio's
  loop, silently. Set `event_loop="asyncio"`, `--event-loop asyncio`,
  `BESSEMER_EVENT_LOOP=asyncio` or `event_loop = "asyncio"` under
  `[tool.bessemer]` to keep the previous behaviour. `BESSEMER_EVENT_LOOP`,
  `[tool.bessemer]` and `--event-loop` reach only `bessemer run`; `app.run()`
  takes the `event_loop=` argument, and `TestClient` needs
  `server_options={"event_loop": "asyncio"}`. `event_loop="bessemer"`
  behaves as before, including the warning and asyncio fallback off Linux and
  macOS. This supersedes the 1.0.0rc1 note that the Rust loop is opt-in and
  off by default; its setting name and the `asyncio` escape hatch stay
  stable. `bessemer run` now logs the resolved loop on its `listening` line.
  Handles returned by the Rust loop's `call_soon`, `call_later` and `call_at`
  are not `asyncio.Handle` or `asyncio.TimerHandle` instances (as with
  uvloop); code that checks for those types needs `event_loop="asyncio"`.
- `io_backend="io_uring"` now takes effect without also setting
  `event_loop="bessemer"`, because the default loop resolves to it on Linux.

### Added

- FastAPI parity: 233 of FastAPI's 258 `docs_src` tutorial modules now build
  with only the import changed (the rest fail loudly at startup; see
  `docs/fastapi-parity.md`). New or completed:
  - `ResponseValidationError`, and `OAuth2`, `OpenIdConnect`, `HTTPDigest`
    and `SecurityBase` security classes;
  - `pydantic.Field` metadata and options on parameters, Pydantic models as
    `Query()`/`Header()`/`Cookie()`/`Form()` parameters, Pydantic functional
    validators in `Annotated`, `time`, `timedelta` and `Decimal` parameters,
    `strict=True`, and the `response: Response` parameter;
  - a native `FileResponse` with byte ranges and conditional requests;
  - `redirect_slashes`, `root_path` and `root_path_in_servers`;
  - `Request.receive()` and `Request.is_disconnected()`;
  - `WebSocket.accept(headers=)`, raw ASGI WebSocket messages and
    `WebSocketState`;
  - `app.router`, router lifespans, `bessemer.exception_handlers` and the
    `bessemer.middleware.cors`/`gzip`/`trustedhost`/`httpsredirect`
    submodules with `HTTPSRedirectMiddleware`.
- `max_in_flight` accepts `"auto"`.
- `benchmarks/compare/` gains a reproducible FastAPI feature matrix, a
  Bessemer/FastAPI/Axum concurrency runner, a CPU-isolation helper and a
  summariser.
- `event_loop` accepts `"auto"` (`App.run`, `bessemer run --event-loop`,
  `BESSEMER_EVENT_LOOP`, `[tool.bessemer]`).
- With `loop.set_debug(True)`, the Rust event loop logs callbacks that run for
  `loop.slow_callback_duration` or longer on asyncio's logger, as asyncio's loop
  does (`Executing <Handle ...> took 0.123 seconds`). Its handles' `repr` now
  ends with `created at file:line` in debug mode, as asyncio's does.

### Fixed

- `redirect_slashes` never answers `//host`-style paths with a
  protocol-relative `Location` (an open redirect found in review).
- An explicit JSON `null` for an optional body with functional validators
  (for example `Annotated[int | None, Body(), AfterValidator(check)]`) runs
  the validators; an absent body keeps its default without running them.
- `HTTPSRedirectMiddleware` keeps `root_path` in its `Location`, including
  for routes of a mounted application.
- A buffered request body of alternating large and tiny frames no longer
  reserves several times the memory the request byte budget charged for it.
- A garbage-collected executor's native thread pool is released once its
  threads have exited.
- A `HEAD` request to a route whose `GET` response is JSON or bytes now gets
  the `Content-Length` the `GET` response has, instead of `0`. When native
  compression could change that length, HEAD omits it without compressing
  or iterating the body.
- Models with custom construction or finalization hooks use Pydantic's
  JSON-validation path, so malformed JSON cannot invoke those hooks.
- The lazy `EventLoopPolicy` class retains its pickleable module identity.
- The archived final-pass benchmark tables now count completed responses when
  calculating response rates and CPU cost per response.
- The CI performance workflow now renders throughput rows from its benchmark
  result JSON.

### Notes

- Synchronous routes do not run in anyio worker threads, on either loop, so
  `anyio.from_thread.run()` and `anyio.from_thread.run_sync()` called from a
  `def` route need an explicit token: save `anyio.lowlevel.current_token()` in
  async code and pass `token=`. This is documented in the application API
  guide; behaviour is unchanged.

## [1.0.0rc1] - 2026-11-06

Release candidate for 1.0. The public API below is frozen for the 1.x series:
later 1.x releases only add to it, and breaking changes wait for 2.0.

### API stability

- Stable: every name in `bessemer.__all__`; the public, non-underscore
  submodules that mirror FastAPI and Starlette (`bessemer.security`,
  `bessemer.status`, `bessemer.testclient`, `bessemer.staticfiles`,
  `bessemer.encoders`, `bessemer.responses`, `bessemer.websockets` and the
  like); the `bessemer run` and `bessemer routes` CLI flags; `BESSEMER_*`
  environment settings and `[tool.bessemer]` keys; and documented response,
  error and OpenAPI shapes.
- Experimental, outside the guarantee: the Rust event loop
  (`event_loop="bessemer"`) and the io_uring backend
  (`io_backend="io_uring"`). Both are opt-in and off by default. Their setting
  names are stable, but their behaviour and fallbacks may change in 1.x.
- Not covered: `bessemer._native`, `bessemer._loop` and other
  underscore-prefixed modules, worker and supervisor internals, Prometheus
  metric names, access-log fields, and the M0 benchmark binary.

### Added

- FastAPI-compatible API, with a [parity assessment](docs/fastapi-parity.md)
  covering every public FastAPI name. This includes:
  - `FastAPI` (an alias of `App`), `APIRouter`, route names with
    `url_path_for`, and the `response_model_*` options;
  - Starlette path converters and `Body(embed=True)`;
  - security dependencies with `Security()` scopes and `SecurityScopes`;
  - `status`, `jsonable_encoder` and `@app.middleware("http")`;
  - mounted Bessemer and ASGI apps, standard ASGI middleware and lifespan
    state.

  Anything that is not supported fails at startup.
- `TestClient` over the real native server, including WebSocket sessions,
  and `dependency_overrides`.
- Forms and file uploads (`Form`, `File`, `UploadFile`), decoded natively.
  Large multipart uploads spool to disk.
- WebSockets, implemented natively: `WebSocket`, `WebSocketDisconnect` and
  `WebSocketException`. Optional `permessage-deflate` is supported, and
  WebSockets also work over HTTP/2.
- Streaming responses and server-sent events (`StreamingResponse`,
  `EventSourceResponse`).
- HTTP/2 alongside HTTP/1.1, with h2spec conformance checked in CI.
- Native TLS through rustls. This covers mutual TLS, client certificate
  revocation lists, OCSP stapling, and reloading rotated certificates without
  a restart.
- Native gzip and Brotli response compression (`Compression`) and static
  files with conditional, range and precompressed responses.
- Native JWT bearer authentication: `JWTAuth`, with static or
  auto-refreshing remote JWKS, single- and multi-tenant Microsoft Entra ID
  presets, `Principal` and `require_scope`.
- OpenTelemetry tracing (`OpenTelemetry`): W3C Trace Context propagation and
  native OTLP/HTTP span export.
- Rate limits shared across workers and replicas through Redis.
- `app.run(workers=...)` serves through the same process supervisor as
  `bessemer run`: 0 for one worker per usable CPU, or 2 to 256. The default
  stays at 1, serving from the calling process. Workers import the application
  by module and variable name, so the call belongs under
  `if __name__ == "__main__":`; an unguarded call fails with an error that
  says so.
- Several asyncio event loops per worker (`loops`), and supported free-threaded
  CPython 3.14t, where sync handlers scale across cores in one worker.
- Experimental, opt-in: a Rust asyncio event loop
  (`event_loop="bessemer"`, Linux), and io_uring socket I/O on it
  (`io_backend="io_uring"`). io_uring falls back to epoll when the kernel or
  seccomp policy refuses it.
- Tag-driven release workflow: an sdist plus manylinux 2.28 x86_64 wheels for
  CPython 3.12, 3.13 and 3.14t. Each wheel is re-tested as installed, then
  published through PyPI Trusted Publishing with attestations, and a GitHub
  release is created with checksums. Running the workflow by hand is a dry
  run that stops before publishing.
- `scripts/release_version.py`, which fails when any version declaration, the
  tag or the changelog disagree.
- Licensed under MIT OR Apache-2.0, with both licence files shipped in the
  wheel and sdist.
- `SECURITY.md` with private vulnerability reporting, release docs, and PyPI
  package metadata.
- macOS (Apple Silicon and Intel) and Windows x86_64 wheels for CPython 3.12
  and 3.13, each built and tested with the Python suite before release. The
  supervisor runs several workers on macOS and Windows as on Linux. On
  Windows, workers share the listening socket through an explicit handle list,
  each runs one I/O thread, and each runs in a job object so a crashed
  supervisor takes its workers with it. The supervisor stops a Windows worker
  gracefully by closing its standard input, so graceful stop works without a
  console, under a service or `pythonw`.

### Changed

- Much faster request path:
  - Requests are routed natively and started on the event-loop thread from
    a native batched queue.
  - Plain Pydantic models are built from JSON bodies and encoded natively.
  - JSON goes directly between Python objects and bytes through the
    `bessemer-codec` crate.
  - Fixed per-request work and allocations have been cut throughout.

  On a 4-vCPU development VM, one core served `/json` at about 99k req/s by
  default and 116k on the Rust loop. FastAPI on uvicorn served 11k, and Axum
  115k ([comparison](benchmarks/results/2026-11-05/compare-current-main/README.md)).
  These are same-host figures, not a formal benchmark.
- Ordinary HTTP/1.1 requests use a purpose-built connection loop instead of
  hyper's, with the same limits, statuses and keep-alive rules. A request with
  both `Transfer-Encoding` and `Content-Length` is now refused with 400.
- The native extension uses mimalloc as its Rust allocator, and release
  builds use fat LTO with one codegen unit.
- The supervisor promotes a replacement worker only after its control channel
  is confirmed. It also no longer counts a normal exit that races its pipe
  closing as a failure.
- Development status is now Beta. Qualification evidence is no longer shipped
  in the source distribution (it stays in the repository).

### Security

- Entra JWT authentication requires delegated scopes or an app-only token with
  `idtyp=app` and roles. Configure the `idtyp` optional access-token claim for
  app-only clients. Key sets without `kid` are limited to eight signing keys.
- WebSocket handshakes with `Origin` require an explicit CORS origin allowlist;
  a wildcard does not allow them. Duplicate credential headers are rejected.
- Stalled HTTP/2 responses close their connection after the send timeout. Static
  file access uses pinned directory handles and rejects non-regular files.
- The experimental Rust event loop snapshots mutable send buffers. Dependency
  auditing runs in CI and weekly, with Dependabot updates enabled.
- A response write that makes no progress for as long as the longest request
  timeout now closes its connection, on HTTP/1.1, HTTP/2 and upgraded
  connections. Before, a client that stopped reading held its connection slot
  and its share of the response byte budget for as long as it liked, so a few
  hundred such connections could make a worker refuse everyone else.
- Chunked request bodies with a control character (such as a bare CR) in a
  chunk extension or trailer field are refused with an error instead of the
  extension being ignored, so intermediaries that read a bare CR as a line end
  cannot disagree with Bessemer about where the chunk ends.
- The development and CI pin of `opentelemetry-api` moves to 1.45.1
  (CVE-2026-114603, quadratic `tracestate` parsing). Bessemer itself only
  passes `tracestate` values of up to 512 bytes to the API, so it was not
  exposed through its own propagation.

### Known limitations

These are tracked in [production readiness](docs/production-readiness.md) and
do not block the 1.0 API freeze:

- Formal performance acceptance (gate 1) needs a dedicated load-generator
  host and has not been run. Every published number is a same-host
  diagnostic.
- Formal qualification (soak, latency and cleanup evidence) covers Linux
  x86_64 only. macOS and Windows pass the test suite but are not qualified.
  Linux arm64 wheels are not built, and free-threaded 3.14t wheels are
  Linux-only.
- `--max-rss` is Linux-only, and SIGHUP rolling reloads are POSIX-only, so
  Windows has neither. The strongest process cleanup guarantees (pidfd, parent-death signals,
  subreaper) are Linux-only.
- Native I/O clients, an extension ABI, OTLP metrics and logs, gRPC export,
  Redis Cluster/Sentinel and a distributed cache are not implemented.
  [FastAPI parity](docs/fastapi-parity.md) lists the remaining gaps.

## [0.2.0a3] - 2026-11-04

First supervised application runtime. Python application routes run on the
Tier 2 bridge; the Tier 1 fast path and formal performance acceptance are open.

### Added

- Installable Rust/Python wheel with validated configuration and a supervised
  `bessemer` CLI (`run`, `routes`), resolving CLI flags, `BESSEMER_*`
  environment variables and `[tool.bessemer]` settings.
- `App`/`Router` registration with startup route and signature compilation:
  typed path, query, header, cookie and body inputs with constraints,
  dependency DAGs with per-request caching, app-scoped resources, sync, async
  and yield dependencies, and lifespan.
- JSON, text, HTML and redirect responses, repeated cookies, response models,
  filtered serialization, exception handlers, `Request`, state and
  request-scoped background tasks. Optional Pydantic 2.10.6+ validation.
- OpenAPI 3.1 generated once at startup, served natively, with optional docs.
- Native routing, JSON parsing and serialization, body and response limits,
  per-worker aggregate payload budgets, admission control, absolute processing
  deadlines and cancellation of queued sync work.
- Bounded HTTP/1 transport: connection, header-read, header count and buffer,
  and request-target limits with strict Host and framing checks.
- Re-exec worker supervisor sharing one listener, with readiness and event-loop
  heartbeats, cgroup CPU sizing, crash backoff, rolling reload, request,
  lifetime and RSS recycling, and TERM/drain/KILL shutdown with Linux
  process-group cleanup.
- Native CORS, environment-sourced API keys, allowed hosts, trusted proxy
  client IPs, rate limits, request IDs, security headers, protected Prometheus
  metrics and redacted access logs.
- Container image on pinned Debian Trixie with verified Tini as PID 1,
  qualified at 512 MiB and 256 MiB, plus an authenticated production example.
- Qualification tooling: installed-wheel HTTP and process-failure tests,
  seeded generated-input campaigns, extended soak and security scans.

### Known limitations

- Formal M0 performance acceptance is not met; local measurements were
  load-generator constrained and outside the 3% spread ceiling.
- The supervisor is POSIX-only; only Linux x86_64 with CPython 3.12 and 3.13
  is qualified.
- JWT/JWKS, distributed rate limiting, compression, streaming, WebSockets and
  OpenTelemetry are not implemented.

## [0.1.0] - 2026-11-02

### Added

- M0 performance spike comparing an Axum JSON handler with a Rust-driven
  Python async handler, plus the paired benchmark harness.
