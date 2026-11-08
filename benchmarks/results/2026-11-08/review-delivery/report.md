# PR #110 delivery and performance review fixes

Main merged through `1558cdd`; instruction baseline is exact `362f71a`.

- A (`76c4858`): transfer FileResponse admission and in-flight accounting to delivery, including abandonment without background tasks.
- B (`18cd49b`): share conditional/range selection with the ASGI bridge; run preparation and reads in the cancellation-safe pool and wait before closing.
- C (`127ec6e`): acknowledge HTTP/2 HEAD/204 delivery after a successful header flush; blocked/failed writes cannot start background tasks.
- D (this commit): precompute adaptive queue deadlines, box optional reply headers, consolidate rare invocation state, and inline startup helpers. Private vgdb prefixes now isolate comparison counters.

### Instructions per request

Callgrind, one connection, 2000 requests; fails above +1%.

| Loop | Route | Base | Head | Change | |
| --- | --- | ---: | ---: | ---: | --- |
| asyncio | `/json` | 47,800 | 47,894 | +0.20% | ok |
| asyncio | `/items/7?q=abc` | 50,853 | 51,057 | +0.40% | ok |
| asyncio | `/echo` | 218,765 | 212,826 | -2.71% | faster |
| bessemer | `/json` | 32,027 | 32,141 | +0.36% | ok |
| bessemer | `/items/7?q=abc` | 35,032 | 35,225 | +0.55% | ok |
| bessemer | `/echo` | 202,588 | 197,655 | -2.43% | faster |


Measured with CPython 3.13.15, one connection, 500 warmup and 2,000 measured requests per route. Both wheels used the same interpreter/compiler and dependencies. The CI driver completed successfully at the stricter 1% threshold:

```sh
python benchmarks/profile/instructions_compare.py \
  --base "$BASE_PYTHON" --head "$HEAD_PYTHON" \
  --routes json,items,echo --loops asyncio,bessemer --threshold 1 \
  --json gate.json --markdown gate.md
```

The release wheel was built and installed before the final required check:
`PYTHON=/home/cmux/venvs/codex/bin/python BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`.
All static checks passed; 245 Rust tests passed (2 ignored), 1,035 Python tests ran (1,001 passed, 34 skipped), and 34 benchmark harness tests passed.

Regressions cover slow file admission/metrics and abandonment, ASGI validators/ranges and cancellation read-before-close, blocked/failed HTTP/2 header writes, adaptive target boundaries, combined disconnect-watch/background resources, and private profiling controls.

Qualification in this round covers CPython 3.13 only; it does not requalify 3.12/3.14t. No isolated throughput claim is made. Native health remains Tier 0, Python bridge routes Tier 2, and the formal M0 gate stays open. The prohibited schema/params/security/middleware/router files were not modified.
