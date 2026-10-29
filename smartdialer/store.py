"""All state mutations. Every transition is a compare-and-set: UPDATE ... WHERE <expected state>, rowcount checked.
That is what makes two workers unable to both win the same agent / borrower / call transition."""
from .states import assert_agent_transition, LIVE, UNANSWERED, TERMINAL

_LIVE_SQL = ",".join(f"'{s}'" for s in LIVE)
_TERM_SQL = ",".join(f"'{s}'" for s in TERMINAL)
_UNANS_SQL = ",".join(f"'{s}'" for s in UNANSWERED)


# ------------------------------------------------------------------ agents
def agent_online(c, agent_id, now):
    return c.execute("UPDATE agents SET state='AVAILABLE', version=version+1, state_since=?, last_seen=? "
                     "WHERE id=? AND state='OFFLINE'", (now, now, agent_id)).rowcount == 1


def reserve_agent(c, campaign, call_id, now, lease, stale_after, to_state="RESERVED"):
    """Atomically take ONE available agent. CAS on (state, version): if two workers pick the same row,
    exactly one UPDATE matches; the loser retries the next candidate."""
    assert_agent_transition("AVAILABLE", to_state)
    for _ in range(8):
        row = c.execute("SELECT id, version FROM agents WHERE campaign_id=? AND state='AVAILABLE' AND last_seen>=? "
                        "ORDER BY state_since LIMIT 1", (campaign, now - stale_after)).fetchone()
        if row is None:
            return None
        n = c.execute("UPDATE agents SET state=?, call_id=?, version=version+1, state_since=?, lease_expires=? "
                      "WHERE id=? AND state='AVAILABLE' AND version=?",
                      (to_state, call_id, now, lease, row["id"], row["version"])).rowcount
        if n == 1:
            return row["id"]
    return None


