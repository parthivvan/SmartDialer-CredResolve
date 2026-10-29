"""Dialer worker. Many of these can run against the same campaign; correctness comes from the DB, not from the workers."""
import json

from . import store, health
from .models import Snapshot
from .providers.base import ProviderError, ProviderTimeout
from .stats import compute_stats


class WorkerCrash(Exception):
    """Injected by failure demos/tests to simulate a process dying at a precise point."""


class Dialer:
    def __init__(self, worker_id, db, clock, cfg, pacing, safety, allocator, providers):
        self.worker_id, self.db, self.clock, self.cfg = worker_id, db, clock, cfg
        self.pacing, self.safety, self.allocator, self.providers = pacing, safety, allocator, providers
        self.fault = None

    def _maybe_crash(self, point):
        if self.fault == point:
            self.fault = None
            raise WorkerCrash(point)

    # ------------------------------------------------------------------ one pacing cycle
    def run_cycle(self):
        cfg, now = self.cfg, self.clock.now()
        self.process_outbox()
        with self.db.tx() as c:
            # Per-campaign mutex: snapshot -> decide -> reserve is one atomic unit, so two workers can never both
            # act on the same stale snapshot and jointly overdial. (PostgreSQL: SELECT ... FOR UPDATE on this row.)
            c.execute("UPDATE campaigns SET cycle_seq=cycle_seq+1 WHERE id=?", (cfg.campaign_id,))
            store.tick_agents(c, cfg.campaign_id, now, cfg.agent_stale_after)
            snap = self._snapshot(c, now)
            stats = compute_stats(c, cfg)
            req = self.pacing.propose(snap, stats)
            dec = self.safety.review(req, snap, stats)
            ids = self.allocator.execute(c, dec, now)
            c.execute("INSERT INTO safety_decisions(campaign_id, ts, mode, action, requested, approved, shed, "
                      "bind_agents, provider, reasons, trace) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (cfg.campaign_id, now, req.mode, dec.action, dec.requested, len(ids), dec.shed,
                       int(dec.bind_agents), dec.provider, ",".join(dec.reasons),
                       json.dumps({"pacing": req.trace, "safety": dec.detail}, default=str)))
        for cid in ids:                         # network I/O happens OUTSIDE the DB transaction
            self.dispatch(cid)
        return dec, ids

    def _snapshot(self, c, now):
        cfg = self.cfg
        cid = cfg.campaign_id
        by = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM agents WHERE campaign_id=? GROUP BY state", (cid,))}
        avail = c.execute("SELECT COUNT(*) n FROM agents WHERE campaign_id=? AND state='AVAILABLE' AND last_seen>=?",
                          (cid, now - cfg.agent_stale_after)).fetchone()["n"]
        wrap = tuple(r["wrap_until"] for r in c.execute(
            "SELECT wrap_until FROM agents WHERE campaign_id=? AND state='WRAP_UP'", (cid,)))
        inflight = {r["bound"]: r["n"] for r in c.execute(
            "SELECT bound, COUNT(*) n FROM calls WHERE campaign_id=? AND state IN ('RESERVED','INITIATED','RINGING') "
            "GROUP BY bound", (cid,))}
        elig = c.execute("SELECT COUNT(*) n FROM (SELECT 1 FROM borrowers WHERE campaign_id=? AND state='PENDING' "
                         "AND next_attempt_at<=? LIMIT 2000)", (cid, now)).fetchone()["n"]
        pstates = {p: health.refresh_breaker(c, p, now, cfg) for p in cfg.provider_order}
        online = sum(n for s, n in by.items() if s != "OFFLINE")
        return Snapshot(now, online, avail, by.get("CONNECTED", 0), wrap, inflight.get(0, 0), inflight.get(1, 0),
                        elig, pstates)

    # ------------------------------------------------------------------ talking to the provider
    def dispatch(self, call_id):
        with self.db.tx() as c:
            call = c.execute("SELECT k.*, b.phone FROM calls k JOIN borrowers b ON b.id=k.borrower_id WHERE k.id=?",
                             (call_id,)).fetchone()
        if call is None or call["state"] != "RESERVED":
            return "SKIP"
        prov = self.providers[call["provider"]]
        try:
            self._maybe_crash("before_place")
            pcid = prov.place_call(call_id, call["phone"])      # idempotent on call_id
            self._maybe_crash("after_place")
        except ProviderTimeout:
            return self._dispatch_failed(call, "PROVIDER_TIMEOUT", hangup=True)   # ambiguous => hang up defensively
        except ProviderError:
            return self._dispatch_failed(call, "PROVIDER_ERROR", hangup=False)
        now = self.clock.now()
        with self.db.tx() as c:
            health.log_dispatch(c, call["provider"], now, True)
            n = c.execute("UPDATE calls SET state='INITIATED', provider_call_id=?, initiated_at=COALESCE(initiated_at,?) "
                          "WHERE id=? AND state='RESERVED'", (pcid, now, call_id)).rowcount
            if n:
                if call["agent_id"]:
                    c.execute("UPDATE agents SET state='DIALING', version=version+1 WHERE id=? AND call_id=? "
                              "AND state='RESERVED'", (call["agent_id"], call_id))
            else:   # an event or a cancel already moved the call on; fill in what we know, never regress
                c.execute("UPDATE calls SET provider_call_id=COALESCE(provider_call_id,?), "
                          "initiated_at=COALESCE(initiated_at,?) WHERE id=?", (pcid, now, call_id))
                st = c.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()["state"]
                if st in ("CANCELLED", "ABANDONED", "FAILED"):   # cancelled while we were placing it
                    store.enqueue_hangup(c, call_id, call["provider"], now)
        return "INITIATED"

    def _dispatch_failed(self, call, reason, hangup):
        now = self.clock.now()
        with self.db.tx() as c:
            health.log_dispatch(c, call["provider"], now, False)
            n = c.execute("UPDATE calls SET state='FAILED', ended_at=?, fail_reason=?, outcome='PROVIDER_FAILED' "
                          "WHERE id=? AND state='RESERVED'", (now, reason, call["id"])).rowcount
            if n:
                if call["agent_id"]:
                    store.agent_release(c, call["agent_id"], call["id"], now, self.cfg.wrap_up_seconds, wrap=False)
                store.borrower_requeue(c, call["borrower_id"], now, self.cfg, consume_attempt=False)
                if hangup:
                    store.enqueue_hangup(c, call["id"], call["provider"], now)
        return reason

    def process_outbox(self, limit=200):
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM outbox WHERE done=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
        for r in rows:
            try:
                self.providers[r["provider"]].hangup(r["call_ref"])
                done = 1
            except ProviderError:
                done = 2 if r["attempts"] >= 10 else 0
            with self.db.tx() as c:
                c.execute("UPDATE outbox SET done=?, attempts=attempts+1 WHERE id=?", (done, r["id"]))
