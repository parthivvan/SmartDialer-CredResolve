# Interview prep: the questions they said they would ask

Know these cold. Each answer points at code you can open.

**Two workers reserve the same agent at the same moment. Walk us through it.**
`store.reserve_agent`: SELECT a candidate, then `UPDATE agents SET state='RESERVED' ... WHERE id=? AND state='AVAILABLE' AND version=?`
and check `rowcount`. The database serialises the two UPDATEs; the first matches, the second matches zero rows and tries
the next candidate. Proof: `tests/test_concurrency.py` (16 threads -> exactly 1 winner, also with plain autocommit, no transaction).
On top of that, `Dialer.run_cycle` takes a per-campaign lock so workers also cannot jointly over-dial from one stale snapshot.

**DB says AVAILABLE, cache says RESERVED - which wins?** There is no cache; the DB row is the only truth (ADR-1). If we added
a cache it would be a hint only, and every reservation would still go through the DB CAS.

**Provider sends ANSWERED, your worker crashes, then COMPLETED arrives.** The handler runs the event insert and state change
in one transaction. Crash before commit -> both roll back (event not recorded). COMPLETED arrives first and is applied
(call COMPLETED, agent released). The redelivered ANSWERED hits "terminal" and is stored as ignored. Final state is consistent
(`demo_out_of_order`, part 5b). If nobody redelivers, the reconciler polls `get_status` for calls with no news.

**Model predicted 70% answer rate; it drops to 10%.** Over-estimating p makes the system *under*-dial (agents idle for a bit) - a
utilization problem, not a safety problem. `stats.p_hat` blends a short window so it corrects in ~15 attempts. The dangerous
case is the opposite (10% -> 70%): the controller uses `p_up = max(long-window upper bound, short-window upper bound)` and the
abandon-rate feedback tightens/forces progressive. Test: `test_answer_rate_crash_is_safe`; scenario D.

**1,000 -> 100,000 agents: what breaks first?** See `docs/SCALE.md`. Measured: an unindexed ORDER BY on the borrower queue
(6 s/cycle at 10k agents -> 12 ms after the index). Reasoned next: serial provider dispatch, O(agents) snapshot, per-campaign
mutex, then single-primary write capacity. Fix = shard by pool, concurrent dispatch, incremental counters; "more servers" alone does not help.

**Why did the algorithm start 17 calls instead of 10?** Read `safety_decisions.trace`: pacing's formula and every input
(`A, W, C, H, T, p_hat, U`), then the controller's bound (`p_up, f_safe, target_abandon_frac, u_allowed`) and the reasons
it reduced or approved. Example query: `SELECT approved, trace FROM safety_decisions ORDER BY id DESC LIMIT 5;`

**What are you least confident about?** (1) Independence of answers; (2) the 50% discount on forecast agent availability;
(3) never ran on PostgreSQL. (ADR "least confident" section.) Saying this plainly is a strength.

**What would you do with another week?** Run it on PostgreSQL with 3 real worker processes; replace the poll loop with
a webhook endpoint; add concurrent dispatch; calibrate the simulator from real call logs; add a "please hold" greeting and 2-3 s
grace queue before abandoning an answered call.

## Live-change drills (practise these)
1. Change wrap-up from 20 s to 45 s: `Config.wrap_up_seconds`; predict the utilization effect, then run scenario B.
2. Add a third provider "C": implement `TelecomProvider`, add to `providers` dict and `Config.provider_order`; no other code changes.
3. Make abandonment count only calls that rang > 5 s: `Dialer`/`events._abandon` + `stats.compute_stats` abandon query.
4. Add a per-agent max-calls-per-hour rule: put it in `store.reserve_agent`'s candidate query.
5. Break something on purpose: delete the `AND state='AVAILABLE'` from the CAS UPDATE and watch `test_raw_cas_without_transaction_still_exclusive` fail.

## Be ready to say, without hedging
* Progressive = zero abandonment *by construction*; predictive = *bounded and measured*, not zero.
* The simulator is my own model; its numbers show the mechanism works, not what production will do.
* Where AI helped: scaffolding and test generation; where I verified: concurrency paths, safety math (property test), failure demos.
