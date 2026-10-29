"""CallAllocator: turns a SEALED SafetyDecision into reserved rows. Refuses anything not produced by the controller."""
import uuid

from . import store
from .models import SafetyDecision


class UnauthorisedDecision(Exception):
    pass


class CallAllocator:
    def __init__(self, cfg, secret: bytes):
        self.cfg = cfg
        self._secret = secret

    def execute(self, c, dec: SafetyDecision, now: float) -> list:
        if not isinstance(dec, SafetyDecision) or not dec.verify(self._secret):
            raise UnauthorisedDecision("decision was not issued by the SafetyController")
        cfg = self.cfg
        if dec.shed:
            rows = c.execute("SELECT * FROM calls WHERE campaign_id=? AND bound=0 AND state IN "
                             "('RESERVED','INITIATED','RINGING') ORDER BY created_at DESC LIMIT ?",
                             (cfg.campaign_id, dec.shed)).fetchall()
            for r in rows:
                store.cancel_call(c, r, "SHED_BY_SAFETY", now, cfg)
        created = []
        for i in range(dec.approved):
            call_id = uuid.uuid4().hex
            b = store.reserve_borrower(c, cfg.campaign_id, now, now + cfg.reservation_lease)
            if b is None:
                break
            agent = None
            if dec.bind_agents:
                agent = store.reserve_agent(c, cfg.campaign_id, call_id, now, now + cfg.reservation_lease,
                                            cfg.agent_stale_after)
                if agent is None:                      # lost the race for the last agent: give the borrower back
                    store.borrower_requeue(c, b["id"], now, cfg, consume_attempt=False, delay=0)
                    break
            c.execute("INSERT INTO calls(id, campaign_id, borrower_id, agent_id, provider, state, bound, idem_key, "
                      "created_at, lease_expires) VALUES (?,?,?,?,?, 'RESERVED', ?, ?, ?, ?)",
                      (call_id, cfg.campaign_id, b["id"], agent, (dec.probe_provider if i == 0 and dec.probe_provider else dec.provider),
                       1 if agent else 0,
                       f"{b['id']}:{b['dial_seq']}", now, now + cfg.reservation_lease))
            created.append(call_id)
        return created
