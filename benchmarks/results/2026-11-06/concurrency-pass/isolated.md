# Isolated timers and model codec

Times are medians from sequential, installed-wheel runs.

| Requested sleep | rc1 Rust loop | Candidate Rust loop |
| --- | ---: | ---: |
| 0.1 ms | 2.307 ms | 0.133 ms |
| 1 ms | 2.288 ms | 1.048 ms |
| 5 ms | 6.802 ms | 5.065 ms |
| 20 ms | 22.066 ms | 20.097 ms |

| Model scenario | Encode, rc1 → candidate | Parse + validation, rc1 → candidate | Unchanged plain-encode control, rc1 → candidate |
| --- | ---: | ---: | ---: |
| plain_1 | 0.30 → 0.24 µs | 1.14 → 0.80 µs | 0.26 → 0.24 µs |
| plain_20 | 4.41 → 3.37 µs | 15.72 → 11.67 µs | 3.94 → 3.54 µs |
| plain_100 | 20.60 → 17.71 µs | 73.48 → 54.90 µs | 18.13 → 17.50 µs |
| escaped_aliases_1 | 0.35 → 0.23 µs | 1.41 → 0.95 µs | 0.34 → 0.26 µs |
| escaped_aliases_20 | 5.33 → 3.54 µs | 17.95 → 14.77 µs | 4.42 → 4.17 µs |
| escaped_aliases_100 | 24.26 → 18.81 µs | 103.37 → 74.31 µs | 23.25 → 20.42 µs |

The unchanged control also varied between runs. Use the raw samples rather than attributing the entire timing difference to the code changes.
