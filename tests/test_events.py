import pytest
from smartdialer.demos import (demo_duplicate_events, demo_out_of_order, demo_agent_disappears_during_setup, _one_call_system, _ev, _q)


def test_duplicate_events_create_one_transition():
    r = demo_duplicate_events(say=lambda *_: None)
    assert r["dispositions"] == ["APPLIED", "DUPLICATE", "DUPLICATE", "APPLIED", "DUPLICATE"]
    assert r["applied"] == 2 and r["call"][0] == "COMPLETED" and r["agent"] == "WRAP_UP"


def test_out_of_order_completed_answered_ringing():
    r = demo_out_of_order(say=lambda *_: None)["ooo"]
    assert r["dispositions"] == ["APPLIED", "IGNORED_AFTER_TERMINAL", "IGNORED_AFTER_TERMINAL"]
    assert r["call"] == "COMPLETED" and r["agent"] == "AVAILABLE"


def test_crash_after_answered_rolls_back_then_recovers_consistently():
    r = demo_out_of_order(say=lambda *_: None)
    c = r["crash"]
    assert c["after_crash"] == {"call": "INITIATED", "events": 0}      # whole tx rolled back, event not recorded
    assert c["then"] == ["APPLIED", "IGNORED_AFTER_TERMINAL"] and c["call"] == "COMPLETED"
    assert r["violations"] == []


def test_event_before_provider_id_is_stored_monotonic_dispatch_does_not_regress():
    s, clock, call = _one_call_system()
    # event for a call we still think is only RESERVED/INITIATED must advance it, never regress later
    assert s.handler.handle(_ev(call, "RINGING")) == "APPLIED"
    assert s.handler.handle(_ev(call, "ANSWERED")) == "APPLIED"
    assert s.handler.handle(_ev(call, "RINGING")) == "DUPLICATE"
    assert _q(s, "SELECT state FROM calls")[0][0] == "CONNECTED"
    s.dialer.dispatch(call)                                         # late redispatch must be a no-op
    assert _q(s, "SELECT state FROM calls")[0][0] == "CONNECTED"


def test_unknown_call_and_contradictory_failure_are_ignored():
    s, clock, call = _one_call_system()
    assert s.handler.handle(_ev("nope", "ANSWERED")) == "UNKNOWN_CALL"
    s.handler.handle(_ev(call, "ANSWERED"))
    assert s.handler.handle(_ev(call, "NO_ANSWER")) == "IGNORED_CONTRADICTORY"
    assert _q(s, "SELECT state FROM calls")[0][0] == "CONNECTED"


def test_late_event_for_old_call_cannot_free_agent_now_on_another_call():
    s, clock, call = _one_call_system()
    s.handler.handle(_ev(call, "ANSWERED"))
    s.handler.handle(_ev(call, "COMPLETED"))
    from smartdialer import store
    with s.db.tx() as c:                       # agent moves on to a different call
        c.execute("UPDATE agents SET state='CONNECTED', call_id='other-call'")
        assert store.agent_release(c, "ag0000", call, clock.now(), 20) is False
        assert c.execute("SELECT call_id FROM agents").fetchone()[0] == "other-call"


def test_agent_disappears_during_setup():
    r = demo_agent_disappears_during_setup(say=lambda *_: None)
    assert r["call"] == ("CANCELLED", "AGENT_LOST") and r["borrower"] == ("PENDING", 0)
    assert r["agent"] == "OFFLINE" and r["hangup_sent"] == 1 and r["late_answered_disposition"] == "IGNORED_AFTER_TERMINAL"
