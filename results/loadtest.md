# Load test results (SQLite, single machine, mock providers with zero network latency)

## Cycle cost vs fleet size (median of 12 cycles; ms)

| agents | cycle total p50 | snapshot | stats | pacing | safety | allocate (per cycle) | dispatch (per call) | calls/cycle |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.5 | 0.24 | 0.02 | 0.01 | 0.08 | 0.02 | 0.07 | 3 |
| 1,000 | 0.9 | 0.55 | 0.02 | 0.02 | 0.09 | 0.02 | 0.05 | 33 |
| 10,000 | 48.9 | 3.68 | 0.07 | 0.06 | 0.19 | 12.23 | 0.05 | 337 |

## Reservation contention
8 threads draining 2000 agents: reserved=2000, unique=2000, **double allocations=0**, 18315/s.

## Event ingestion
6000 provider events, one thread: 26151 events/s (each = 1 dedupe insert + 1-2 row updates in one transaction).
