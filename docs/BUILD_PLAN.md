# How this would be built at a large company

## 0. What I did in 3 days vs what a team would do
This repo is the **walking skeleton**: correct core, simulated environment, honest gaps. A production programme adds
real providers, PostgreSQL, observability, compliance sign-off and a staged rollout. Below is that plan.

## 1. Team and roles (6-8 people, ~12 weeks to GA)
Tech lead, 2 backend engineers (dialer core, provider adapters), 1 data/ML engineer (answer-rate and talk-time models),
1 SRE/platform, 1 QA/test-automation, product owner, and a compliance/legal reviewer (part-time) who owns the abandonment policy.

## 2. Phases
| Phase | Weeks | Output | Exit gate |
|---|---|---|---|
| Discovery & requirements | 1 | Written policy: abandon cap definition, calling hours, consent/DNC rules, SLOs | Compliance signs the definition of "abandoned call" and the cap |
| Design | 2 | ADRs, data model, state machines, threat/failure model, API contracts for providers | Design review passes (security, SRE, compliance) |
| Core build (this repo, hardened) | 3-6 | State machines, atomic allocation, event handler, safety controller, reconciler on PostgreSQL | Property tests + concurrency tests green in CI; invariant checker clean under soak |
| Provider integration | 5-8 | Real adapters (signature verification, provider idempotency, rate limits), sandbox contract tests | Replay of recorded provider traffic produces identical state |
| Simulation & shadow mode | 7-10 | Simulator calibrated on real call logs; predictive runs in **shadow** (computes decisions, does not dial) | Shadow abandon forecast within tolerance of real progressive outcomes |
| Pilot | 10-11 | 1 campaign, 5% of agents, predictive on, progressive fallback armed | Abandon rate < 50% of cap for 2 weeks; no Sev-1 |
| Ramp / GA | 11-12 | 25% -> 50% -> 100% | Per-step go/no-go review with the compliance owner |

## 3. Engineering standards
* Trunk-based development, mandatory review, CI: unit + property + concurrency + simulation tests, `EXPLAIN` regression check on hot queries, load-test budget (cycle p95).
* Schema migrations are versioned and backward-compatible (expand -> migrate -> contract).
* Feature flags: **mode** (progressive/predictive/shadow) is flaggable per campaign; the *safety controller* is not.
* Config changes (cap, discount, windows) go through review + audit log; the controller refuses a cap above the compliance-approved ceiling.

## 4. Observability and SLOs
* SLIs: abandon rate (rolling, per campaign and aggregate), agent utilization, time-to-recover after provider failure, decision-cycle latency, event lag, count of reconciler repairs.
* Alerts: abandon rate >= 80% of cap (page), invariant violation > 0 (page), breaker open > 5 min, cycle p95 > 500 ms, reconciler repairs spike.
* Every dial decision stores its inputs (`safety_decisions.trace`), so "why 17 calls?" is a SQL query.
* Runbooks: provider outage, mass agent logout, abandon-rate breach, stuck-calls backlog.

## 5. Risk register (top 5)
| Risk | Mitigation |
|---|---|
| Real answer patterns violate independence assumption | Shadow mode first; feedback throttle; conservative target (half the cap); per-segment p estimates |
| Provider semantics differ from the mock (ordering, retries, idempotency) | Contract tests against sandbox; recorded-traffic replay; adapter-level normalisation |
| Compliance definition of "abandoned" differs from mine | Written sign-off in phase 0; metric implemented from their definition |
| Hot-row contention at scale | Pool sharding (see SCALE.md); load test in CI |
| Operator override pressure ("turn off the limiter, we need numbers") | No disable switch by design; the only lever is the compliance-approved cap |

## 6. Rollback
Flip the campaign to progressive (flag) - instantly safe because reserve-first needs no prediction. In-flight speculative
calls drain or are shed by the next cycle.
