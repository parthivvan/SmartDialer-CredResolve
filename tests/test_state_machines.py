import pytest
from smartdialer.states import AgentState, AGENT_TRANSITIONS, assert_agent_transition, CALL_RANK, CallState, TERMINAL, rank


def test_every_agent_state_has_transitions_defined():
    assert set(AGENT_TRANSITIONS) == set(AgentState)


@pytest.mark.parametrize("a,b", [("OFFLINE", "CONNECTED"), ("WRAP_UP", "CONNECTED"), ("PAUSED", "RESERVED"),
                                  ("CONNECTED", "RESERVED"), ("OFFLINE", "WRAP_UP")])
def test_illegal_agent_transitions_rejected(a, b):
    with pytest.raises(ValueError):
        assert_agent_transition(a, b)


def test_call_ranks_are_monotonic_along_happy_path():
    path = ["QUEUED", "RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED", "COMPLETED"]
    ranks = [rank(s) for s in path]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_all_terminal_states_share_top_rank():
    assert {CALL_RANK[CallState(s)] for s in TERMINAL} == {6}
