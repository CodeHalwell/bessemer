# Final pull-request review regressions

The publication review corrected two additional cases:

- An outer WebSocket-session cancellation during explicit AG-UI run cancellation
  now propagates after the agent's cleanup finishes. Repeated session cancellation
  cannot resume receiving frames or leave the session alive after cleanup.
- Multi-loop WebSocket scheduling keeps unknown application-state containers on
  the main loop. Dictionaries, slots objects, custom objects and namespace
  subclasses retain compatibility; only an empty default namespace permits
  stateless lane distribution. Root and mounted application cases are covered.

Both macOS wheels were rebuilt into separate `pr-wheels-*` directories and
installed into `venv-pr-*` environments. Each final installed environment passed
24 tests: six WebSocket-lane tests and 18 SDK/native AG-UI tests, with no skips.
The 3.13 GIL remained enabled and the 3.14t GIL remained disabled through imports
and execution. The installed worker matched the source byte for byte.

```text
worker.py SHA-256: b4ee6cd8807e303c4d70bf8ed4003814380726fdb66b6dac3c103b7e132acb8e
adapter.py SHA-256: 1289f4efcf663e7f9f17d28cf518d0d51f8d7520810c86f60bf157b89d7f8c25
```

The final whole-tree Ruff lint/format check passed; focused
new-function complexity and whitespace checks passed. The earlier full
[macOS check](check-macos-313.log) and [Linux check](check-linux-focused.log)
predate these two review fixes. Original benchmark wheels/environments and
measured-build hashes remain unchanged; the focused checks validate the final
review fixes without attributing older measurements to the new wheel.

Evidence: [3.13 log](check-pr-final-313.log),
[3.14t log](check-pr-final-314t.log),
[guarded driver](check-pr-final.py). Earlier driver-path failures are preserved
as `check-pr-final-*-driver-path-failed.log`; the passing rerun adds only the
repository root needed to import the unpackaged example, while asserting that
Bessemer itself comes from site-packages.
