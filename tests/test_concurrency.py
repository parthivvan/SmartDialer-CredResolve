import threading
from smartdialer import store
from smartdialer.demos import demo_double_reserve_race
from smartdialer.factory import build_system, seed
from smartdialer.clock import SimClock
from smartdialer.config import Config
from smartdialer.providers.mock import provider_a, provider_b


def test_sixteen_workers_one_agent_exactly_one_winner():
    r = demo_double_reserve_race(16, say=lambda *_: None)
    assert len(r["winners"]) == 1 and r["agent"][0] == "RESERVED"


def test_raw_cas_without_transaction_still_exclusive():
    """CAS alone (autocommit, no BEGIN IMMEDIATE) must be enough - this is what protects us on PostgreSQL."""
    clock = SimClock()
    s = build_system("progressive", Config(), clock, {"A": provider_a(clock), "B": provider_b(clock)})
    seed(s, agents=1, borrowers=1)
    wins, barrier = [], threading.Barrier(12)

    def w(i):
        c = s.db.conn()
        barrier.wait()
        if store.reserve_agent(c, "c1", f"k{i}", clock.now(), clock.now() + 30, 15):
            wins.append(i)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1


def test_concurrent_workers_never_overallocate_agents_or_borrowers():
    clock = SimClock()
    s = build_system("progressive", Config(), clock, {"A": provider_a(clock), "B": provider_b(clock)})
    seed(s, agents=20, borrowers=200)
    barrier = threading.Barrier(8)
    workers = [s.new_worker(f"w{i}") for i in range(8)]

    def run(d):
        barrier.wait()
        for _ in range(3):
            d.run_cycle()
    ts = [threading.Thread(target=run, args=(d,)) for d in workers]
    [t.start() for t in ts]; [t.join() for t in ts]
    with s.db.tx() as c:
        n_calls = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        reserved_b = c.execute("SELECT COUNT(DISTINCT borrower_id) FROM calls").fetchone()[0]
        assert store.check_invariants(c) == []
    assert n_calls == 20            # one call per agent, no matter how many workers raced
    assert reserved_b == 20         # no borrower dialed twice


def test_duplicate_dial_job_is_rejected_by_idempotency_key():
    import sqlite3, pytest
    clock = SimClock()
    s = build_system("progressive", Config(), clock, {"A": provider_a(clock), "B": provider_b(clock)})
    seed(s, agents=1, borrowers=1)
    s.dialer.run_cycle()
    with s.db.tx() as c:
        row = c.execute("SELECT * FROM calls").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO calls(id, campaign_id, borrower_id, provider, state, bound, idem_key, created_at) "
                      "VALUES('dup','c1',?,'A','RESERVED',0,?,0)", (row["borrower_id"], row["idem_key"]))
