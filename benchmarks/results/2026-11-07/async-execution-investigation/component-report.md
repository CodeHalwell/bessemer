# Final Future-only installed-wheel component probe

macOS CPython 3.13.18; immutable main wheel versus exact Future-only wheel, B,F,F,B process order. Each process ran 30,000 iterations × 5 rounds, using process CPU time. All operations call the Bessemer loop `create_future()` path; task type remained C asyncio.Task.

| Operation | Main CPU ns/op | Future-only CPU ns/op | Change |
|---|---:|---:|---:|
| future_new | 208.17 | 160.90 | -22.7% |
| future_result | 242.06 | 198.01 | -18.2% |
| future_add_callback | 230.10 | 191.15 | -16.9% |
| future_plain_batch | 206.85 | 167.72 | -18.9% |
| future_callback_batch | 845.29 | 773.13 | -8.5% |
| future_bookkept_batch | 1053.75 | 942.30 | -10.6% |

Main native SHA-256 `80fc3d9cc41ecaff4b9501e5df2e3d9197a36c61380354f31cb521911582df05`; Future-only native SHA-256 `976824800bf062a3ca74705cdb15859b680d8cba3830f5528014e71eba955b80`. Future-only wheel SHA-256 `c4d0fa48d1b0df8f2119c020265649f8fa84ec6943b597b6675540ddb843c6bc`; source SHA-256 `4f4bd8e89ebf723537ad2a71e0ef34a2afb5bd5fbd252a21f1d913c7bbdede09`.

All six component paths improved in this bounded probe. The separate Linux HTTP fanout repeat did not establish an end-to-end speed gain. Raw per-round samples, interpreter/native hashes and exact command lines: `final-future-micro-*.json`, `final-future-micro-commands.json`.
