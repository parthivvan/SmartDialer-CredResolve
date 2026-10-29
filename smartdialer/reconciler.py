"""Crash/lag recovery. Runs periodically on any worker; every action is idempotent and CAS-guarded."""
from . import store, health
from .providers.base import ProviderEvent, ProviderError

_SYNTH = {"ANSWERED": "ANSWERED", "COMPLETED": "COMPLETED", "NO_ANSWER": "NO_ANSWER", "BUSY": "BUSY"}


class Reconciler:
    def __init__(self, db, clock, cfg, dialer, handler, providers):
        self.db, self.clock, self.cfg, self.dialer, self.handler, self.providers = db, clock, cfg, dialer, handler, providers

    def run_once(self):
        cfg, now = self.cfg, self.clock.now()
        out = {"redispatched": 0, "polled": 0, "forced": 0, "orphan_borrowers": 0, "orphan_agents": 0}
        with self.db.tx() as c:
            stale_reserved = [r["id"] for r in c.execute(
                "SELECT id FROM calls WHERE campaign_id=? AND state='RESERVED' AND lease_expires<?",
                (cfg.campaign_id, now))]
            stuck = c.execute(
                "SELECT id, provider, state, initiated_at, answered_at, connected_at FROM calls WHERE campaign_id=? "
                "AND ((state IN ('INITIATED','RINGING') AND initiated_at<?) OR "
                "(state IN ('ANSWERED','CONNECTED') AND COALESCE(connected_at, answered_at)<?))",
                (cfg.campaign_id, now - cfg.ring_timeout, now - cfg.max_call_seconds)).fetchall()
        # 1. worker died between reserve and dispatch (or after place_call): re-drive. Safe: place_call is idempotent.
        for cid in stale_reserved:
            self.dialer.dispatch(cid)
            out["redispatched"] += 1
        # 2. calls with no recent provider news: ask the provider what really happened
        for r in stuck:
            age_from = r["initiated_at"] if r["state"] in ("INITIATED", "RINGING") else (r["connected_at"] or r["answered_at"])
            age = now - age_from
            try:
                st = self.providers[r["provider"]].get_status(r["id"])
            except ProviderError:
                if age > cfg.stuck_hard_timeout + (cfg.max_call_seconds if r["state"] in ("ANSWERED", "CONNECTED") else 0):
                    out["forced"] += self._force_close(r, now)
                continue
            out["polled"] += 1
            if st == "UNKNOWN":
                out["forced"] += self._force_close(r, now)
            elif st in _SYNTH:
                self.handler.handle(ProviderEvent(r["provider"], r["id"], _SYNTH[st], now, "RECONCILED"))
        # 3. orphaned reservations (defence in depth) + leaked agents
        with self.db.tx() as c:
            out["orphan_borrowers"] = c.execute(
                "UPDATE borrowers SET state='PENDING', attempts=MAX(attempts-1,0), lease_expires=NULL "
                "WHERE campaign_id=? AND state='RESERVED' AND lease_expires<? AND id NOT IN "
                "(SELECT borrower_id FROM calls WHERE state IN ('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED'))",
                (cfg.campaign_id, now)).rowcount
            out["orphan_agents"] = c.execute(
                "UPDATE agents SET state='AVAILABLE', call_id=NULL, version=version+1, state_since=? "
                "WHERE campaign_id=? AND state IN ('RESERVED','DIALING') AND (lease_expires IS NULL OR lease_expires<?) "
                "AND (call_id IS NULL OR call_id NOT IN (SELECT id FROM calls WHERE state IN "
                "('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED')))",
                (now, cfg.campaign_id, now)).rowcount
        return out

    def _force_close(self, r, now):
        """Provider can't tell us (outage/unknown call): fail the call locally and make sure it is hung up."""
        with self.db.tx() as c:
            call = c.execute("SELECT * FROM calls WHERE id=?", (r["id"],)).fetchone()
            if call["state"] in ("COMPLETED", "FAILED", "CANCELLED", "ABANDONED"):
                return 0
            connected = call["state"] in ("ANSWERED", "CONNECTED")
            c.execute("UPDATE calls SET state=?, ended_at=?, fail_reason='LOST', outcome=? WHERE id=?",
                      ("COMPLETED" if connected else "FAILED", now, "FORCED_CLOSE", r["id"]))
            if call["agent_id"]:
                store.agent_release(c, call["agent_id"], r["id"], now, self.cfg.wrap_up_seconds, wrap=connected)
            store.borrower_requeue(c, call["borrower_id"], now, self.cfg, consume_attempt=False)
            health.log_dispatch(c, call["provider"], now, False)
            store.enqueue_hangup(c, r["id"], call["provider"], now)
        return 1
