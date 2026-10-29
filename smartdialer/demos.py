"""Executable failure-case demos. `python -m smartdialer.demos` prints a narrative; tests assert on the returned facts."""
import threading
from dataclasses import replace

from .config import Config
from .dialer import WorkerCrash
from .factory import build_system, seed
from .providers.base import ProviderEvent
from .providers.mock import provider_a, provider_b
from .clock import SimClock
from . import store

_say = print


def _q(system, sql, *a):
    with system.db.tx() as c:
        return [tuple(r) for r in c.execute(sql, a).fetchall()]


def _quiet_providers(clock):
    return {"A": provider_a(clock, answer_fn=lambda t: 0.5, fail_rate=0.0), "B": provider_b(clock)}


# 1 ---------------------------------------------------------------------------------------------
def demo_worker_crash(say=_say):
    clock = SimClock()
    s = build_system("progressive", Config(), clock, _quiet_providers(clock))
    seed(s, agents=3, borrowers=20)
    s.dialer.fault = "after_place"
    say("1. WORKER CRASH: agents+borrowers reserved, call row committed, worker dies right after place_call()")
    try:
        s.dialer.run_cycle()
    except WorkerCrash:
        pass
    before = dict(_q(s, "SELECT state, COUNT(*) FROM calls GROUP BY state"))
    agents_before = dict(_q(s, "SELECT state, COUNT(*) FROM agents GROUP BY state"))
    say(f"   after crash : calls={before} agents={agents_before} provider.place_calls={s.providers['A'].stats['place_calls']}")
    clock.advance(s.cfg.reservation_lease + 1)
    out = s.reconciler.run_once()
    after = dict(_q(s, "SELECT state, COUNT(*) FROM calls GROUP BY state"))
    placed = s.providers["A"].stats["place_calls"]
    say(f"   reconciler  : {out}  calls={after} provider.place_calls={placed} (no double dial)")
    with s.db.tx() as c:
        viol = store.check_invariants(c)
    return {"before": before, "after": after, "agents_before": agents_before, "provider_place_calls": placed,
            "violations": viol, "reconciler": out}


# 2 ---------------------------------------------------------------------------------------------
def _outage_run(api_only, say):
    clock = SimClock()
    s = build_system("progressive", Config(), clock, _quiet_providers(clock))
    seed(s, agents=12, borrowers=400)
    A = s.providers["A"]
    s.dialer.run_cycle()
    s.pump_events()
    inflight = _q(s, "SELECT COUNT(*) FROM calls WHERE state IN ('INITIATED','RINGING','CONNECTED','ANSWERED')")[0][0]
    if api_only:
        A.api_outage = True
    else:
        A.outage = True
    opened = None
    for t in range(1, 240):
        clock.advance(1)
        with s.db.tx() as c:
            c.execute("UPDATE agents SET last_seen=? WHERE state!='OFFLINE'", (clock.now(),))   # agent heartbeats
        s.pump_events()
        if t % 5 == 0:
            s.reconciler.run_once()
        s.dialer.run_cycle()
        if opened is None and _q(s, "SELECT 1 FROM breaker WHERE provider='A'"):
            opened = t
        if t == 120:
            A.outage = A.api_outage = False        # provider recovers
    with s.db.tx() as c:
        viol = store.check_invariants(c)
    return {"breaker_opened_at": opened, "inflight_at_outage": inflight,
            "calls_by_provider": dict(_q(s, "SELECT provider, COUNT(*) FROM calls GROUP BY provider")),
            "failures": dict(_q(s, "SELECT fail_reason, COUNT(*) FROM calls WHERE state='FAILED' GROUP BY fail_reason")),
            "completed_or_connected": _q(s, "SELECT COUNT(*) FROM calls WHERE answered_at IS NOT NULL")[0][0],
            "borrowers_burned": _q(s, "SELECT COUNT(*) FROM borrowers WHERE state='EXHAUSTED'")[0][0],
            "final_breaker": _q(s, "SELECT state FROM breaker WHERE provider='A'"),
            "agents": dict(_q(s, "SELECT state, COUNT(*) FROM agents GROUP BY state")), "violations": viol}


def demo_provider_outage(say=_say):
    say("2a. PROVIDER API OUTAGE: provider A's place_call() times out from t=0 (webhooks for existing calls still flow)")
    r = _outage_run(True, say)
    say(f"    {r}")
    return r


def demo_provider_blackout(say=_say):
    say("2b. PROVIDER BLACKOUT: provider A goes completely dark with calls in flight (no API, no webhooks) for 120s")
    r = _outage_run(False, say)
    say(f"    {r}")
    return r


# 3 ---------------------------------------------------------------------------------------------
def demo_agent_drop(say=_say):
    from .sim import Sim, Scenario, const
    sc = Scenario("drop", const(0.5), 90, "agent drop demo", duration=900)
    sim = Sim(sc, "predictive")
    say("3. AGENT AVAILABILITY DROP: 100 agents, steady predictive dialing, then 40 vanish in one second")
    for t in range(600):
        sim.step(t)
    snap_before = _q(sim.system, "SELECT COUNT(*) FROM calls WHERE bound=0 AND state IN ('RESERVED','INITIATED','RINGING')")[0][0]
    ab_before = _q(sim.system, "SELECT COUNT(*) FROM calls WHERE outcome='ABANDONED'")[0][0]
    sim.drop_agents(40)
    dec = sim.step(600)
    unb_after = _q(sim.system, "SELECT COUNT(*) FROM calls WHERE bound=0 AND state IN ('RESERVED','INITIATED','RINGING')")[0][0]
    say(f"   speculative calls ringing: {snap_before} -> {unb_after} one cycle later; decision={dec.action} "
        f"shed={dec.shed} approved={dec.approved} reasons={dec.reasons}")
    for t in range(601, 800):
        sim.step(t)
    ab_after = _q(sim.system, "SELECT COUNT(*) FROM calls WHERE outcome='ABANDONED'")[0][0]
    answered = _q(sim.system, "SELECT COUNT(*) FROM calls WHERE answered_at IS NOT NULL")[0][0]
    say(f"   abandoned calls before drop={ab_before}, 200s after={ab_after} (overall rate {ab_after / max(answered, 1):.2%})")
    return {"unbound_before": snap_before, "unbound_after_one_cycle": unb_after, "decision": dec,
            "abandoned_before": ab_before, "abandoned_after": ab_after, "answered": answered,
            "violations": sim.violations}


