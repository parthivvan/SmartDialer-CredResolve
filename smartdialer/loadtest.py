"""Micro load tests that locate the first bottleneck.  python -m smartdialer.loadtest  -> results/loadtest.md

1. cycle cost vs fleet size (100 / 1,000 / 10,000 agents), split into phases
2. agent-reservation contention across threads (correctness + throughput)
3. provider-event ingestion throughput
"""
import json
import os
import statistics
import threading
import time
from dataclasses import replace

from . import store
from .clock import SimClock
from .config import Config
from .factory import build_system, seed
from .providers.mock import provider_a, provider_b
from . import dialer as dialer_mod
from .providers.base import ProviderEvent


def _timed(obj, name, bucket):
    fn = getattr(obj, name)

    def w(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            bucket.setdefault(name, []).append((time.perf_counter() - t) * 1000)
    setattr(obj, name, w)


def cycle_cost(n_agents, cycles=12):
    clock = SimClock()
    cfg = Config(max_new_calls_per_cycle=500)
    s = build_system("predictive", cfg, clock, {"A": provider_a(clock, answer_fn=lambda t: 0.5), "B": provider_b(clock)})
    seed(s, agents=n_agents, borrowers=60000)
    now = clock.now()
    with s.db.tx() as c:   # realistic mix: 30% free, 50% on call, 20% wrap-up
        c.execute("UPDATE agents SET state='CONNECTED', call_id='x' WHERE rowid % 10 IN (0,1,2,3,4)")
        c.execute("UPDATE agents SET state='WRAP_UP', wrap_until=? + (rowid % 20) WHERE rowid % 10 IN (5,6)", (now,))
        c.execute("UPDATE agents SET last_seen=?", (now,))
    d = s.dialer
    from . import stats as stats_mod
    b = {}
    _timed(d, "_snapshot", b)
    _timed(d.pacing, "propose", b)
    _timed(d.safety, "review", b)
    _timed(d.allocator, "execute", b)
    _timed(d, "dispatch", b)
    tot, created = [], 0
    for _ in range(cycles):
        clock.advance(1)
        with s.db.tx() as c:
            c.execute("UPDATE agents SET last_seen=?", (clock.now(),))
        t = time.perf_counter()
        _, ids = d.run_cycle()
        tot.append((time.perf_counter() - t) * 1000)
        created += len(ids)
    with s.db.tx() as c:
        t = time.perf_counter()
        stats_mod.compute_stats(c, cfg)
        stats_ms = (time.perf_counter() - t) * 1000
    med = lambda k: round(statistics.median(b[k]), 2) if b.get(k) else 0
    return {"agents": n_agents, "cycle_ms_p50": round(statistics.median(tot), 1), "cycle_ms_max": round(max(tot), 1),
            "snapshot_ms": med("_snapshot"), "pacing_ms": med("propose"), "safety_ms": med("review"),
            "stats_ms": round(stats_ms, 2), "allocate_ms_per_cycle": med("execute"),
            "dispatch_ms_per_call": med("dispatch"), "calls_created": created}


def reservation_contention(n_agents=2000, threads=8):
    clock = SimClock()
    s = build_system("progressive", Config(), clock, {"A": provider_a(clock), "B": provider_b(clock)})
    seed(s, agents=n_agents, borrowers=1)
    got, lock, barrier = [], threading.Lock(), threading.Barrier(threads)

    def w(i):
        mine = []
        barrier.wait()
        while True:
            with s.db.tx() as c:
                a = store.reserve_agent(c, "c1", f"t{i}-{len(mine)}", clock.now(), clock.now() + 30, 15)
            if a is None:
                break
            mine.append(a)
        with lock:
            got.extend(mine)
    t0 = time.perf_counter()
    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]; [t.join() for t in ts]
    dt = time.perf_counter() - t0
    return {"agents": n_agents, "threads": threads, "reserved": len(got), "unique": len(set(got)),
            "double_allocations": len(got) - len(set(got)), "reservations_per_sec": round(len(got) / dt)}


def event_throughput(n=6000):
    clock = SimClock()
    s = build_system("progressive", Config(), clock, {"A": provider_a(clock), "B": provider_b(clock)})
    seed(s, agents=1, borrowers=n)
    with s.db.tx() as c:
        c.executemany("INSERT INTO calls(id, campaign_id, borrower_id, provider, state, bound, idem_key, created_at) "
                      "VALUES(?, 'c1', ?, 'A', 'INITIATED', 0, ?, 0)", [(f"k{i}", f"b{i:06d}", f"i{i}") for i in range(n)])
    evs = [ProviderEvent("A", f"k{i}", "RINGING", 0.0) for i in range(n)]
    t0 = time.perf_counter()
    for e in evs:
        s.handler.handle(e)
    dt = time.perf_counter() - t0
    return {"events": n, "events_per_sec_single_thread": round(n / dt)}


def run(out="results"):
    os.makedirs(out, exist_ok=True)
    res = {"cycle_cost": [cycle_cost(n) for n in (100, 1000, 10000)],
           "reservation_contention": reservation_contention(), "event_throughput": event_throughput()}
    with open(os.path.join(out, "loadtest.json"), "w") as f:
        json.dump(res, f, indent=1)
    L = ["# Load test results (SQLite, single machine, mock providers with zero network latency)\n",
         "## Cycle cost vs fleet size (median of 12 cycles; ms)\n",
         "| agents | cycle total p50 | snapshot | stats | pacing | safety | allocate (per cycle) | dispatch (per call) | calls/cycle |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in res["cycle_cost"]:
        L.append(f"| {r['agents']:,} | {r['cycle_ms_p50']} | {r['snapshot_ms']} | {r['stats_ms']} | {r['pacing_ms']} | "
                 f"{r['safety_ms']} | {r['allocate_ms_per_cycle']} | {r['dispatch_ms_per_call']} | {r['calls_created'] // 12} |")
    rc, ev = res["reservation_contention"], res["event_throughput"]
    L += [f"\n## Reservation contention\n{rc['threads']} threads draining {rc['agents']} agents: reserved={rc['reserved']}, "
          f"unique={rc['unique']}, **double allocations={rc['double_allocations']}**, {rc['reservations_per_sec']}/s.",
          f"\n## Event ingestion\n{ev['events']} provider events, one thread: {ev['events_per_sec_single_thread']} events/s "
          f"(each = 1 dedupe insert + 1-2 row updates in one transaction)."]
    with open(os.path.join(out, "loadtest.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    run()
