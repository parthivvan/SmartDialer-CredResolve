import hashlib
import hmac
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class Snapshot:
    """Read-only numbers handed to the pacing engine. Pacing never gets handles to providers/allocator."""
    now: float
    online: int
    available: int            # AVAILABLE with a fresh heartbeat
    connected: int
    wrap_until: tuple         # wrap-up end times of WRAP_UP agents
    unbound_inflight: int     # speculative calls not yet answered (no agent attached)
    bound_inflight: int       # progressive calls (agent already reserved) not yet answered
    eligible_borrowers: int
    provider_states: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Stats:
    n: int = 0                # resolved attempts in the long window
    answered_n: int = 0
    p_hat: float = 0.3        # best estimate of answer probability (used by pacing)
    p_up: float = 1.0         # upper confidence bound (used ONLY by the safety controller)
    avg_setup: float = 10.0   # seconds from INITIATED to answer
    avg_talk: float = 90.0
    abandon_rate: float = 0.0
    abandon_n: int = 0        # answered calls in the abandonment window


@dataclass(frozen=True)
class DialRequest:
    mode: str
    n: int
    trace: dict


@dataclass(frozen=True)
class SafetyDecision:
    action: str               # APPROVE | REDUCE | REJECT | FALLBACK_PROGRESSIVE
    requested: int
    approved: int
    shed: int
    bind_agents: bool         # True => each call gets an agent reserved up-front (cannot be abandoned)
    provider: Optional[str]
    reasons: tuple
    detail: dict
    probe_provider: Optional[str] = None   # a HALF_OPEN provider gets exactly one probe call
    seal: str = ""

    def _payload(self) -> bytes:
        d = asdict(self)
        d.pop("seal")
        return json.dumps(d, sort_keys=True, default=str).encode()

    def sealed(self, secret: bytes) -> "SafetyDecision":
        s = hmac.new(secret, self._payload(), hashlib.sha256).hexdigest()
        d = asdict(self)
        d["seal"] = s
        d["reasons"] = tuple(self.reasons)
        return SafetyDecision(**d)

    def verify(self, secret: bytes) -> bool:
        return bool(self.seal) and hmac.compare_digest(
            self.seal, hmac.new(secret, self._payload(), hashlib.sha256).hexdigest())
