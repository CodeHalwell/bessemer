Python 3.13.18 on Linux-6.18.44-fc-v236-x86_64-with-glibc2.39 (4 CPUs); loops: asyncio 3.13.18, uvloop 0.23.0, bessemer 0.1.0; 5 runs per case, median shown.

| benchmark | unit | asyncio ops/s | uvloop ops/s | uvloop vs asyncio | bessemer ops/s | bessemer vs asyncio |
|---|---|---:|---:|---:|---:|---:|
| call_soon/chain | cb | 932.4k ±1% | 2.39M ±2% | 2.56x | 3.14M ±0% | 3.37x |
| call_soon/fanout100 | cb | 2.10M ±0% | 6.02M ±2% | 2.87x | 5.64M ±1% | 2.69x |
| call_soon/batch1000 | cb | 2.43M ±3% | 7.33M ±2% | 3.01x | 6.54M ±0% | 2.69x |
| call_soon/threadsafe_chain | cb | 352.9k ±1% | 1.05M ±19% | 2.97x | 3.24M ±0% | 9.17x |
| timers/schedule_cancel_half | timer | 825.3k ±26% | 5.17M ±0% | 6.27x | 3.81M ±6% | 4.61x |
| timers/zero_delay_fire | timer | 784.4k ±4% | 5.37M ±10% | 6.84x | 4.29M ±27% | 5.47x |
| timers/far_future_cancel | timer | 1.40M ±2% | 1.07M ±2% | 0.77x | 2.95M ±3% | 2.11x |
| timers/timer_chain | timer | 575.7k ±13% | 2.09M ±9% | 3.62x | 2.37M ±10% | 4.11x |
| futures/resolved_await | future | 4.53M ±0% | 5.35M ±4% | 1.18x | 4.56M ±0% | 1.01x |
| futures/call_soon_resolved | future | 350.8k ±15% | 855.8k ±23% | 2.44x | 975.7k ±1% | 2.78x |
| futures/done_callback | future | 1.32M ±33% | 2.49M ±9% | 1.89x | 2.25M ±0% | 1.71x |
| tasks/create_gather1000 | task | 408.1k ±4% | 652.5k ±1% | 1.60x | 605.4k ±8% | 1.48x |
| tasks/create_await | task | 282.0k ±10% | 604.0k ±2% | 2.14x | 668.2k ±1% | 2.37x |
| tasks/ping_pong | switch | 608.4k ±7% | 1.32M ±0% | 2.17x | 1.42M ±1% | 2.34x |
| tasks/sleep0_x10 | yield | 1.27M ±5% | 2.37M ±2% | 1.87x | 2.41M ±1% | 1.90x |
| sockets/sock_echo_64B | rtt | 49.1k ±1% | 72.1k ±5% | 1.47x | 113.6k ±1% | 2.31x |
| sockets/sock_echo_16KiB | rtt | 43.2k ±1% | 62.8k ±1% | 1.45x | 83.2k ±0% | 1.93x |
| sockets/streams_echo_64B | rtt | 75.5k ±2% | 122.6k ±2% | 1.62x | 107.0k ±1% | 1.42x |
| sockets/streams_echo_16KiB | rtt | 60.5k ±1% | 84.3k ±2% | 1.40x | 80.4k ±1% | 1.33x |
| sockets/protocol_echo_64B | rtt | 109.3k ±1% | 191.8k ±1% | 1.75x | 162.1k ±1% | 1.48x |
| sockets/protocol_echo_16KiB | rtt | 93.4k ±0% | 132.6k ±1% | 1.42x | 128.9k ±1% | 1.38x |
| executor/sequential | call | 12.3k ±1% | 14.4k ±11% | 1.17x | 14.2k ±7% | 1.15x |
| executor/concurrent32 | call | 25.6k ±6% | 41.6k ±10% | 1.63x | 40.3k ±5% | 1.58x |
| **geometric mean** |  |  |  | **2.04x** |  | **2.24x** |

<details><summary>Per-operation wall and CPU time</summary>

