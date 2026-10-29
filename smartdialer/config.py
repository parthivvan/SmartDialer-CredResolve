from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    campaign_id: str = "c1"
    provider_order: tuple = ("A", "B")
    # lifecycle / leases
    reservation_lease: float = 30.0      # a RESERVED call/borrower/agent not dispatched within this is recoverable
    agent_stale_after: float = 15.0      # heartbeat older than this => agent not dialable
    wrap_up_seconds: float = 20.0
    ring_timeout: float = 45.0           # INITIATED/RINGING older than this => ask provider for status
    stuck_hard_timeout: float = 90.0     # ...and if provider is unreachable, force-fail after this
    max_call_seconds: float = 3600.0
    max_attempts: int = 3
    retry_delay: float = 600.0           # between non-answered attempts to the same borrower
    provider_retry_delay: float = 5.0    # after infrastructure failure (attempt not consumed)
    abandon_callback_delay: float = 30.0
    # safety
    abandon_cap: float = 0.03            # max abandoned / answered, rolling. Set from compliance policy.
    safety_target_fraction: float = 0.5  # plan to run at half the cap, keep the rest as variance headroom
    min_samples: int = 30                # below this many resolved attempts: progressive only
    min_abandon_samples: int = 20
    fallback_at_fraction_of_cap: float = 0.8
    max_new_calls_per_cycle: int = 500
    max_unbound_ratio: float = 5.0       # hard cap: unanswered speculative calls <= ratio * online agents
    talk_end_discount: float = 0.5       # controller only trusts half of the forecast "agents finishing soon"
    calling_hours: tuple = (8, 19)       # IST, [start, end); None disables the window check
    # stats
    stats_window: int = 200
    abandon_window: int = 400            # answered calls over which abandonment is measured
    short_window: int = 30
    prior_p: float = 0.3
    prior_strength: float = 10.0
    min_p: float = 0.03
    # provider circuit breaker
    breaker_window: float = 60.0
    breaker_min_samples: int = 8
    breaker_fail_rate: float = 0.5
    breaker_cooldown: float = 30.0
