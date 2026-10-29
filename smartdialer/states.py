from enum import Enum


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # picked for a specific call (progressive) - lease protected
    DIALING = "DIALING"        # that call is ringing
    CONNECTED = "CONNECTED"    # on a live call
    WRAP_UP = "WRAP_UP"        # after-call work, timed
    PAUSED = "PAUSED"          # agent-initiated break


A = AgentState
AGENT_TRANSITIONS = {
    A.OFFLINE: {A.AVAILABLE},
    A.AVAILABLE: {A.RESERVED, A.CONNECTED, A.OFFLINE, A.PAUSED},   # CONNECTED = claimed on answer (predictive)
    A.RESERVED: {A.DIALING, A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.DIALING: {A.CONNECTED, A.AVAILABLE, A.OFFLINE},
    A.CONNECTED: {A.WRAP_UP, A.AVAILABLE, A.OFFLINE, A.PAUSED},
    A.WRAP_UP: {A.AVAILABLE, A.OFFLINE, A.PAUSED},
    A.PAUSED: {A.AVAILABLE, A.OFFLINE},
}


def assert_agent_transition(src, dst) -> None:
    if A(dst) not in AGENT_TRANSITIONS[A(src)]:
        raise ValueError(f"illegal agent transition {src} -> {dst}")


class CallState(str, Enum):
    QUEUED = "QUEUED"          # lives on the borrower row (PENDING); a call row is born at RESERVED
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"    # answered but no agent could be attached (compliance-relevant)


C = CallState
# Monotonic rank: an event may only move a call FORWARD. This single rule gives idempotency and
# out-of-order tolerance without per-provider logic.
CALL_RANK = {C.QUEUED: 0, C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3, C.ANSWERED: 4, C.CONNECTED: 5,
             C.COMPLETED: 6, C.FAILED: 6, C.CANCELLED: 6, C.ABANDONED: 6}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "ABANDONED"}
LIVE = ("RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED")
UNANSWERED = ("RESERVED", "INITIATED", "RINGING")


def rank(state: str) -> int:
    return CALL_RANK[CallState(state)]
