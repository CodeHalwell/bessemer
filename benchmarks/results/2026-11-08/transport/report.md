# Connection, HTTP/2 and TLS cost, 8 November 2026

Baseline local main `47fd343`; CPython 3.13.15 installed release wheels,
compare app `/json`, one worker, native event loop, fixed 4,096 admission.
Private vgdb prefixes and PID-scoped zero/dump. HTTP profiles use 1,000 warmup
and 6,000 measured requests; TLS connection profiles use 30 warmup and 300
measured connections. All reported HTTP responses are 200. Request counts
include the new client connection(s) after warmup; connection setup is
amortized for HTTP/2, deliberately not amortized for HTTP/1 close and TLS
connection tests. Kernel accept/close and network latency are not instruction
counts. Shared-host throughput is not qualified.

| `/json` workload | Before instructions/request | After | Change |
| --- | ---: | ---: | ---: |
| HTTP/1 keep-alive, c1 | 33,329 | 33,325 | unchanged |
| HTTP/1 new connection each request, c1 | 48,059 | 45,612 | -5.1% |
| h2c, c1 / 1 stream | 69,025 | 66,853 | -3.1% |
| h2c, c1 / 64 streams | 56,483 | 54,453 | -3.6% |
| h2c, c1 / 128 streams | 56,523 | 54,390 | -3.8% |
| TLS h2, c1 / 64 streams | 59,291 | 57,269 | -3.4% |
| TLS 1.3 full handshake + closing HTTP/1 request | 11,164,534 | 11,161,627 | -0.03% |
| TLS 1.3 resumed connection + closing HTTP/1 request | 1,306,369 | 1,303,584 | -0.21% |

## Implemented

`bf6fed8` uses the native JSON writer for closing HTTP/1.1 responses. This
avoids Response/Body/header construction previously used on every closing
connection. General HTTP/1.0 framing and deferred/background response handling
are preserved. The Rust regression compares the direct and general writer's
wire bytes, including repeated cookies, status, security/request-ID headers
and Connection: close. Before, general `write_parts` cost about 2,838
instructions/request inclusive; accept4's userspace component was only about
125 instructions/request, excluding kernel work.

`8755692` pins each Hyper HTTP/2 stream future before Tokio submission.
It saves copies of the inline future when Tokio completes the task; tasks,
wakeups, cancellation and ownership remain with Tokio. At 64 streams memcpy
falls from 6,889 to 4,659 instructions/request. HPACK header encoding remains
about 8,400 instructions/stream inclusive, and origin-form conversion about
653. Stream limits, rapid-reset protection, flow-control/send timeout tracking
and post-send cleanup guards retain their existing behavior.

## TLS findings

No runtime TLS settings changed. Rustls configuration is parsed at startup
and reload, not rebuilt per accepted connection. Defaults in rustls 0.23.46
are a 256-entry stateful session cache, two TLS 1.3 tickets per handshake,
no stateless ticket encryptor and no early data. TLS 1.3 stateful ticket
resumption works: each 300-connection resumed profile explicitly verifies
299 resumptions (the first connection establishes a fresh session). Tickets
are consumed and replaced; this preserves ECDHE forward secrecy. An installed
regression checks verified TLS 1.3, unchanged ALPN and repeated resumption.

The fixture uses RSA-2048 and the same certificate/key for before and after
TLS profiles. Full-handshake RSA signing accounts for about 89.5% of worker
instructions. On resumed connections X25519 key agreement/public-key generation
accounts for about 51% and SHA-512 blocks about 32% (inclusive components can
overlap); session-cache lookup/consume is only 0.39%. One full handshake still
occurs in each resumed sample. These are whole connection + request costs,
not isolated crypto microbenchmarks, and Callgrind's crypto CPU-feature choices
make them unsuitable for estimating real-time handshake throughput.

The small TLS savings are the closing-response improvement; no speculative
session-cache rewrite, cipher/certificate replacement, ticket/early-data change
or security relaxation was retained. Full handshakes remain expensive and
HTTP/2 remains materially more costly per stream than keep-alive HTTP/1.

## Reproduction and rejected samples

Use `benchmarks/profile/instructions_transport.sh`, e.g.:

```sh
PYTHON=/home/cmux/venvs/codex/bin/python OHA=$HOME/.cargo/bin/oha \
  BESSEMER_EVENT_LOOP=auto PROFILE_MAX_IN_FLIGHT=4096 PORT=18101 \
  CONNECTIONS=1 STREAMS=64 MODE=h2c OUT=/tmp/my-private-profile \
  benchmarks/profile/instructions_transport.sh /json 6000
```

Modes: keep, close, h2c, h2tls, tls-full, tls-resume. TLS generation uses a
CA:false self-signed leaf trusted explicitly by clients; no insecure flags.
For paired TLS runs set PROFILE_TLS_CERT/PROFILE_TLS_KEY to the same files.
To profile the immutable baseline, unpack its wheel into a private directory
and set PYTHONPATH to that directory. The benchmark does not alter M0.

Raw output is local under `raw/`. Baseline plain HTTP profiles: before-keep,
before-close, before-h2c; baseline valid TLS profiles: verified-before-*.
Closing-only controls: close-keep, close-close. Final h2c/h2tls/full-TLS:
pinned-*; final resumed TLS: final-tls-resume; p1: before/pinned-h2c-p1;
p128: final-before/after-h2c-p128.

The initial before-h2tls sample failed certificate verification: OpenSSL's
default generated CA:true certificate was rejected as an end entity. Its
10,975,772 count is invalid and excluded. The harness now requires every oha
request to return 200 (oha exits successfully even when all requests fail).
Initial before/close TLS connection runs overlapped wheel replacement and
are superseded by immutable-baseline, same-key runs above. Incomplete startup
attempts (before/pinned-h2c-p128 and pinned-tls-resume) produced no count and
were rerun successfully. Wall-clock measurements under Callgrind are excluded.

## Validation

Focused: closing writer regression 1 Rust test; transport suite 12 Rust and
11 installed Python tests; installed TLS suite 9 tests, all pass.
Full installed-wheel check results are recorded below after completion.
Isolated one-core req/s/p99 against Axum/uvicorn remains for the orchestrator;
an isolated run was requested, with no throughput acceptance inferred here.
Python routes remain Tier 2, native health Tier 0; the formal M0 gate is open.

Final installed-wheel `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` passed:
241 Rust tests (2 ignored), 992 Python tests (34 skipped), 23 original harness
tests, rustfmt, warnings-denied Clippy, Ruff lint/format and shell syntax.
The final client response-length assertion was added after this check and
verified with 3 full and 3 resumed connections plus focused Ruff checks.
