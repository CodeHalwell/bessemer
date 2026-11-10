# Native production controls

Controls run in Rust around application routes, native health, documentation and
fallback responses. Their rejections do not call Python. Configure them on
`App`; invalid settings fail at startup. Authentication, CORS, rate limiting,
access logging and the metrics endpoint are opt in. Request IDs and the security
headers described below are enabled by default.

```python
from bessemer import App, CORS

app = App(
    cors=CORS(
        origins=["https://app.example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    ),
    api_key={
        "env": "SERVICE_API_KEY",
        "header": "X-API-Key",
        "exclude_paths": ["/healthz"],
    },
    rate_limit={
        "requests": 60,
        "window_seconds": 60,
        "max_keys": 10_000,
        "exclude_paths": ["/healthz"],
    },
    trusted_proxies=["127.0.0.1/32"],
    allowed_hosts=["api.example.com", "127.0.0.1"],
    access_log=True,
    metrics_path="/metrics",
    metrics_token_env="SERVICE_METRICS_TOKEN",
)
app.health("/healthz")
```

Supply both environment variables through the deployment's secret store before
starting this example. Secret values must contain 16–4096 visible ASCII
characters without spaces. Configuration, route exports and OpenAPI contain
environment variable names, never resolved values. Missing or invalid values
prevent the listener from starting; errors identify the variable without
including its value. Rotate a key by replacing the secret and restarting the
workers. This implementation supports one API key per application; bearer JWTs
are described in [JWT bearer authentication](#jwt-bearer-authentication).

Run metrics-enabled applications with one worker:

```sh
bessemer run my_service:app --workers 1
```

## Adaptive request admission

`max_in_flight="auto"` is the default for `App.run`, the CLI and project settings.
Auto admission allows up to 4,096 outstanding requests per worker, including
healthy suspended handlers. Each Python loop observes request queue delay and
uninterrupted invocation batches. Delay above one tenth of `request_timeout`
(clamped to 0.1–5 ms) persisting over twenty target intervals marks that loop congested for four
target intervals;
new requests receive `503` with `Retry-After` when all loops are congested.
This is a reactive delay signal, not a guarantee on latency or CPU utilization.
Already admitted requests finish normally; response and upload byte budgets
continue to apply.

An explicit positive integer keeps the fixed-cap behavior, without delay-based
shedding: `app.run(max_in_flight=128)`, `--max-in-flight 128` (alias
`--backpressure`), `BESSEMER_MAX_IN_FLIGHT=128`, or `[tool.bessemer]`
`max_in_flight = 128`. Each also accepts `auto` (quoted in TOML).
Capacity remains held through uploads, cancellation cleanup, streaming and
post-delivery background tasks. The in-flight metric tracks that same lifetime.
Native health remains independent of Python loop congestion.

## Authentication and public paths

The API key is sent as the complete value of the configured request header.
Comparisons use constant-time byte comparison. Missing or wrong keys return
generic `401` responses with `WWW-Authenticate: ApiKey` and `Cache-Control:
no-store`. Duplicate key headers return `400`. Framing and request metadata
header names, including `Host`, `Origin`, `X-Request-ID` and `X-Forwarded-*`,
cannot be used as the API-key header.

Only exact paths listed in `exclude_paths` bypass API-key authentication.
Health, documentation, HEAD requests and unknown paths do not receive automatic
exemptions. Give public health probes an explicit exemption as in the example.
Query strings do not affect path matching. Exemptions cannot contain wildcards,
path parameters, queries or fragments. Configured exact paths use ASCII URI
text; percent-encode non-ASCII characters consistently with declared routes.

`metrics_path` requires either `api_key` or `metrics_token_env`. With a separate
metrics token, scrapes must send `Authorization: Bearer <token>`; the API key
does not grant access to metrics. Without a separate token, the configured
API-key header protects metrics. API-key `exclude_paths` never makes metrics
public. Credentials in query parameters are not accepted. Metrics supports GET
and HEAD; authenticated requests with other methods receive `405`.

## JWT bearer authentication

`App(jwt=JWTAuth(...))` verifies `Authorization: Bearer <token>` in Rust before
any Python runs. Signature, `exp`, `nbf`, `iss` and `aud` are checked natively;
missing tokens return `401` with `WWW-Authenticate: Bearer`, and invalid ones
add `error="invalid_token"`. Handlers and dependencies receive the verified
claims by annotating a parameter with `Principal`; `require_scope(...)` is a
dependency that returns `403` unless every named scope is present in the
`scope`, `scp` or `roles` claims.

```python
from typing import Annotated
from bessemer import App, Depends, JWTAuth, Principal, require_scope

app = App(jwt=JWTAuth(
    algorithms=["RS256"],
    jwks_url="https://login.example.com/.well-known/jwks.json",
    # or jwks_file="/etc/service/jwks.json", or jwks_env="SERVICE_JWKS"
    issuer="https://login.example.com/",
    audience="api://items",
    exclude_paths=["/healthz"],
))

@app.get("/me")
async def me(user: Principal):
    return {"sub": user.subject}

@app.post("/items")
async def create(user: Annotated[Principal, Depends(require_scope("items.write"))]):
    ...
```

| Setting | Default | Notes |
| --- | --- | --- |
| `algorithms` | required | `HS256/384/512`, `RS256/384/512`, `PS256/384/512`, `ES256`, `ES384`, `EdDSA`. `none` is never accepted. |
| `secret_env` | — | Required for, and only for, HMAC algorithms; at least 32 bytes. |
| `jwks_env` / `jwks_file` / `jwks_url` | — | One is required for asymmetric algorithms; a JSON Web Key Set of at most 1 MiB and 256 keys. |
| `jwks_refresh_seconds` | 300 | With `jwks_url`: how often the set is refetched, 30 to 86400. |
| `jwks_ca_file` | system roots | With an `https` `jwks_url`: a PEM bundle of CAs to trust instead, for private identity providers. |
| `issuer`, `audience` | unchecked | A string or a list of accepted values (up to 1024 issuers and 64 audiences). Set both in production. |
| `leeway_seconds` | 30 | Clock skew applied to `exp` and `nbf`, at most 300. |
| `require_exp` | `true` | Tokens without `exp` are rejected unless disabled. |
| `exclude_paths` | `[]` | Exact public paths. A valid token there still yields a `Principal`; an invalid one is ignored. |
| `cache_size` | 4096 | Verified tokens kept until expiry (at most 300 s); `0` disables the cache. |
| `entra_access_token` | `false` | Set by `JWTAuth.entra(...)`: require delegated `scp`, or app-only `idtyp=app` with nonempty `roles`. Generic JWT validation leaves this off. |

Each key is bound to an algorithm allow-list: a JWK's own `alg`, otherwise its
key type (`RSA` keys accept the configured RS/PS algorithms, `P-256` only
`ES256`, `P-384` only `ES384`, `Ed25519` only `EdDSA`). Encryption keys,
symmetric JWKs and unknown key types are ignored, so a public key can never be
used as an HMAC secret. Tokens with a `crit` header, more than three segments,
non-canonical base64url or more than 8 KiB are rejected. When the key set uses
`kid`s, a token must name a matching `kid`. A key set may contain at most eight
signing keys without `kid`; larger sets fail to load.

Signature checks dominate the cost of an uncached verification (roughly 21 µs
for RS256 and 55 µs for ES256 on the development machine); a verified-cache hit
costs under 0.2 µs. The cache is keyed by the full token and re-checks `exp`
on every hit. Static keys (`jwks_env`, `jwks_file`) load once at startup:
rotate them by updating the key set and restarting workers.

### Remote key sets

With `jwks_url`, each worker runs a small native refresher thread that fetches
the key set over HTTPS (verified against the system roots, or `jwks_ca_file`)
and swaps it in atomically. Plain `http` is accepted only for loopback hosts.
Verification never waits on the network:

- The first fetch is awaited for up to 5 seconds at startup. If it fails, the
  worker starts anyway, rejects tokens with `401`, logs the failure and retries
  with backoff (1 s, 2 s, 4 s ... up to the refresh interval).
- The set is refetched every `jwks_refresh_seconds`. A failed refresh keeps the
  current keys; an unchanged document is not republished, so the verified-token
  cache stays warm. A valid set with no usable signing key is published like any
  other, so revoking every key makes verification fail closed.
- A fetched set is used key by key. A key that cannot be used (an RSA modulus
  under 2048 bits, a malformed member, a `kid` over 256 bytes) is left out and
  logged, and the rest of the set still rotates in. Limits on the set as a
  whole (256 keys, eight signing keys without `kid`) still reject the fetch.
  A static `jwks` in the configuration is stricter: any unusable key there is
  a startup error.
- A token naming a `kid` that is not in the set triggers an early refetch, at
  most once every 5 seconds, so a newly rotated signing key is accepted within
  moments without a restart. That first token is still rejected.
- When the published set changes, verifications cached against the old set are
  discarded, so a retired key stops being honoured at once.

Outbound proxies (`HTTPS_PROXY`) are not used for the fetch. Native per-route
scope checks are not implemented yet. With both `api_key` and `jwt` configured,
a request must satisfy both.

### Microsoft Entra ID

`JWTAuth.entra(...)` fills in the issuer, key set URL and algorithm for access
tokens from Entra ID tenants:

```python
app = App(jwt=JWTAuth.entra(
    "72f988bf-86f1-41af-91ab-2d7cd011db47",          # directory (tenant) ID
    audience=["api://items", "<client-id>"],         # Application ID URI and/or client ID
    exclude_paths=["/healthz"],
))
```

A multi-tenant API lists every tenant it accepts tokens from. Each listed tenant
gets its own issuer, so a token from an unlisted tenant is rejected even though
Entra signs it with the same keys; the keys are read from the shared `common`
endpoint:

```python
app = App(jwt=JWTAuth.entra(
    ["72f988bf-86f1-41af-91ab-2d7cd011db47", "<customer-tenant-id>"],
    audience="<client-id>",
))
```

| Argument | Default | Notes |
| --- | --- | --- |
| `tenant_id` | required | A tenant GUID, or a list of 1 to 1024 tenant GUIDs for a multi-tenant API. Duplicates are ignored. Domain names and the aliases `common`, `organizations` and `consumers` are rejected, because the token issuer embeds the GUID and an alias would trust every tenant. |
| `audience` | required | The API's client ID (v2 tokens) and/or Application ID URI (v1 tokens). |
| `version` | `2` | The access token version your app registration issues (`accessTokenAcceptedVersion`). `1` uses the `sts.windows.net` issuer and is available in the `global` and `china` clouds. |
| `cloud` | `global` | `global` (`login.microsoftonline.com`), `usgov` (`login.microsoftonline.us`) or `china` (`login.chinacloudapi.cn`). |
| `exclude_paths`, `leeway_seconds`, `jwks_refresh_seconds`, `cache_size` | as above | Passed through. |

Tokens are RS256 and keys rotate automatically through the remote key set.
Delegated permissions arrive in `scp` and app roles in `roles`, and both count
for `require_scope(...)`. Tokens minted for Microsoft Graph cannot be verified
by a third-party API by design; request a token for your own API's audience.
The Entra preset additionally rejects tokens without a nonempty delegated `scp`
claim or an app-only `idtyp=app` claim with nonempty `roles`. This rejects ID
tokens in Microsoft's documented format and roleless app-only tokens. To accept
app-only tokens, configure the [`idtyp` optional claim](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims-reference)
on the API registration's **access tokens** and assign an application role.
Delegated tokens with `scp` work without that optional claim. The generic
`JWTAuth(...)` verifier does not impose this Entra profile.
Use separate registrations for a browser client and the API; Entra ID tokens
and v2 access tokens can otherwise share the same client ID audience.
Accepting any tenant without a list is not supported: it would need a check
that each token's issuer matches its own `tid` claim plus an application-level
tenant decision, and a list keeps that decision explicit. Issuer lists are
checked with a hash lookup, so a long tenant list costs nothing per request.

## CORS

`cors` accepts `CORS(...)` or a dictionary with the same fields. Origins are
exact serialized HTTP(S) origins with no trailing slash, path, user information,
query or fragment. The string `null` can be allowed explicitly. The wildcard
`*` must be the only origin and cannot be combined with credentials.

Allowed preflights complete natively with `204` before API-key authentication,
including preflights for an unknown application path. They still consume the
configured rate quota. Disallowed origins, methods and request headers receive
`403`; malformed or duplicate singleton CORS headers receive `400`. Actual
requests with a disallowed Origin also receive `403`. Requests without Origin
proceed normally. Allowed origins receive CORS headers on authentication and
rate-limit errors too. Existing `Vary` values are retained.

Supported methods are GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. A lone
`*` in `allow_methods` enables all of them. Safelisted request header names
`Accept`, `Accept-Language`, `Content-Language` and `Content-Type` are always
allowed in a preflight; additional names require `allow_headers`. A lone `*`
in `allow_headers` accepts valid requested names and returns those names
explicitly. Configure `expose_headers` for response headers browser clients
need to read. Credentialed policies cannot expose a wildcard. `max_age` is an
integer from 0 to 86400 seconds.

## Response compression

`compression` accepts `Compression(...)`, `True` (gzip and Brotli with the
defaults) or a dictionary with the same fields. It is off by default.

```python
from bessemer import App, Compression

app = App(compression=Compression(gzip=True, br=True, min_size=1024))
```

The coding is negotiated in Rust from `Accept-Encoding`, honouring quality
values and `*`. Brotli is preferred when both are acceptable at equal quality;
`identity` or no header leaves the response uncompressed. Brotli uses quality
4 and gzip level 6, which suit dynamic responses. Only text-like media types
are compressed: `text/*`, JSON, JavaScript, XML, `+json` and `+xml` types, SVG,
WebAssembly and uncompressed fonts. Images, archives and
`application/octet-stream` pass through untouched.

A response is left alone when it is a HEAD response, a 1xx, 204, 206 or 304,
already has `Content-Encoding` or `Content-Range`, or carries
`Cache-Control: no-transform`. Buffered bodies shorter than `min_size` bytes
are not compressed, and a compressed body is only used when it is smaller.
Compressed responses get a new `Content-Length`, lose `Accept-Ranges`, and
have a strong `ETag` weakened to `W/"..."`. Every compressible response,
compressed or not, carries `Vary: Accept-Encoding` so shared caches keep the
variants apart.

Streaming responses and server-sent events are compressed frame by frame with
a sync flush after each frame, so each event still reaches the client as soon
as it is sent. Bodies over 64 KiB are compressed on the blocking thread pool
rather than the I/O threads. Compressed bytes keep the original bytes' share
of the response byte budget until they are released. zstd and `deflate` are not
offered.

## HTTP/2 response progress

HTTP/2 response data is tracked in 16 KiB chunks until the transport releases
it. If a response makes no chunk progress for the send timeout, Bessemer closes
the entire HTTP/2 connection, ending its other streams too. This also covers a
client that reads the socket but withholds stream `WINDOW_UPDATE` frames. The
send timeout is the longest configured request or route timeout; it is not a
separate setting. Waiting for the next event in a streaming response does not
start this timer. A link that takes longer than the timeout to deliver one
16 KiB chunk can be closed even if bytes are moving. HTTP/1.1 does not have
stream flow control and retains its socket write-progress timeout.

## Proxy trust and rate limits

`allowed_hosts` optionally restricts the HTTP Host independently of proxy
trust. `None` leaves hosts unrestricted; an empty list rejects every host.
Entries are exact DNS names, IPv4/IPv6 literals or `*.example.com` DNS suffixes.
A suffix wildcard accepts one or more subdomain labels and excludes the apex
`example.com`. The literal wildcard `*`, URLs and configured ports are rejected.
DNS names match without case and without one trailing dot; IPv6 configuration
can use bare or bracketed literals. Incoming Host ports are strictly validated
and ignored for matching. Missing, malformed, duplicate or disallowed hosts
receive `400`. Host checks also apply to health and metrics, so include the
host used by deployment probes. Forwarded host headers cannot bypass this
policy.

The socket peer is the client address by default. Forwarding headers from an
untrusted peer are ignored. Configure only the actual ingress proxy addresses
or CIDRs in `trusted_proxies`; the loopback example assumes a local proxy.
Trusting a network allows any host in that network to supply forwarding data.

For a trusted peer, Bessemer parses `X-Forwarded-For` strictly and walks from
right to left through trusted proxy hops, stopping at the first untrusted
address. Multiple header lines form one chain. The aggregate is limited to
1024 bytes and 32 addresses. Malformed trusted forwarding data receives `400`.
IPv4-mapped IPv6 socket addresses are normalized to IPv4. RFC 7239 `Forwarded`
and other forwarding headers do not determine the client address.

Rate limiting uses an exact sliding window per resolved client IP. The
`requests` admitted requests share one `window_seconds` window. Valid preflights,
authentication failures, metrics, HEAD and fallback requests consume quota
unless their exact path appears in the rate limiter's own `exclude_paths`.
API-key and rate-limit exemptions are independent. CORS and malformed proxy
rejections occur before quota admission.

Each IPv4 address has its own quota. IPv6 clients are counted by their /64:
a network gives a host the whole /64, so its low 64 bits are the client's own
to choose, and a quota per full address would be 2^64 quotas for one host.

Exhausted clients receive `429` and an integer `Retry-After`. `max_keys` bounds
the number of active IP records; a full active table fails closed with `503`
and `Retry-After` rather than allocating more records. Expired records are
reclaimed. An unavailable socket address also fails closed when rate limiting
is enabled. Supported bounds are 1–10000 requests, 0.001–86400 seconds and
1–100000 keys, with `requests * max_keys <= 5_000_000`.

Without `redis` the limits are process local: multiple workers or replicas
each have their own quota.

### Shared limits through Redis

With a `redis` store every worker and replica draws on one quota per client IP:

```python
app = App(
    rate_limit={
        "requests": 600,
        "window_seconds": 60,
        "exclude_paths": ["/healthz"],
        "redis": {
            "url_env": "RATE_LIMIT_REDIS_URL",  # redis:// or rediss:// URL
            "key_prefix": "orders:rate:",
            "timeout_seconds": 0.25,
            "pool_size": 16,
            "fail_open": False,
        },
    },
)
```

`url_env` names an environment variable read at startup that holds
`redis://[[user]:password@]host[:port][/db]`, or `rediss://` for TLS (verified
against the system roots or a PEM `ca_file`). Percent-encode reserved characters
in the user and password. Query parameters are rejected, as are a missing or
malformed URL; errors name the variable, never its value. Any server that speaks
RESP2 and runs Lua scripts with `TIME` (Redis 5 or later, Valkey) works.

The decision is the same exact sliding window as the local limiter, made
atomically by one Lua script per request and timed by the Redis server's clock,
so workers with skewed clocks still agree. Each client IP is a sorted set at
`key_prefix` plus the address, holding one member per admitted request and
expiring after one idle window. Use a distinct `key_prefix` for each policy
that shares a database. `max_keys` bounds only the local table and is rejected
with `redis`.

Each worker keeps at most `pool_size` connections (1–1024, default 16).
Connecting, authenticating, selecting the database, running the script and
reading the reply share one `timeout_seconds` deadline (0.001–10, default
0.25). When the store cannot decide in time, or returns an error, the request
fails closed with `503`, `Retry-After: 1` and the detail
`Rate limiter is unavailable`; `fail_open=True` admits it instead. Either way
`bessemer_rate_limit_store_errors_total` counts the failure and a summary is
written to stderr at most once a minute. A pooled connection that the server
closed is replaced once per request, which can count a request twice if the
server ran the script but the reply was lost.

Redis Cluster redirections (`MOVED`/`ASK`) and Sentinel discovery are not
supported; point `url_env` at a primary or a proxy that handles them.

## Request IDs, security headers and access logs

Exactly one valid `X-Request-ID` is preserved and returned. Valid IDs contain
1–128 ASCII letters, digits or `._:-`. Missing, duplicate or invalid IDs are
replaced with a generated identifier. IDs are correlation labels, not
authentication credentials. Set `request_id=False` to disable this behavior.
A generated identifier is a prefix chosen at random when the worker starts
followed by that worker's request count in hexadecimal. The prefix says
nothing about the process, but the count is sequential: a client that reads
two of its own IDs can tell how many requests that worker served in between.

Default security headers are `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`, including on native
control errors. Set `security_headers=False` if the application needs a
different policy. Configure TLS, HSTS and an application-specific Content
Security Policy at the ingress or application boundary. Protocol framing
rejections can occur before the application controls run.

`access_log=True` writes one JSON event per completed request to stderr. Events
contain only `event`, `timestamp`, `method`, declared route template, `status`,
`duration_ms`, `request_id` and resolved `client_ip`. Unknown paths use
`<unmatched>` and cancelled requests use `<cancelled>`. Raw paths, query strings,
request and response bodies, cookies and authentication headers are omitted.
Keep sensitive values out of request IDs and declared static route names.

The producer never waits for stderr: a background writer receives a queue of
at most 1024 events. Full or disconnected queues drop events and increment
`bessemer_access_log_dropped_total`. A write that fails (a full nonblocking
pipe, for example) loses and counts that one event and the writer carries on;
only a closed sink (`EPIPE`) stops it, after which every event is counted as
dropped. Logging is best effort and pending events
are not guaranteed to flush after forced shutdown. Route telemetry supports at
most 1024 declared templates, each at most 1024 bytes.
Each encoded record, including its newline, is at most 4096 bytes and is emitted
in one write. On the qualified Linux target this keeps pipe records atomic
across workers. Oversized records are dropped as whole events and counted.

## OpenTelemetry tracing

`App(otel=OpenTelemetry(...))` turns on native W3C Trace Context propagation
and, with an `endpoint`, OTLP span export:

```python
from bessemer import App, OpenTelemetry

app = App(otel=OpenTelemetry(
    endpoint="http://otel-collector:4318/v1/traces",
    service_name="orders",
    sample_ratio=0.1,
))
# Or read the standard OTEL_* variables:
app = App(otel=OpenTelemetry.from_env())
```

Every HTTP request gets one server span, named `METHOD /declared/route` (or
just the method for an unmatched path), with `http.request.method`,
`url.path`, `http.route`, `http.response.status_code` and `client.address`.
Query strings, headers and bodies are never recorded. Responses of 500 and
above, and requests cancelled before a response, have error status. A valid
`traceparent` header makes the span a child of the caller's span, and its
`tracestate` (up to 512 bytes) is carried along. Otherwise a new trace starts.
An invalid or repeated `traceparent` is ignored. Sampling is parent based: the
caller's sampled flag is always kept, and new traces are sampled with
probability `sample_ratio` by trace id.

Handlers see their span as `request.traceparent`, ready to forward on outbound
calls. When the `opentelemetry-api` package is installed (`pip install 'bessemer[otel]'`), the span is also the
current OpenTelemetry context while the route, its dependencies and HTTP
middleware run, so spans recorded with the OpenTelemetry SDK become its
children. Sync handlers inherit it on the executor.

Without an `endpoint`, nothing is exported: the caller's `traceparent` is
only propagated to the handler and the SDK, for applications that export with
the OpenTelemetry SDK themselves.

Spans are exported as OTLP/HTTP JSON to `endpoint`, `http` or `https`, from a
dedicated thread with a queue of 2048 spans. Batches hold up to 512 spans and
are sent at least once a second, each within 10 seconds. Request handling
never waits for the collector. A full queue or a failed export drops spans,
counts them in `bessemer_otel_spans_dropped_total` and writes one diagnostic
per failure streak. `headers_env` names an environment variable with export
headers in the OTLP form (`key=value,key2=value2`, values percent-encoded),
read at startup, for collector authentication. `ca_file` trusts a private CA
for `https`. After a graceful shutdown the worker exports the spans still
queued, waiting up to `graceful_timeout` (at most 10 seconds); spans queued at a
forced shutdown are lost.

`OpenTelemetry.from_env()` reads `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (or
`OTEL_EXPORTER_OTLP_ENDPOINT` plus `/v1/traces`), `OTEL_SERVICE_NAME`,
`OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG`, the `*_HEADERS` and
`*_CERTIFICATE` variables. Only the `http/json` protocol and the `always_*`,
`parentbased_always_*`, `traceidratio` and `parentbased_traceidratio` samplers
are supported. Others, and `OTEL_SDK_DISABLED=true`, fail startup rather than
being ignored. Both ratio samplers are parent based here. WebSocket handshakes
are not traced, and the `opentelemetry-sdk` exporters are not used: the native
exporter sends only Bessemer's server spans.

## Prometheus metrics

Metrics are collected only when `metrics_path` is configured. The protected
endpoint returns Prometheus text with `Cache-Control: no-store`. Available
families are:

- `bessemer_http_requests_total`, labelled by declared route, fixed HTTP method
  and status class.
- `bessemer_http_request_duration_seconds`, a histogram with buckets at 1, 5,
  10, 25, 50, 100, 250 and 500 milliseconds, and 1, 2.5, 5 and 10 seconds.
- `bessemer_http_in_flight` and `bessemer_http_cancelled_total`.
- `bessemer_access_log_dropped_total`.
- `bessemer_otel_spans_dropped_total`, when OpenTelemetry export is configured.
- `bessemer_rate_limit_store_errors_total`, when rate limits use a Redis store.
- `bessemer_metrics_scrape_omitted_series`, the number of series excluded from
  the current scrape by its byte budget, and
  `bessemer_metrics_omitted_series_total`, cumulative omissions across scrapes.

Raw paths, query values, IDs, IP addresses and credentials are never metric
labels. Unknown methods become `OTHER`; unknown routes become `<unmatched>`.
Series are bounded to 10000 plus one overflow series. Dropping an unfinished
request decrements the in-flight gauge and records cancellation. Latency runs
from control admission until a response is produced, rather than measuring
the time a client takes to read the response body. The current scrape is
reflected in subsequent counters.

Each encoded metrics response is limited to 4 MiB. The renderer includes a
deterministic prefix of complete counter/histogram series and omits remaining
series as whole units when the byte budget is reached. The omission metrics
remain present; no individual series or Prometheus line is truncated. Reduce
declared route cardinality or the number of observed route/method/status
combinations if omissions are nonzero.

Rendering snapshots fixed-size values and shared route labels while holding a
brief mutex, then performs formatting on a blocking worker thread. A process
admits only one GET scrape at a time; concurrent scrapes receive `503` with
`Retry-After: 1`. Admission remains held while any encoded response bytes are
owned by the HTTP transport, including during a slow client write. HEAD checks
authentication and returns headers without allocating or rendering a scrape.

Metrics are process local and do not aggregate across workers. Startup rejects
`metrics_path` with more than one supervised worker, because a shared listener
would otherwise return inconsistent snapshots. External replica aggregation
can use one worker per replica and one scrape target per replica.
