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