| benchmark | loop | ops/s | ns/op wall | ns/op CPU | spread | ops x runs |
|---|---|---:|---:|---:|---:|---:|
| call_soon/chain | asyncio | 932.4k | 1072 | 1070 | ±1.5% | 400000 x 5 |
| call_soon/chain | uvloop | 2.39M | 418 | 418 | ±2.4% | 400000 x 5 |
| call_soon/chain | bessemer | 3.14M | 319 | 318 | ±0.3% | 400000 x 5 |
| call_soon/fanout100 | asyncio | 2.10M | 477 | 476 | ±0.5% | 400000 x 5 |
| call_soon/fanout100 | uvloop | 6.02M | 166 | 165 | ±2.4% | 400000 x 5 |
| call_soon/fanout100 | bessemer | 5.64M | 177 | 177 | ±0.7% | 400000 x 5 |
| call_soon/batch1000 | asyncio | 2.43M | 411 | 410 | ±3.4% | 400000 x 5 |
| call_soon/batch1000 | uvloop | 7.33M | 136 | 136 | ±2.4% | 400000 x 5 |
| call_soon/batch1000 | bessemer | 6.54M | 153 | 153 | ±0.5% | 400000 x 5 |
| call_soon/threadsafe_chain | asyncio | 352.9k | 2834 | 2826 | ±1.3% | 300000 x 5 |
| call_soon/threadsafe_chain | uvloop | 1.05M | 956 | 955 | ±18.8% | 300000 x 5 |
| call_soon/threadsafe_chain | bessemer | 3.24M | 309 | 309 | ±0.4% | 300000 x 5 |
| timers/schedule_cancel_half | asyncio | 825.3k | 1212 | 1206 | ±25.5% | 200000 x 5 |
| timers/schedule_cancel_half | uvloop | 5.17M | 193 | 193 | ±0.4% | 200000 x 5 |
| timers/schedule_cancel_half | bessemer | 3.81M | 263 | 254 | ±5.8% | 200000 x 5 |
| timers/zero_delay_fire | asyncio | 784.4k | 1275 | 1275 | ±3.8% | 200000 x 5 |
| timers/zero_delay_fire | uvloop | 5.37M | 186 | 186 | ±9.9% | 200000 x 5 |
| timers/zero_delay_fire | bessemer | 4.29M | 233 | 230 | ±26.9% | 200000 x 5 |
| timers/far_future_cancel | asyncio | 1.40M | 717 | 716 | ±1.9% | 200000 x 5 |
| timers/far_future_cancel | uvloop | 1.07M | 933 | 931 | ±1.8% | 200000 x 5 |
| timers/far_future_cancel | bessemer | 2.95M | 339 | 335 | ±2.6% | 200000 x 5 |
| timers/timer_chain | asyncio | 575.7k | 1737 | 1737 | ±13.0% | 200000 x 5 |
| timers/timer_chain | uvloop | 2.09M | 479 | 479 | ±9.1% | 200000 x 5 |
| timers/timer_chain | bessemer | 2.37M | 423 | 418 | ±10.2% | 200000 x 5 |
| futures/resolved_await | asyncio | 4.53M | 221 | 221 | ±0.3% | 500000 x 5 |
| futures/resolved_await | uvloop | 5.35M | 187 | 186 | ±3.9% | 500000 x 5 |
| futures/resolved_await | bessemer | 4.56M | 219 | 219 | ±0.1% | 500000 x 5 |
| futures/call_soon_resolved | asyncio | 350.8k | 2851 | 2850 | ±14.5% | 200000 x 5 |
| futures/call_soon_resolved | uvloop | 855.8k | 1169 | 1151 | ±22.8% | 200000 x 5 |
| futures/call_soon_resolved | bessemer | 975.7k | 1025 | 1025 | ±0.8% | 200000 x 5 |
| futures/done_callback | asyncio | 1.32M | 759 | 759 | ±33.0% | 300000 x 5 |
| futures/done_callback | uvloop | 2.49M | 401 | 397 | ±8.8% | 300000 x 5 |
| futures/done_callback | bessemer | 2.25M | 445 | 445 | ±0.5% | 300000 x 5 |
| tasks/create_gather1000 | asyncio | 408.1k | 2450 | 2423 | ±4.2% | 150000 x 5 |
| tasks/create_gather1000 | uvloop | 652.5k | 1533 | 1532 | ±1.4% | 150000 x 5 |
| tasks/create_gather1000 | bessemer | 605.4k | 1652 | 1646 | ±7.5% | 150000 x 5 |
| tasks/create_await | asyncio | 282.0k | 3546 | 3496 | ±9.8% | 100000 x 5 |
| tasks/create_await | uvloop | 604.0k | 1656 | 1655 | ±1.7% | 100000 x 5 |
| tasks/create_await | bessemer | 668.2k | 1497 | 1495 | ±0.6% | 100000 x 5 |
| tasks/ping_pong | asyncio | 608.4k | 1644 | 1644 | ±6.9% | 200000 x 5 |
| tasks/ping_pong | uvloop | 1.32M | 758 | 758 | ±0.5% | 200000 x 5 |
| tasks/ping_pong | bessemer | 1.42M | 703 | 703 | ±1.0% | 200000 x 5 |
| tasks/sleep0_x10 | asyncio | 1.27M | 789 | 789 | ±4.5% | 200000 x 5 |
| tasks/sleep0_x10 | uvloop | 2.37M | 421 | 421 | ±1.9% | 200000 x 5 |
| tasks/sleep0_x10 | bessemer | 2.41M | 415 | 415 | ±1.5% | 200000 x 5 |
| sockets/sock_echo_64B | asyncio | 49.1k | 20347 | 20336 | ±0.9% | 20000 x 5 |
| sockets/sock_echo_64B | uvloop | 72.1k | 13876 | 13809 | ±4.8% | 20000 x 5 |
| sockets/sock_echo_64B | bessemer | 113.6k | 8806 | 8801 | ±1.0% | 20000 x 5 |
| sockets/sock_echo_16KiB | asyncio | 43.2k | 23154 | 23151 | ±1.2% | 12000 x 5 |
| sockets/sock_echo_16KiB | uvloop | 62.8k | 15917 | 15908 | ±1.3% | 12000 x 5 |
| sockets/sock_echo_16KiB | bessemer | 83.2k | 12014 | 12007 | ±0.5% | 12000 x 5 |
| sockets/streams_echo_64B | asyncio | 75.5k | 13237 | 13230 | ±1.6% | 12000 x 5 |
| sockets/streams_echo_64B | uvloop | 122.6k | 8157 | 8156 | ±1.9% | 12000 x 5 |
| sockets/streams_echo_64B | bessemer | 107.0k | 9343 | 9333 | ±1.5% | 12000 x 5 |
| sockets/streams_echo_16KiB | asyncio | 60.5k | 16541 | 16538 | ±0.9% | 8000 x 5 |
| sockets/streams_echo_16KiB | uvloop | 84.3k | 11857 | 11859 | ±1.6% | 8000 x 5 |
| sockets/streams_echo_16KiB | bessemer | 80.4k | 12441 | 12440 | ±1.0% | 8000 x 5 |
| sockets/protocol_echo_64B | asyncio | 109.3k | 9147 | 9126 | ±0.7% | 20000 x 5 |
| sockets/protocol_echo_64B | uvloop | 191.8k | 5213 | 5209 | ±0.7% | 20000 x 5 |
| sockets/protocol_echo_64B | bessemer | 162.1k | 6170 | 6167 | ±1.0% | 20000 x 5 |
| sockets/protocol_echo_16KiB | asyncio | 93.4k | 10710 | 10708 | ±0.2% | 10000 x 5 |
| sockets/protocol_echo_16KiB | uvloop | 132.6k | 7541 | 7541 | ±1.2% | 10000 x 5 |
| sockets/protocol_echo_16KiB | bessemer | 128.9k | 7761 | 7754 | ±0.8% | 10000 x 5 |
| executor/sequential | asyncio | 12.3k | 81048 | 81740 | ±1.4% | 8000 x 5 |
| executor/sequential | uvloop | 14.4k | 69355 | 68576 | ±10.9% | 8000 x 5 |
| executor/sequential | bessemer | 14.2k | 70528 | 70618 | ±7.0% | 8000 x 5 |
| executor/concurrent32 | asyncio | 25.6k | 39085 | 44590 | ±5.8% | 12000 x 5 |
| executor/concurrent32 | uvloop | 41.6k | 24027 | 23448 | ±10.0% | 12000 x 5 |
| executor/concurrent32 | bessemer | 40.3k | 24792 | 23973 | ±5.3% | 12000 x 5 |

</details>
