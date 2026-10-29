"""Pacing engines. PURE functions of (Snapshot, Stats) -> DialRequest.

Deliberately imports nothing about providers, the allocator, the DB or the safety controller's secret
(enforced by tests/test_safety.py::test_pacing_cannot_reach_telecom). Pacing can only *ask*.
"""
from math import exp, floor

from .models import DialRequest, Snapshot, Stats


class ProgressivePacing:
    name = "progressive"

    def __init__(self, cfg):
        self.cfg = cfg

    def propose(self, snap: Snapshot, stats: Stats) -> DialRequest:
        return DialRequest("progressive", snap.available,
                           {"rule": "one call per available agent", "available": snap.available})


class PredictivePacing:
    """N_new = floor( (A + W + C*(1-exp(-H/T))) / p  -  U )

      A  agents available now                       W  agents whose wrap-up ends within H
      C  agents on a call                           T  mean talk time (exponential-hazard approximation)
      H  lead time = measured INITIATED->ANSWER delay of recent calls (a call placed now answers ~H from now)
      p  estimated answer probability               U  speculative calls already ringing

    i.e. keep (calls ringing) x p equal to (agents that will be free when those calls answer)."""
    name = "predictive"

    def __init__(self, cfg):
        self.cfg = cfg

    def propose(self, snap: Snapshot, stats: Stats) -> DialRequest:
        cfg = self.cfg
        H = min(60.0, max(3.0, stats.avg_setup))
        p = max(cfg.min_p, stats.p_hat)
        T = max(10.0, stats.avg_talk)
        W = sum(1 for w in snap.wrap_until if w <= snap.now + H)
        C_end = snap.connected * (1 - exp(-H / T))
        F = snap.available + W + C_end
        target = F / p
        n = max(0, min(floor(target - snap.unbound_inflight), cfg.max_new_calls_per_cycle))
        trace = {"formula": "floor((A+W+C*(1-exp(-H/T)))/p - U)", "A": snap.available, "W": W,
                 "C": snap.connected, "C_end": round(C_end, 2), "H": round(H, 1), "T": round(T, 1),
                 "p_hat": round(p, 3), "F": round(F, 2), "target_inflight": round(target, 1),
                 "U": snap.unbound_inflight, "n": n}
        return DialRequest("predictive", n, trace)