# 4 + 5 -----------------------------------------------------------------------------------------
def _one_call_system():
    clock = SimClock()
    s = build_system("progressive", Config(), clock, _quiet_providers(clock))
    seed(s, agents=1, borrowers=1)
    s.dialer.run_cycle()
    call = _q(s, "SELECT id FROM calls")[0][0]
    return s, clock, call


def _ev(call, typ, t=0.0):
    return ProviderEvent("A", call, typ, t)


def demo_duplicate_events(say=_say):
    s, clock, call = _one_call_system()
    say("4. DUPLICATE EVENTS: provider sends ANSWERED, ANSWERED, ANSWERED, COMPLETED, COMPLETED")
    disp = [s.handler.handle(_ev(call, t)) for t in ("ANSWERED", "ANSWERED", "ANSWERED", "COMPLETED", "COMPLETED")]
    transitions = _q(s, "SELECT COUNT(*) FROM call_events WHERE disposition='APPLIED'")[0][0]
    say(f"   dispositions={disp}; state transitions applied={transitions}")
    return {"dispositions": disp, "applied": transitions,
            "call": _q(s, "SELECT state, outcome FROM calls")[0], "agent": _q(s, "SELECT state FROM agents")[0][0]}


def demo_out_of_order(say=_say):
    s, clock, call = _one_call_system()
    say("5a. OUT OF ORDER: COMPLETED, ANSWERED, RINGING")
    disp = [s.handler.handle(_ev(call, t)) for t in ("COMPLETED", "ANSWERED", "RINGING")]
    r1 = {"dispositions": disp, "call": _q(s, "SELECT state FROM calls")[0][0], "agent": _q(s, "SELECT state FROM agents")[0][0]}
    say(f"   {r1}")
    # 5b: crash after ANSWERED is applied but before commit
    s2, clock2, call2 = _one_call_system()
    say("5b. CRASH AFTER ANSWERED: handler applies ANSWERED then dies before COMMIT; COMPLETED arrives; ANSWERED is redelivered")
    orig = s2.handler._apply

    def crashing(c, ev, now):
        orig(c, ev, now)
        raise WorkerCrash("after ANSWERED applied, before commit")
    s2.handler._apply = crashing
    try:
        s2.handler.handle(_ev(call2, "ANSWERED"))
    except WorkerCrash:
        pass
    s2.handler._apply = orig
    after_crash = {"call": _q(s2, "SELECT state FROM calls")[0][0], "events": _q(s2, "SELECT COUNT(*) FROM call_events")[0][0]}
    d2 = [s2.handler.handle(_ev(call2, "COMPLETED")), s2.handler.handle(_ev(call2, "ANSWERED"))]
    r2 = {"after_crash": after_crash, "then": d2, "call": _q(s2, "SELECT state FROM calls")[0][0],
          "agent": _q(s2, "SELECT state FROM agents")[0][0]}
    say(f"   {r2}")
    with s2.db.tx() as c:
        viol = store.check_invariants(c)
    return {"ooo": r1, "crash": r2, "violations": viol}


# 6 ---------------------------------------------------------------------------------------------
def demo_agent_disappears_during_setup(say=_say):
    s, clock, call = _one_call_system()
    say("6. AGENT DISAPPEARS DURING CALL SETUP: agent logs out while the call is ringing")
    s.handler.handle(_ev(call, "RINGING"))
    with s.db.tx() as c:
        res = store.agent_offline(c, "ag0000", clock.now(), s.cfg)
    s.dialer.process_outbox()
    r = {"result": res, "call": _q(s, "SELECT state, fail_reason FROM calls")[0],
         "borrower": _q(s, "SELECT state, attempts FROM borrowers")[0], "agent": _q(s, "SELECT state FROM agents")[0][0],
         "hangup_sent": _q(s, "SELECT done FROM outbox")[0][0]}
    say(f"   {r}")
    late = s.handler.handle(_ev(call, "ANSWERED"))
    r["late_answered_disposition"] = late
    return r


# 7 ---------------------------------------------------------------------------------------------
def demo_double_reserve_race(threads=16, say=_say):
    clock = SimClock()
    s = build_system("progressive", Config(), clock, _quiet_providers(clock))
    seed(s, agents=1, borrowers=1)
    winners, barrier = [], threading.Barrier(threads)

    def worker(i):
        barrier.wait()
        with s.db.tx() as c:
            got = store.reserve_agent(c, s.cfg.campaign_id, f"call-{i}", clock.now(), clock.now() + 30, 15)
        if got:
            winners.append(i)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    say(f"7. RACE: {threads} workers try to reserve the same single agent -> winners={winners}")
    return {"winners": winners, "agent": _q(s, "SELECT state, call_id FROM agents")[0]}


ALL = [demo_worker_crash, demo_provider_outage, demo_provider_blackout, demo_agent_drop, demo_duplicate_events, demo_out_of_order,
       demo_agent_disappears_during_setup, demo_double_reserve_race]

if __name__ == "__main__":
    for d in ALL:
        d()
        print()
