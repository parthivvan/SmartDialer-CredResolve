"""Provider event ingestion. Idempotent, order-insensitive, crash-safe (one transaction per event).

Why this is correct:
  * dedupe_key UNIQUE(provider|call|type): replays/duplicates are rejected by the DB, not by app memory.
  * A call only moves FORWARD in CALL_RANK. Late/out-of-order events are stored but ignored.
  * Event row + state change commit atomically. A worker crash mid-event rolls back BOTH, so the provider's
    redelivery (or the reconciler's status poll) re-applies it cleanly.
"""
from .states import TERMINAL, rank
from . import store

EVENT_TARGET = {"RINGING": "RINGING", "ANSWERED": "ANSWERED", "COMPLETED": "COMPLETED",
                "NO_ANSWER": "FAILED", "BUSY": "FAILED", "FAILED": "FAILED"}


class EventHandler:
    def __init__(self, db, clock, cfg):
        self.db, self.clock, self.cfg = db, clock, cfg

    def handle(self, ev) -> str:
        now = self.clock.now()
        with self.db.tx() as c:
            key = f"{ev.provider}|{ev.call_ref}|{ev.event_type}"
            cur = c.execute("INSERT OR IGNORE INTO call_events(dedupe_key, call_ref, provider, event_type, "
                            "provider_ts, received_at, disposition) VALUES (?,?,?,?,?,?, 'PENDING')",
                            (key, ev.call_ref, ev.provider, ev.event_type, ev.provider_ts, now))
            if cur.rowcount == 0:
                c.execute("INSERT INTO event_dupes(dedupe_key) VALUES(?) ON CONFLICT(dedupe_key) DO UPDATE SET n=n+1", (key,))
                return "DUPLICATE"
            disp = self._apply(c, ev, now)
            c.execute("UPDATE call_events SET disposition=? WHERE id=?", (disp, cur.lastrowid))
            return disp

    # ------------------------------------------------------------------
    def _apply(self, c, ev, now):
        cfg = self.cfg
        call = c.execute("SELECT * FROM calls WHERE id=?", (ev.call_ref,)).fetchone()
        if call is None:
            return "UNKNOWN_CALL"
        target = EVENT_TARGET.get(ev.event_type)
        if target is None:
            return "UNKNOWN_EVENT"
        if call["state"] in TERMINAL:
            return "IGNORED_AFTER_TERMINAL"
        if rank(target) <= rank(call["state"]):
            return "IGNORED_STALE"
        if target == "FAILED" and call["answered_at"] is not None:
            return "IGNORED_CONTRADICTORY"
        cid, aid = call["id"], call["agent_id"]

        if target == "RINGING":
            c.execute("UPDATE calls SET state='RINGING' WHERE id=?", (cid,))
            if aid:
                c.execute("UPDATE agents SET state='DIALING', version=version+1 WHERE id=? AND call_id=? "
                          "AND state='RESERVED'", (aid, cid))
            return "APPLIED"

        if target == "ANSWERED":
            c.execute("UPDATE calls SET state='ANSWERED', answered_at=COALESCE(answered_at,?), "
                      "resolved_at=COALESCE(resolved_at,?) WHERE id=?", (now, now, cid))
            got = None
            if aid:   # progressive: agent was reserved up-front
                ok = c.execute("UPDATE agents SET state='CONNECTED', version=version+1, state_since=?, "
                               "lease_expires=NULL WHERE id=? AND call_id=? AND state IN ('RESERVED','DIALING')",
                               (now, aid, cid)).rowcount
                got = aid if ok else None
            if got is None:   # predictive (or the reserved agent vanished): claim ANY free agent atomically
                got = store.reserve_agent(c, call["campaign_id"], cid, now, None, cfg.agent_stale_after,
                                          to_state="CONNECTED")
            if got is None:
                return self._abandon(c, call, now)
            c.execute("UPDATE calls SET state='CONNECTED', agent_id=?, connected_at=? WHERE id=?", (got, now, cid))
            return "APPLIED"

        if target == "COMPLETED":
            c.execute("UPDATE calls SET state='COMPLETED', ended_at=?, answered_at=COALESCE(answered_at,?), "
                      "resolved_at=COALESCE(resolved_at,?), outcome=?, fail_reason=? WHERE id=?",
                      (now, now, now, "CONNECTED" if call["connected_at"] else "COMPLETED_UNSEEN",
                       ev.reason, cid))
            if aid:
                store.agent_release(c, aid, cid, now, cfg.wrap_up_seconds)
            c.execute("UPDATE borrowers SET state='DONE', lease_expires=NULL WHERE id=?", (call["borrower_id"],))
            return "APPLIED"

        # FAILED family
        reason = ev.reason or ev.event_type
        unanswered = ev.event_type in ("NO_ANSWER", "BUSY")
        c.execute("UPDATE calls SET state='FAILED', ended_at=?, fail_reason=?, outcome=?, resolved_at=? WHERE id=?",
                  (now, reason, "NO_ANSWER" if unanswered else "PROVIDER_FAILED",
                   now if unanswered else None, cid))
        if aid:
            store.agent_release(c, aid, cid, now, cfg.wrap_up_seconds, wrap=False)
        store.borrower_requeue(c, call["borrower_id"], now, cfg, consume_attempt=unanswered)
        return "APPLIED"

    def _abandon(self, c, call, now):
        """Answered but no agent free: hang up, count it, and queue a prompt callback."""
        c.execute("UPDATE calls SET state='ABANDONED', ended_at=?, outcome='ABANDONED', agent_id=NULL WHERE id=?",
                  (now, call["id"]))
        store.borrower_requeue(c, call["borrower_id"], now, self.cfg, consume_attempt=False,
                               delay=self.cfg.abandon_callback_delay, callback=1)
        store.enqueue_hangup(c, call["id"], call["provider"], now)
        return "ABANDONED"
