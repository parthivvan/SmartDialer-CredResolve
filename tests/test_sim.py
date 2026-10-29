from smartdialer.sim import Sim, Scenario, const


def test_predictive_beats_progressive_and_stays_under_cap():
    sc = Scenario("T", const(0.5), 90, "test", duration=900)
    prog = Sim(sc, "progressive").run()
    pred = Sim(sc, "predictive").run()
    assert prog["abandoned"] == 0                                   # deterministic floor
    assert pred["utilization"] > prog["utilization"] + 0.03
    assert pred["abandon_rate"] <= 0.03
    assert prog["invariant_violations"] == 0 and pred["invariant_violations"] == 0


def test_flaky_provider_duplicates_and_reordering_keep_invariants():
    sc = Scenario("T", const(0.5), 90, "flaky", primary="B", duration=600)
    r = Sim(sc, "predictive").run()
    assert r["event_dispositions"].get("DUPLICATE", 0) > 0
    assert r["invariant_violations"] == 0 and r["abandon_rate"] <= 0.03


def test_answer_rate_crash_is_safe():
    # 70% -> 10% (the interview question): over-estimating p only under-dials, so no abandonment spike
    sc = Scenario("T", lambda t: 0.7 if t < 400 else 0.1, 90, "crash", duration=700)
    r = Sim(sc, "predictive").run()
    assert r["abandon_rate"] <= 0.03 and r["invariant_violations"] == 0
