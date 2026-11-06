# Broad API comparison results

195 measured samples; 6,992,391 completed responses/messages; 0 reported errors; 0 unexpected HTTP statuses. 14 samples flagged as client-limited.

Throughput is requests/s, except WebSocket messages/s. Values are medians of per-sample values, with minimum/maximum ranges. CPU costs use process-tree CPU time divided by completed responses/messages.

## Default loop

| Capability | rc1 throughput [range] | Candidate throughput [range] | Change | CPU µs/response, rc1 → candidate |
| --- | ---: | ---: | ---: | ---: |
| json | 66,880 [51,319–100,737] | 68,263 [57,647–81,956] | +2.1% | 30.9 → 27.5 |
| echo | 52,043 [43,055–52,935] | 48,995 [44,225–53,346] | -5.9% | 44.7 → 43.6 |
| coerced | 25,620 [20,643–26,833] | 26,862 [22,874–31,672] | +4.8% | 62.3 → 60.7 |
| invalid | 46,714 [44,182–50,488] | 46,736 [45,420–63,761] | +0.0% | 46.5 → 44.7 |
| bytes | 11,914 [11,339–11,923] | 13,693 [11,173–13,879] | +14.9% | 133.6 → 124.5 |
| io | 38,715 [38,363–40,830] | 36,602 [32,423–42,596] | -5.5% | 44.1 → 42.0 |
| fanout | 8,186 [6,720–8,904] | 6,797 [4,533–7,742] | -17.0% | 143.1 → 166.4 |
| dependencies | 26,014 [23,303–28,153] | 22,813 [21,764–26,572] | -12.3% | 58.8 → 63.4 |
| sync | 6,104 [5,918–6,230] | 5,991 [5,666–6,231] | -1.8% | 168.7 → 184.1 |
| stream | 3,699 [2,897–5,560] | 4,575 [4,141–5,657] | +23.7% | 313.9 → 249.4 |
| sync-stream | 1,087 [889–1,159] | 975 [830–1,399] | -10.2% | 1209.4 → 1298.6 |
| events | 5,031 [4,876–5,567] | 5,655 [3,365–8,167] | +12.4% | 258.0 → 241.1 |
| websocket | 28,419 [25,766–29,335] | 35,885 [32,299–38,571] | +26.3% | 27.6 → 23.4 |

## Rust loop

| Capability | rc1 throughput [range] | Candidate throughput [range] | Change | CPU µs/response, rc1 → candidate |
| --- | ---: | ---: | ---: | ---: |
| json | 42,120 [40,558–44,484] | 38,697 [34,303–42,570] | -8.1% | 19.8 → 21.2 |
| echo | 24,264 [18,384–24,713] | 22,469 [21,686–24,909] | -7.4% | 37.3 → 40.6 |
| coerced | 12,488 [11,849–14,905] | 14,771 [13,327–15,959] | +18.3% | 69.0 → 54.9 |
| invalid | 27,115 [23,018–28,366] | 22,371 [21,725–24,879] | -17.5% | 32.6 → 41.3 |
| bytes | 8,044 [7,091–9,260] | 10,132 [8,463–11,294] | +26.0% | 89.7 → 84.4 |
| io | 18,945 [17,503–19,956] | 28,503 [21,199–33,342] | +50.4% | 25.5 → 28.7 |
| fanout | 9,230 [8,723–14,023] | 8,757 [7,839–9,099] | -5.1% | 95.0 → 103.5 |
| dependencies | 20,746 [19,875–21,350] | 16,953 [15,308–18,725] | -18.3% | 42.2 → 51.8 |
| sync | 5,717 [5,470–5,976] | 5,653 [5,278–6,053] | -1.1% | 126.9 → 126.4 |
| stream | 5,565 [4,020–5,866] | 5,537 [5,522–5,588] | -0.5% | 156.9 → 164.7 |
| sync-stream | 1,340 [985–1,501] | 1,199 [1,182–1,747] | -10.5% | 769.5 → 760.7 |
| events | 4,829 [3,754–5,675] | 5,527 [4,526–7,092] | +14.4% | 179.9 → 144.1 |
| websocket | 31,079 [23,111–34,858] | 25,676 [24,443–29,533] | -17.4% | 13.1 → 17.1 |

## Candidate compared with FastAPI

| Capability | FastAPI throughput [range] | Default / FastAPI | Rust loop / FastAPI |
| --- | ---: | ---: | ---: |
| json | 10,361 [9,810–10,869] | 6.59× | 3.73× |
| echo | 5,804 [5,235–5,937] | 8.44× | 3.87× |
| coerced | 6,554 [5,221–7,950] | 4.10× | 2.25× |
| invalid | 5,687 [5,044–6,078] | 8.22× | 3.93× |
| bytes | 5,957 [4,836–6,028] | 2.30× | 1.70× |
| io | 7,419 [7,402–10,212] | 4.93× | 3.84× |
| fanout | 3,879 [3,865–3,922] | 1.75× | 2.26× |
| dependencies | 6,263 [5,738–6,881] | 3.64× | 2.71× |
| sync | 4,131 [3,755–4,706] | 1.45× | 1.37× |
| stream | 2,100 [2,033–2,470] | 2.18× | 2.64× |
| sync-stream | 595 [536–598] | 1.64× | 2.02× |
| events | 1,933 [1,658–2,183] | 2.93× | 2.86× |
| websocket | 29,164 [28,127–33,144] | 1.23× | 0.88× |

Same-host, unpinned, short exploratory samples. WebSocket figures can be limited by the Python client. See raw.json for per-sample CPU, latency, errors, versions and native hashes.
