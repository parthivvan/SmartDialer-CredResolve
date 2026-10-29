# What breaks first as the fleet grows (100 -> 1,000 -> 10,000 -> 100,000 agents)

All numbers are measured by `python -m smartdialer.loadtest` on one machine with SQLite and zero-latency mock providers
(`results/loadtest.md`). Anything not measured is labelled **reasoned**.

| Agents | Cycle p50 | Snapshot | Allocate (per cycle) | Calls/cycle |
|---|---|---|---|---|
| 100 | 0.5 ms | 0.24 ms | 0.02 ms | 3 |
| 1,000 | 0.9 ms | 0.55 ms | 0.02 ms | 33 |
| 10,000 | 48.9 ms | 3.68 ms | 12.2 ms | 337 |

Also measured: 8 threads draining 2,000 agents -> 0 double allocations at ~18k reservations/s; one thread ingests ~26k provider events/s.

## Bottleneck 1 (found by measurement, fixed): the borrower-queue query, not CPU and not agent contention
My first 10,000-agent run took **6,089 ms per cycle** (`results/loadtest_before_fix.md`), 99% of it in allocation.
Cause: `reserve_borrower` orders pending borrowers by `callback, priority, next_attempt_at`; my index did not match that
order, so every reservation sorted ~60,000 rows (~18 ms each x 337 calls). Fix: an index whose column order equals the
`ORDER BY` (`ix_borrowers_pick`) -> **12 ms per cycle (~500x)**. Lesson I would repeat in production: run `EXPLAIN` on the
hot pick queries and keep a load test in CI so query-plan regressions fail a build.
Caveat: that index scans past not-yet-eligible retries at the head of the ordering. In PostgreSQL use a partial index
(`WHERE state='PENDING'`) and keep retries in a separate delayed queue so the hot index stays small.

## Next, in the order I expect them to hurt (reasoned unless noted)
2. **Serial dispatch.** Provider calls are I/O. 337 calls/cycle x 50-300 ms real HTTP = 17-100 s if serial. The mock is instant,
   so this does not show in my numbers. Fix: dispatch through a bounded worker pool / async client with per-provider
   concurrency limits and timeouts; the DB transaction already ends before dispatch, so this change is local.
3. **Snapshot is O(agents)** (measured: 0.24 -> 0.55 -> 3.7 ms, linear). At 100k agents expect ~40 ms per cycle. Fix: keep
   per-state counters in a campaign-summary row updated in the same transaction as each transition, and a sorted set of wrap-up
   deadlines, so the snapshot is O(1).
4. **One decision per campaign at a time** (ADR-3). Fine to tens of thousands of agents, then it caps pacing frequency.
   Fix: split a large campaign into pools (disjoint agents + borrower partition), each with its own controller instance,
   with the abandonment cap enforced per pool *and* in aggregate (a global counter, checked by every controller).
5. **Single primary write capacity.** Event load ~ 4 events per call. At 10k agents (~220 dials/s at p=0.5, talk 90 s) that is
   under 1k events/s versus ~26k/s measured: comfortable. At 100k agents it is ~9k/s: still inside one box on SQLite numbers but
   too close for comfort on a networked database with durable commits. Fix: shard by campaign/pool across databases; events are
   already keyed by `call_ref`, so route by call.

**Why not "add servers":** workers are stateless and already scale horizontally, but they all contend on the same rows.
More workers on one campaign increases lock waits (SQLite: writer queue; PostgreSQL: row-lock waits) without increasing
dial capacity. The fix is to partition the *data* (pools), not to add workers.

## 100k agents
The first thing to break is (2) serial dispatch, then (3)+(4) per-cycle read cost and the campaign mutex, then (5) write capacity.
The fix for all of (3)-(5) is the same design move: shard by pool so each controller sees a bounded fleet (~2-5k agents).
