# Load test results (SQLite, single machine, mock providers with zero network latency)

## Cycle cost vs fleet size (median of 12 cycles; ms)

| agents | cycle total p50 | snapshot | stats | pacing | safety | allocate (per cycle) | dispatch (per call) | calls/cycle |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.6 | 0.22 | 0.02 | 0.01 | 0.07 | 0.02 | 0.05 | 3 |
| 1,000 | 1.2 | 0.6 | 0.03 | 0.03 | 0.14 | 0.04 | 0.05 | 33 |
| 10,000 | 6088.9 | 4.01 | 0.03 | 0.08 | 0.22 | 6036.92 | 0.07 | 337 |

## Reservation contention
8 threads draining 2000 agents: reserved=2000, unique=2000, **double allocations=0**, 4522/s.

## Event ingestion
6000 provider events, one thread: 22485 events/s (each = 1 dedupe insert + 1-2 row updates in one transaction).
