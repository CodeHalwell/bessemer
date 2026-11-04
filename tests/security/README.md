# Independent security qualification

`test_adversarial.py` in `tests/python/` exercises real HTTP with malformed
framing, strict Host and cookie handling, duplicate security headers, proxy
spoofing, JSON precision and depth limits, response framing and allocation
limits, and cancellation cleanup. It belongs to the normal installed-wheel
test suite.

`fuzz_transport.py` runs seven bounded Hypothesis properties through two isolated
single-worker services. It compares JSON types and values across Python/Rust,
uses a large-response service as the byte-size oracle for a 256-byte service,
and checks Host allowlists, cookies, scalar request headers, and native response
headers. Malformed JSON properties compare typed bodies and raw `Request.json`
for overwritten invalid values, unpaired surrogates, invalid UTF-8, numeric
bounds, depth, and syntax errors. Generated requests use only loopback and
synthetic fixture data.

Run it from a clean environment with the candidate wheel:

```sh
python3 -m venv work/security-fuzz
work/security-fuzz/bin/python -m pip install hypothesis==6.170.14 /path/to/candidate.whl
work/security-fuzz/bin/python tests/security/fuzz_transport.py \
  --examples 3000 --seed 0xB355E2026 \
  --wheel /path/to/candidate.whl --output work/security-evidence
```

Each selected property receives the requested example count. The output records
attempts and distinct inputs separately, seeds, tool and interpreter versions,
candidate and installed-package hashes, shrunk failures, and fixture server
logs. The supplied wheel must match every installed package file. A failed
property leaves a failed report; a partial run is never marked passed.
The output directory must be new or empty, preventing stale evidence from a
previous run from being mistaken for a new result.

The float budget fixture records a minimized failure discovered in candidate
0.2.0a1: Python rendered an exponent as `e-07`, while native JSON rendered
`e-7`. A response of exactly 256 native bytes was incorrectly rejected by
Python's preliminary budget. The corresponding normal HTTP regression lives in
`test_adversarial.py`.

The duplicate-value fixture records a separate parser regression: validating
only the final object map let a later duplicate key hide an invalid earlier
value. Both typed JSON bodies and raw `Request.json` now validate each original
pair before applying ordinary last-key-wins behavior.

These checks do not establish native code coverage, prove memory safety, replace
an external penetration test, or satisfy the project's M0 performance gate.
Dependency advisory reports are generated separately by
`scripts/security-check.sh`; their database revisions and lockfile hashes must
accompany any clean-scan claim.
