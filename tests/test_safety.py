import ast, os, random
import pytest
from dataclasses import replace
from smartdialer.allocator import CallAllocator, UnauthorisedDecision
from smartdialer.clock import SimClock
from smartdialer.config import Config
from smartdialer.models import DialRequest, SafetyDecision, Snapshot, Stats
from smartdialer.safety import SafetyController, abandon_fraction, expected_abandons, max_unbound
from smartdialer.pacing import PredictivePacing, ProgressivePacing

SECRET = b"s" * 32
NOW = SimClock().now()


def snap(**kw):
    d = dict(now=NOW, online=100, available=10, connected=60, wrap_until=(), unbound_inflight=0, bound_inflight=0,
             eligible_borrowers=5000, provider_states={"A": "CLOSED", "B": "CLOSED"})
    d.update(kw)
    return Snapshot(**d)


def stats(**kw):
    d = dict(n=200, answered_n=100, p_hat=0.5, p_up=0.58, avg_setup=8, avg_talk=90, abandon_rate=0.0, abandon_n=300)
    d.update(kw)
    return Stats(**d)


def ctl(**cfg):
    return SafetyController(replace(Config(), **cfg), SECRET)


def test_pacing_cannot_reach_telecom():
    """Architecture test: pacing.py must not import providers, allocator, dialer, db, or the safety module."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "smartdialer", "pacing.py")).read()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0] if n.level == 0 else (n.module or ""))
            mods.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
    forbidden = {"allocator", "dialer", "db", "safety", "providers", "store", "sqlite3", "factory"}
    assert not (mods & forbidden), mods & forbidden


def test_allocator_rejects_unsealed_or_tampered_decisions():
    alloc = CallAllocator(Config(), SECRET)
    forged = SafetyDecision("APPROVE", 50, 50, 0, False, "A", (), {})            # pacing tries to authorise itself
    with pytest.raises(UnauthorisedDecision):
        alloc.execute(None, forged, NOW)
    real = ctl().review(DialRequest("predictive", 5, {}), snap(), stats())
    tampered = SafetyDecision(**{**real.__dict__, "approved": real.approved + 100})
    with pytest.raises(UnauthorisedDecision):
        alloc.execute(None, tampered, NOW)
    with pytest.raises(UnauthorisedDecision):                                      # sealed with a different secret
        CallAllocator(Config(), b"x" * 32).execute(None, real, NOW)


def test_cold_start_falls_back_to_progressive_and_binds_agents():
    d = ctl().review(DialRequest("predictive", 40, {}), snap(available=7), stats(n=5))
    assert d.action == "FALLBACK_PROGRESSIVE" and d.bind_agents and d.approved <= 7 and "COLD_START" in d.reasons


def test_abandon_rate_near_cap_forces_progressive():
    d = ctl().review(DialRequest("predictive", 40, {}), snap(), stats(abandon_rate=0.026, abandon_n=300))
    assert d.action == "FALLBACK_PROGRESSIVE" and d.bind_agents and "ABANDON_RATE_NEAR_CAP" in d.reasons


def test_fallback_never_reserves_agents_that_ringing_speculative_calls_need():
    d = ctl().review(DialRequest("predictive", 40, {}), snap(available=10, unbound_inflight=20),
                     stats(abandon_rate=0.03, abandon_n=300, p_up=0.6))
    assert d.approved <= 10 - 12 + 0 or d.approved == 0     # 20 * 0.6 = 12 agents needed > 10 available
    assert d.shed > 0


def test_outside_calling_hours_rejected():
    from datetime import datetime
    from smartdialer.clock import IST
    night = datetime(2026, 10, 29, 22, 0, tzinfo=IST).timestamp()
    d = ctl().review(DialRequest("predictive", 10, {}), snap(now=night), stats())
    assert d.action == "REJECT" and d.approved == 0 and "OUTSIDE_CALLING_HOURS" in d.reasons


def test_all_providers_open_rejects():
    d = ctl().review(DialRequest("progressive", 10, {}), snap(provider_states={"A": "OPEN", "B": "OPEN"}), stats())
    assert d.action == "REJECT" and "NO_HEALTHY_PROVIDER" in d.reasons


def test_failover_to_second_provider_and_half_open_probe():
    d = ctl().review(DialRequest("progressive", 10, {}), snap(provider_states={"A": "OPEN", "B": "CLOSED"}), stats())
    assert d.provider == "B"
    d = ctl().review(DialRequest("progressive", 10, {}), snap(provider_states={"A": "HALF_OPEN", "B": "CLOSED"}), stats())
    assert d.provider == "B" and d.probe_provider == "A"
    d = ctl().review(DialRequest("predictive", 30, {}), snap(provider_states={"A": "HALF_OPEN", "B": "OPEN"}), stats())
    assert d.approved <= 1 and d.bind_agents


def test_progressive_never_exceeds_available_agents():
    for avail in (0, 1, 7, 50):
        d = ctl().review(DialRequest("progressive", 999, {}), snap(available=avail), stats())
        assert d.approved <= avail and d.bind_agents


def test_expected_abandons_math():
    assert expected_abandons(5, 0.5, 5) == 0.0
    # n=2,p=.5,f=0 -> E[X]=1
    assert expected_abandons(2, 0.5, 0) == pytest.approx(1.0, rel=1e-6)
    assert abandon_fraction(40, 0.5, 20) > abandon_fraction(30, 0.5, 20)


def test_property_approved_never_breaks_exposure_bound():
    """Randomised property test: for any snapshot/stats the controller approves only calls whose resulting
    speculative exposure keeps expected-abandon-fraction <= target (or binds agents)."""
    rnd = random.Random(1)
    c = ctl()
    for _ in range(400):
        s = snap(online=rnd.randint(5, 400), available=rnd.randint(0, 30), connected=rnd.randint(0, 300),
                 unbound_inflight=rnd.randint(0, 80), wrap_until=tuple(NOW + rnd.uniform(0, 30) for _ in range(rnd.randint(0, 10))))
        st = stats(p_hat=rnd.uniform(0.05, 0.9), p_up=rnd.uniform(0.1, 1.0), n=rnd.randint(0, 300),
                   abandon_rate=rnd.uniform(0, 0.04), abandon_n=rnd.randint(0, 400))
        st = replace(st, p_up=max(st.p_up, st.p_hat))
        req = DialRequest("predictive", rnd.randint(0, 300), {})
        d = c.review(req, s, st)
        assert d.approved >= 0 and d.approved <= max(req.n, s.available)
        if d.action in ("APPROVE", "REDUCE") and not d.bind_agents and d.approved > 0:
            f_safe = d.detail["f_safe"]
            total = s.unbound_inflight + d.approved
            assert total <= int(c.cfg.max_unbound_ratio * max(s.online, 1))
            assert abandon_fraction(total, st.p_up, f_safe) <= d.detail["target_abandon_frac"] + 6e-5  # detail is rounded to 4dp
        if d.bind_agents:
            assert d.approved <= s.available


def test_exposure_shrinks_as_answer_rate_upper_bound_rises():
    a = max_unbound(30, 0.4, 0.015, 500)
    b = max_unbound(30, 0.8, 0.015, 500)
    assert b < a


def test_pacing_decision_trace_explains_the_number():
    cfg = Config()
    req = PredictivePacing(cfg).propose(snap(available=5, connected=50, unbound_inflight=4, wrap_until=(NOW + 2, NOW + 3)),
                                        stats(p_hat=0.4, avg_setup=10, avg_talk=100))
    t = req.trace
    assert t["A"] == 5 and t["W"] == 2 and t["U"] == 4
    assert req.n == int((t["F"] / 0.4) - 4)            # N = floor(F/p - U)
    assert "formula" in t
    assert ProgressivePacing(cfg).propose(snap(available=9), stats()).n == 9
