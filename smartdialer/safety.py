"""SafetyController - the only component that can authorise a dial. No 'disable' switch exists by design.

Guarantees
  G1 (deterministic floor)  bind_agents=True  => every call has an agent reserved before it is placed =>
                            such a call can NEVER be abandoned. This is the fallback and the cold-start mode.
  G2 (bounded exposure)     bind_agents=False => unanswered speculative calls U are capped so that the expected
                            abandon fraction  E[max(0, Bin(U,p_up) - F_safe)] / (U*p_up)  <= target, where p_up is an
                            UPPER confidence bound on answer rate and F_safe counts only agents we can trust to be free.
  G3 (feedback)             rolling measured abandon rate throttles the target to 0 and forces G1 before the cap is hit.
  G4 (hard gates)           calling hours, provider health (circuit breaker), absolute caps.
Output is HMAC-sealed; the CallAllocator refuses unsealed/modified decisions.
"""
from math import exp, floor, lgamma, log, log1p, sqrt

from .clock import ist_hour
from .models import DialRequest, SafetyDecision, Snapshot, Stats


def expected_abandons(n: int, p: float, f: float) -> float:
    """E[max(0, X - f)], X ~ Binomial(n, p)."""
    if n <= f or n <= 0:
        return 0.0
    p = min(max(p, 1e-6), 0.999)
    mean, sd = n * p, sqrt(n * p * (1 - p))
    lo, hi = max(int(f) + 1, int(mean - 8 * sd)), min(n, int(mean + 8 * sd) + 1)
    lg_n = lgamma(n + 1)
    tot = 0.0
    for k in range(lo, hi + 1):
        lp = lg_n - lgamma(k + 1) - lgamma(n - k + 1) + k * log(p) + (n - k) * log1p(-p)
        tot += (k - f) * exp(lp)
    return tot


def abandon_fraction(n: int, p: float, f: float) -> float:
    return expected_abandons(n, p, f) / (n * p) if n > 0 else 0.0


def max_unbound(f_safe: float, p_up: float, target: float, hard_cap: int) -> int:
    """Largest U in [0, hard_cap] with abandon_fraction(U) <= target (monotone in U => binary search)."""
    if target <= 0:
        return min(hard_cap, int(floor(f_safe)))
    lo, hi = 0, hard_cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if abandon_fraction(mid, p_up, f_safe) <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


class SafetyController:
    def __init__(self, cfg, secret: bytes):
        self.cfg = cfg
        self._secret = secret

    def review(self, req: DialRequest, snap: Snapshot, stats: Stats) -> SafetyDecision:
        cfg = self.cfg
        reasons, detail = [], {}

        probe = {"p": None}

        def done(action, approved, shed, bind, provider):
            return SafetyDecision(action, req.n, approved, shed, bind, provider, tuple(reasons),
                                  detail, probe["p"] if approved > 0 else None).sealed(self._secret)

        # --- G4 hard gates ---------------------------------------------------------------
        if cfg.calling_hours and not (cfg.calling_hours[0] <= ist_hour(snap.now) < cfg.calling_hours[1]):
            reasons.append("OUTSIDE_CALLING_HOURS")
            return done("REJECT", 0, 0, True, None)
        provider, pstate = None, None
        for want in ("CLOSED", "HALF_OPEN"):
            for name in cfg.provider_order:
                if snap.provider_states.get(name, "CLOSED") == want:
                    provider, pstate = name, want
                    break
            if provider:
                break
        if provider is None:
            reasons.append("NO_HEALTHY_PROVIDER")
            return done("REJECT", 0, 0, True, None)
        detail["provider_state"] = pstate
        if pstate == "CLOSED":      # a recovering (HALF_OPEN) provider gets one probe call, the rest go to the healthy one
            half = [n for n in cfg.provider_order if snap.provider_states.get(n) == "HALF_OPEN"]
            probe["p"] = half[0] if half else None

        # --- deterministic floor vs. bounded overdial --------------------------------------
        fallback = []
        if req.mode == "progressive":
            bind = True
        else:
            if stats.n < cfg.min_samples:
                fallback.append("COLD_START")
            if stats.abandon_n >= cfg.min_abandon_samples and \
                    stats.abandon_rate >= cfg.fallback_at_fraction_of_cap * cfg.abandon_cap:
                fallback.append("ABANDON_RATE_NEAR_CAP")
            if pstate == "HALF_OPEN":
                fallback.append("PROVIDER_HALF_OPEN")
            bind = bool(fallback)
        reasons.extend(fallback)

        H = min(60.0, max(3.0, stats.avg_setup))
        W = sum(1 for w in snap.wrap_until if w <= snap.now + H)
        C_end = snap.connected * (1 - exp(-H / max(10.0, stats.avg_talk)))
        f_safe = snap.available + W + floor(cfg.talk_end_discount * C_end)
        hard_cap = int(cfg.max_unbound_ratio * max(snap.online, 1))
        target = self._target_fraction(stats)
        detail.update({"p_up": round(stats.p_up, 3), "f_safe": f_safe, "target_abandon_frac": round(target, 4),
                       "U": snap.unbound_inflight, "stats_n": stats.n, "abandon_rate": round(stats.abandon_rate, 4)})

        shed = 0
        if bind:
            # reserve agents one-per-call, but never take agents that already-ringing speculative calls will need
            reserve_for_spec = int(snap.unbound_inflight * stats.p_up + 0.999)
            room = max(0, snap.available - reserve_for_spec)
            approved = min(room, cfg.max_new_calls_per_cycle, snap.eligible_borrowers)
            if req.mode == "predictive":
                approved = min(approved, snap.available)
            u_allowed = max_unbound(f_safe, stats.p_up, 0.0, hard_cap)
            shed = max(0, snap.unbound_inflight - u_allowed) if req.mode == "predictive" and fallback else 0
            if shed:
                reasons.append("SHED_SPECULATIVE_CALLS")
        else:
            u_allowed = max_unbound(f_safe, stats.p_up, target, hard_cap)
            room = u_allowed - snap.unbound_inflight
            approved = min(req.n, max(room, 0), cfg.max_new_calls_per_cycle, snap.eligible_borrowers)
            if room < 0:
                shed = -room
                reasons.append("EXPOSURE_ABOVE_BOUND_SHED")
            detail["u_allowed"] = u_allowed

        if pstate == "HALF_OPEN":
            approved = min(approved, 1)
            bind = True

        if approved < req.n and not (req.mode == "progressive" and approved == snap.available):
            reasons.append("REDUCED_TO_SAFE_BOUND")
        if approved == 0 and req.n > 0 and not fallback:
            return done("REJECT", 0, shed, bind, provider)
        if fallback and req.mode == "predictive":
            return done("FALLBACK_PROGRESSIVE", approved, shed, True, provider)
        if approved < req.n:
            return done("REDUCE", approved, shed, bind, provider)
        return done("APPROVE", approved, shed, bind, provider)

    def _target_fraction(self, stats: Stats) -> float:
        cap = self.cfg.abandon_cap
        base = cap * self.cfg.safety_target_fraction
        if stats.abandon_n < self.cfg.min_abandon_samples:
            return base * 0.5
        return base * max(0.0, 1 - stats.abandon_rate / (self.cfg.fallback_at_fraction_of_cap * cap))
