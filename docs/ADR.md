# Architecture decisions

Format: What / Why / What it solves / What it makes harder.

## ADR-1: One relational DB as the only source of truth (no Redis, Kafka, microservices)
* **What:** SQLite in the prototype, PostgreSQL in production. Agents, borrowers, calls, events, breaker state all live in it.
* **Why:** every hard requirement here is a *consistency* problem (no double allocation, idempotent events, crash recovery).
  A transactional DB gives atomic compare-and-set, unique constraints and one place to look. A cache/queue adds a second
  copy of agent state - which is exactly the "DB says AVAILABLE, cache says RESERVED" question. With no cache the question cannot arise.
* **Solves:** correctness, debuggability, easy replay (events are rows).
* **Harder:** write throughput is bounded by one primary; see `docs/SCALE.md` for the measured first bottlenecks and the sharding plan.

## ADR-2: Compare-and-set on every transition, not locks held across I/O
* **What:** `UPDATE ... WHERE state=<expected> [AND version=<seen>]`, check rowcount. Network calls happen outside transactions.
* **Why:** the CAS is correct under any isolation level; the winner is decided by the database, never by process memory.
  Holding a lock while waiting on a telecom provider would make provider latency a global stall.
* **Harder:** callers must handle "lost the race" (retry next candidate) and leases for crash recovery.

## ADR-3: Per-campaign mutex around snapshot -> decide -> reserve
* **What:** each cycle updates the campaign row first (SQLite: write lock; PostgreSQL: `SELECT ... FOR UPDATE`).
* **Why:** without it two workers read the same snapshot ("I can start 15 calls") and jointly start 30. Pacing decisions are
  read-modify-write over shared state and need serialising per campaign. Per-agent CAS alone does not prevent *over-dialing*.
* **Harder:** one decision cycle per campaign at a time. Cycles are ~1 ms at 1,000 agents so this is cheap; shard by campaign/pool beyond that.

## ADR-4: Safety Controller is the only authority; decisions are HMAC-sealed
* **What:** pacing returns a `DialRequest`; only the controller can produce a `SafetyDecision` the allocator accepts.
  There is no "enabled" flag anywhere. An architecture test asserts `pacing.py` imports nothing that can reach telecom.
* **Why:** "the predictive algorithm should not have a way to switch the safety mechanism off" - make it structurally impossible, not a convention.
* **Honest limit:** this defends against bugs and careless code in the same process, not against a malicious engineer with the secret.

## ADR-5: Reserve-first hybrid instead of pure predictive
* **What:** the controller has two outputs. `bind_agents=True` reserves an agent per call before dialing (progressive, cannot
  abandon). `bind_agents=False` lets a *bounded* number of speculative calls ring and claims an agent on answer.
* **Why:** it gives a hard floor (G1) and a measurable, capped risk above it (G2/G3), and the system degrades to the floor
  automatically (cold start, abandon rate near cap, provider half-open).
* **Harder:** two code paths in the event handler (agent pre-bound vs claimed on answer).

## ADR-6: Monotonic call state + dedupe table instead of per-provider event logic
* **What:** `CALL_RANK`; events apply only forward; `UNIQUE(provider|call_ref|event_type)`.
* **Why:** duplicates and reordering are handled by two generic rules, so a new provider with different quirks needs no new code.
* **Harder:** information in a late event can be lost (e.g. ANSWERED arriving after COMPLETED does not backfill `connected_at`).

## ADR-7: Our `call_ref` is the provider key; side effects go through an outbox
* **What:** `place_call/hangup/get_status` take our call id. Hangups are rows in `outbox` retried until done.
* **Why:** a timeout is ambiguous (the call may exist). Idempotent place + defensive hangup + status polling resolves it
  without guessing. Events can be matched before we have stored the provider's own id.

## ADR-8: Leases + reconciler instead of distributed failure detection
* **What:** reservations carry lease times; any worker runs the reconciler; every repair is idempotent and CAS-guarded.
* **Why:** workers need no membership protocol. A crashed worker's work is recovered by whoever sees the expired lease first.
* **Harder:** recovery latency is lease length (30s default) - tunable, and a slow-but-alive worker may race the reconciler; idempotency makes that harmless.

## What I am least confident about
1. **The safety model's independence assumption.** Real answer events cluster (shift ends, salary dates). The feedback loop contains it, but I would shadow-run it on real traffic before trusting the numbers.
2. **`F_safe`'s 50% discount on forecast talk-endings** is a judgement call, not a derived value. The cap-sensitivity run shows the controller is conservative (raising the cap from 3% to 5% bought ~0.6 pp utilization), so there is headroom to tune.
3. **Not tested on PostgreSQL.** The SQL is written to be portable (CAS, no RETURNING, no SQLite-only functions in hot paths) but unverified.
