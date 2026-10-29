# SmartDialer

A working prototype of a collections dialer that gets most of **predictive** dialing's utilization gain while keeping
**progressive** dialing's deterministic safety floor.

```
Campaign -> Pacing Engine (Progressive | Predictive) -> Safety Controller -> Call Allocator -> Telecom Provider
              (pure function, can only ASK)            (only authority)     (needs sealed decision)
```

* Python 3.10+, **standard library only** (SQLite). `pytest` for tests. No Redis/Kafka/microservices - see `docs/ADR.md` for why.
* Same code runs against a wall clock (production) and a virtual clock (simulator).

## Run it

```bash
python -m pip install pytest
python -m pytest -q                      # 39 tests (the simulation tests may take about a minute)
python -m smartdialer.demos              # the 5 required failure cases + 2 extras, narrated
python -m smartdialer.sim                # scenarios A-E x {progressive, predictive} -> results/simulation.{md,json}
python -m smartdialer.loadtest           # 100 / 1,000 / 10,000 agents -> results/loadtest.{md,json}
```

## What's where

| Path | Purpose |
|---|---|
| `smartdialer/states.py` | Agent + call state machines, transition table, monotonic call rank |
| `smartdialer/store.py` | Every state mutation as a compare-and-set; invariant checker |
| `smartdialer/pacing.py` | `ProgressivePacing`, `PredictivePacing` (pure functions, no telecom access) |
| `smartdialer/safety.py` | `SafetyController`: hard gates, exposure bound, feedback throttle, fallback, HMAC-sealed decision |
| `smartdialer/allocator.py` | Executes only sealed decisions; reserves agents/borrowers atomically |
| `smartdialer/events.py` | Idempotent, order-insensitive provider-event handler |
| `smartdialer/reconciler.py` | Crash/lag recovery (redrive, status polling, orphan repair) |
| `smartdialer/health.py` | Shared circuit breaker with half-open probing |
| `smartdialer/providers/` | `TelecomProvider` interface; mock A (fast/reliable) and B (slow/timeouts/dupes/reordering/outage) |
| `smartdialer/demos.py` | Executable failure scenarios (tests assert on them) |
| `smartdialer/sim.py`, `loadtest.py` | Simulation and load test |
| `docs/` | `ARCHITECTURE.md` (diagrams), `ADR.md`, `SCALE.md`, `FINAL_ANSWER.md`, `BUILD_PLAN.md`, `INTERVIEW_PREP.md` |

## Reviewer quick path

If you have five minutes, run the commands below in this order. They exercise the parts of the
prototype that are easiest to get subtly wrong: concurrent allocation, unreliable provider events,
the non-bypassable safety boundary, and recovery after failures.

```bash
python -m pytest -q
python -m smartdialer.demos
python -m smartdialer.sim
```

| Assignment requirement | Evidence in this repository |
|---|---|
| Progressive and predictive dialing | `smartdialer/pacing.py`, `smartdialer/dialer.py` |
| Separate, non-bypassable safety controller | `smartdialer/safety.py`, HMAC decision check in `smartdialer/allocator.py`, `tests/test_safety.py` |
| Agent and call lifecycles | `smartdialer/states.py`, diagrams in `docs/ARCHITECTURE.md` |
| Concurrent agent/borrower allocation | compare-and-set operations in `smartdialer/store.py`, `tests/test_concurrency.py` |
| Duplicate and out-of-order provider events | `smartdialer/events.py`, `tests/test_events.py` |
| Two distinct mock providers and outage handling | `smartdialer/providers/`, `smartdialer/health.py`, `tests/test_failures.py` |
| Required failure cases | `python -m smartdialer.demos` and `results/demos_output.txt` |
| Simulation and scaling analysis | `smartdialer/sim.py`, `smartdialer/loadtest.py`, `results/`, `docs/SCALE.md` |
| Architecture decisions and final short answer | `docs/ADR.md`, `docs/FINAL_ANSWER.md` |

## Headline results (simulation, 100 agents, 30 virtual minutes; abandon cap = 3%)

| Scenario | Progressive util. | Predictive util. | Predictive abandon rate |
|---|---|---|---|
| A: answer 20%, talk 120s | 49.3% | **70.0%** | 0.00% |
| B: answer 50%, talk 90s | 63.5% | **74.4%** | 0.59% |
| C: answer 70%, talk 180s | 81.9% | 84.4% | 2.23% |
| D: changing answer rate + provider outage + 40 agents drop | 61.4% | **66.9%** | 1.03% |
| E: flaky provider (timeouts, dup + reordered events) | 56.6% | **68.7%** | 1.61% |

Progressive abandoned **zero** calls in every scenario (by construction); no scenario produced an invariant violation.
Full table, timelines and cap sensitivity: `results/simulation.md`. These numbers come from a simulator I wrote with
assumed provider behaviour, so treat them as evidence the *mechanism* works, not as production forecasts.

## Known limitations (honest list)

* SQLite is a single-writer store; the design maps to PostgreSQL (see ADR) but I have **not** run it on PostgreSQL.
* Provider adapters are mocks. A real adapter needs webhook signature checks, provider-side idempotency support, and
  its own timeout tuning.
* The safety model assumes answers are independent across borrowers and uses an exponential-hazard approximation for
  talk time. Real campaigns have correlated answer bursts; the abandonment feedback loop (G3) is the backstop.
* Predictive mode can still abandon a call (bounded statistically, measured, capped). Only the fallback mode is a hard zero.
* SQLite runs with `synchronous=OFF` for speed (no durability); production would use durable commits.
* Hangup is best-effort via an outbox; an abandoned borrower may hear silence for a few seconds. No "please hold" greeting.
