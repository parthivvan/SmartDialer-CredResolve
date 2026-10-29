from smartdialer.demos import demo_worker_crash, demo_provider_outage, demo_provider_blackout, demo_agent_drop


def test_worker_crash_recovers_without_double_dial():
    r = demo_worker_crash(say=lambda *_: None)
    assert r["before"] == {"RESERVED": 3} and r["after"] == {"INITIATED": 3}
    assert r["provider_place_calls"] == 3 and r["violations"] == []


def test_provider_api_outage_trips_breaker_fails_over_and_recovers():
    r = demo_provider_outage(say=lambda *_: None)
    assert r["breaker_opened_at"] is not None and r["breaker_opened_at"] < 40
    assert r["calls_by_provider"].get("B", 0) > 0              # traffic failed over
    assert r["final_breaker"] == []                             # probe succeeded after recovery -> closed again
    assert r["borrowers_burned"] == 0 and r["violations"] == []  # infra failures never consume borrower attempts


def test_provider_blackout_reconciler_closes_stuck_calls():
    r = demo_provider_blackout(say=lambda *_: None)
    assert r["failures"].get("LOST") == r["inflight_at_outage"]
    assert r["calls_by_provider"].get("B", 0) > 0 and r["violations"] == [] and r["borrowers_burned"] == 0


def test_agent_drop_sheds_and_does_not_spike_abandonment():
    r = demo_agent_drop(say=lambda *_: None)
    d = r["decision"]
    assert d.approved == 0 and d.shed > 0
    assert r["unbound_after_one_cycle"] < r["unbound_before"]
    assert (r["abandoned_after"] - r["abandoned_before"]) <= 3
    assert r["violations"] == []
