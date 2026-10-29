"""Deterministic (seeded) mock telecom providers running on the injected clock.

Provider A: fast, reliable, low failure rate, in-order events.
Provider B: slower setup, timeouts (some of which actually placed the call = 'ghost'), duplicate events,
            out-of-order delivery (random delivery jitter), and an outage switch for failure demos.
"""
import heapq
import random

from .base import TelecomProvider, ProviderEvent, ProviderError, ProviderTimeout


class MockProvider(TelecomProvider):
    def __init__(self, name, clock, seed=0, *, answer_fn=lambda t: 0.5, talk_mean=90.0, setup_mean=3.0,
                 fail_rate=0.0, timeout_rate=0.0, ghost_prob=0.5, dup_rate=0.0, reorder_jitter=0.0,
                 event_delay=0.2):
        self.name = name
        self.clock = clock
        self.rng = random.Random(seed)
        self.answer_fn = answer_fn
        self.talk_mean = talk_mean
        self.setup_mean = setup_mean
        self.fail_rate, self.timeout_rate, self.ghost_prob = fail_rate, timeout_rate, ghost_prob
        self.dup_rate, self.reorder_jitter, self.event_delay = dup_rate, reorder_jitter, event_delay
        self.outage = False        # total blackout: API and webhooks dark
        self.api_outage = False    # API calls (place/hangup) time out, but webhooks for existing calls still flow
        self.calls = {}        # call_ref -> {'pcid', 'events': [(t, type)], 'hung': bool}
        self._q = []           # heap of (deliver_at, seq, ProviderEvent)
        self._seq = 0
        self.stats = {"place_calls": 0, "timeouts": 0, "errors": 0, "ghosts": 0, "dups_sent": 0}

    # ---- API used by the dialer -------------------------------------------------------------
    def place_call(self, call_ref, to):
        now = self.clock.now()
        if self.outage or self.api_outage:
            self.stats["timeouts"] += 1
            raise ProviderTimeout(f"{self.name} outage")
        if call_ref in self.calls:                       # idempotent on our reference
            return self.calls[call_ref]["pcid"]
        self.stats["place_calls"] += 1
        r = self.rng.random()
        if r < self.timeout_rate:
            self.stats["timeouts"] += 1
            if self.rng.random() < self.ghost_prob:      # timed out to us, but the call really started
                self.stats["ghosts"] += 1
                self._create(call_ref, now)
            raise ProviderTimeout(f"{self.name} timeout")
        if r < self.timeout_rate + self.fail_rate:
            self.stats["errors"] += 1
            raise ProviderError(f"{self.name} 5xx")
        return self._create(call_ref, now)

    def hangup(self, call_ref):
        if self.outage or self.api_outage:
            raise ProviderTimeout(f"{self.name} outage")
        c = self.calls.get(call_ref)
        if not c or c["hung"]:
            return
        now = self.clock.now()
        c["hung"] = True
        self._q = [x for x in self._q if not (x[2].call_ref == call_ref and x[2].provider_ts > now)]
        heapq.heapify(self._q)
        c["events"] = [e for e in c["events"] if e[0] <= now] + [(now, "COMPLETED")]
        self._push(ProviderEvent(self.name, call_ref, "COMPLETED", now, "HANGUP", c["pcid"]), now)

    def get_status(self, call_ref):
        if self.outage:
            raise ProviderTimeout(f"{self.name} outage")
        c = self.calls.get(call_ref)
        if not c:
            return "UNKNOWN"
        now = self.clock.now()
        past = [e for e in c["events"] if e[0] <= now]
        return past[-1][1] if past else "INITIATED"

    def poll_events(self):
        if self.outage:                                   # webhooks are delayed until recovery (late arrival)
            return []
        now = self.clock.now()
        out = []
        while self._q and self._q[0][0] <= now:
            out.append(heapq.heappop(self._q)[2])
        return out

    # ---- internals --------------------------------------------------------------------------
    def _create(self, call_ref, now):
        pcid = f"{self.name}-{len(self.calls) + 1}"
        setup = max(0.5, self.rng.gauss(self.setup_mean, self.setup_mean * 0.3))
        ring_at = now + setup
        evs = [(ring_at, "RINGING")]
        if self.rng.random() < self.answer_fn(now):
            ans = ring_at + self.rng.uniform(3, 10)
            talk = max(10.0, self.rng.gammavariate(4, self.talk_mean / 4))
            evs += [(ans, "ANSWERED"), (ans + talk, "COMPLETED")]
        else:
            end = ring_at + self.rng.uniform(12, 28)
            evs.append((end, "BUSY" if self.rng.random() < 0.05 else "NO_ANSWER"))
        self.calls[call_ref] = {"pcid": pcid, "events": evs, "hung": False}
        for t, typ in evs:
            self._push(ProviderEvent(self.name, call_ref, typ, t, None, pcid), t)
        return pcid

    def _push(self, ev, t):
        deliver = t + self.event_delay + (self.rng.uniform(0, self.reorder_jitter) if self.reorder_jitter else 0.0)
        self._seq += 1
        heapq.heappush(self._q, (deliver, self._seq, ev))
        if self.dup_rate and self.rng.random() < self.dup_rate:
            self.stats["dups_sent"] += 1
            self._seq += 1
            heapq.heappush(self._q, (deliver + self.rng.uniform(0, 3), self._seq, ev))


def provider_a(clock, seed=1, **kw):
    base = dict(setup_mean=2.5, fail_rate=0.01, event_delay=0.2)
    base.update(kw)
    return MockProvider("A", clock, seed, **base)


def provider_b(clock, seed=2, **kw):
    base = dict(setup_mean=6.0, fail_rate=0.02, timeout_rate=0.05, dup_rate=0.15, reorder_jitter=6.0, event_delay=1.0)
    base.update(kw)
    return MockProvider("B", clock, seed, **base)