def agent_release(c, agent_id, call_id, now, wrap_seconds, wrap=True):
    """Free an agent AFTER a call - but only if it is still bound to THIS call (a stale/late event for an old
    call must never free an agent that has moved on)."""
    row = c.execute("SELECT state, call_id, pending FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not row or row["call_id"] != call_id or row["state"] not in ("RESERVED", "DIALING", "CONNECTED"):
        return False
    if row["pending"] == "OFFLINE":
        to = "OFFLINE"
    elif row["pending"] == "PAUSED" and row["state"] == "CONNECTED":
        to = "PAUSED"
    elif wrap and row["state"] == "CONNECTED":
        to = "WRAP_UP"
    else:
        to = "AVAILABLE"
    assert_agent_transition(row["state"], to)
    wrap_until = now + wrap_seconds if to == "WRAP_UP" else None
    return c.execute("UPDATE agents SET state=?, call_id=NULL, pending=NULL, version=version+1, state_since=?, "
                     "wrap_until=?, lease_expires=NULL WHERE id=? AND call_id=?",
                     (to, now, wrap_until, agent_id, call_id)).rowcount == 1


def agent_offline(c, agent_id, now, cfg):
    """Agent logs out / vanishes. Mid-setup => cancel that call (borrower returns to queue, attempt not consumed).
    Mid-conversation => finish the call first (pending flag)."""
    row = c.execute("SELECT state, call_id FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not row or row["state"] == "OFFLINE":
        return "NOOP"
    if row["state"] in ("AVAILABLE", "WRAP_UP", "PAUSED"):
        c.execute("UPDATE agents SET state='OFFLINE', version=version+1, state_since=? WHERE id=? AND state IN "
                  "('AVAILABLE','WRAP_UP','PAUSED')", (now, agent_id))
        return "OFFLINE"
    if row["state"] in ("RESERVED", "DIALING"):
        cid = row["call_id"]
        c.execute("UPDATE agents SET state='OFFLINE', call_id=NULL, version=version+1, state_since=? WHERE id=?",
                  (now, agent_id))
        call = c.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
        if call and call["state"] in UNANSWERED:
            cancel_call(c, call, "AGENT_LOST", now, cfg)
        return "OFFLINE_CALL_CANCELLED"
    c.execute("UPDATE agents SET pending='OFFLINE' WHERE id=?", (agent_id,))   # CONNECTED: after the call
    return "PENDING_OFFLINE"


def tick_agents(c, campaign, now, stale_after):
    """Timer-driven transitions: wrap-up expiry, and heartbeat-loss => OFFLINE."""
    a = c.execute("UPDATE agents SET state='AVAILABLE', wrap_until=NULL, version=version+1, state_since=? "
                  "WHERE campaign_id=? AND state='WRAP_UP' AND wrap_until<=?", (now, campaign, now)).rowcount
    b = c.execute("UPDATE agents SET state='OFFLINE', version=version+1, state_since=? WHERE campaign_id=? "
                  "AND state IN ('AVAILABLE','WRAP_UP','PAUSED') AND last_seen<?",
                  (now, campaign, now - stale_after * 4)).rowcount
    return a, b


# ------------------------------------------------------------------ borrowers
def reserve_borrower(c, campaign, now, lease):
    for _ in range(8):
        row = c.execute("SELECT id, phone, dial_seq FROM borrowers WHERE campaign_id=? AND state='PENDING' "
                        "AND next_attempt_at<=? ORDER BY callback DESC, priority DESC, next_attempt_at, id LIMIT 1",
                        (campaign, now)).fetchone()
        if row is None:
            return None
        n = c.execute("UPDATE borrowers SET state='RESERVED', dial_seq=dial_seq+1, attempts=attempts+1, "
                      "lease_expires=? WHERE id=? AND state='PENDING'", (lease, row["id"])).rowcount
        if n == 1:
            return {"id": row["id"], "phone": row["phone"], "dial_seq": row["dial_seq"] + 1}
    return None


def borrower_requeue(c, borrower_id, now, cfg, consume_attempt, delay=None, callback=0):
    """Return a borrower to the queue after a call ended unsuccessfully."""
    if consume_attempt:
        row = c.execute("SELECT attempts FROM borrowers WHERE id=?", (borrower_id,)).fetchone()
        if row["attempts"] >= cfg.max_attempts:
            c.execute("UPDATE borrowers SET state='EXHAUSTED', lease_expires=NULL WHERE id=?", (borrower_id,))
            return
        d = cfg.retry_delay if delay is None else delay
        c.execute("UPDATE borrowers SET state='PENDING', next_attempt_at=?, lease_expires=NULL WHERE id=?",
                  (now + d, borrower_id))
    else:
        d = cfg.provider_retry_delay if delay is None else delay
        c.execute("UPDATE borrowers SET state='PENDING', attempts=MAX(attempts-1,0), next_attempt_at=?, callback=?, "
                  "lease_expires=NULL WHERE id=?", (now + d, callback, borrower_id))


# ------------------------------------------------------------------ calls
def enqueue_hangup(c, call_ref, provider, now):
    c.execute("INSERT INTO outbox(kind, call_ref, provider, created_at) VALUES('HANGUP', ?, ?, ?)",
              (call_ref, provider, now))


def cancel_call(c, call, reason, now, cfg):
    """Cancel a not-yet-answered call (shed / agent lost). No attempt is consumed."""
    n = c.execute(f"UPDATE calls SET state='CANCELLED', ended_at=?, fail_reason=?, outcome='CANCELLED' "
                  f"WHERE id=? AND state IN ({_UNANS_SQL})", (now, reason, call["id"])).rowcount
    if n != 1:
        return False
    if call["agent_id"]:
        agent_release(c, call["agent_id"], call["id"], now, cfg.wrap_up_seconds, wrap=False)
    borrower_requeue(c, call["borrower_id"], now, cfg, consume_attempt=False, delay=0)
    enqueue_hangup(c, call["id"], call["provider"], now)
    return True


# ------------------------------------------------------------------ invariants (used by tests, sim, monitoring)
def check_invariants(c):
    v = []
    for r in c.execute(f"""SELECT a.id FROM agents a LEFT JOIN calls k ON k.id=a.call_id
            WHERE a.state IN ('RESERVED','DIALING','CONNECTED')
              AND (k.id IS NULL OR k.agent_id IS NOT a.id OR k.state NOT IN ({_LIVE_SQL}))"""):
        v.append(f"agent {r['id']} busy without a matching live call")
    for r in c.execute(f"""SELECT k.id FROM calls k LEFT JOIN agents a ON a.id=k.agent_id
            WHERE k.state IN ({_LIVE_SQL}) AND k.agent_id IS NOT NULL
              AND (a.call_id IS NOT k.id OR a.state NOT IN ('RESERVED','DIALING','CONNECTED'))"""):
        v.append(f"live call {r['id']} points at an agent that is not bound to it")
    for r in c.execute(f"SELECT agent_id, COUNT(*) n FROM calls WHERE state IN ({_LIVE_SQL}) AND agent_id IS NOT NULL "
                       f"GROUP BY agent_id HAVING n>1"):
        v.append(f"agent {r['agent_id']} double-allocated to {r['n']} live calls")
    for r in c.execute(f"SELECT borrower_id, COUNT(*) n FROM calls WHERE state IN ({_LIVE_SQL}) GROUP BY borrower_id "
                       f"HAVING n>1"):
        v.append(f"borrower {r['borrower_id']} has {r['n']} live calls")
    return v
