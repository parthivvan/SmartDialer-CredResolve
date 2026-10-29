import os
import tempfile
from dataclasses import dataclass

from .allocator import CallAllocator
from .clock import SimClock
from .config import Config
from .db import Database
from .dialer import Dialer
from .events import EventHandler
from .pacing import PredictivePacing, ProgressivePacing
from .providers.mock import provider_a, provider_b
from .reconciler import Reconciler
from .safety import SafetyController


@dataclass
class System:
    db: Database
    clock: object
    cfg: Config
    providers: dict
    dialer: Dialer
    handler: EventHandler
    reconciler: Reconciler
    safety: SafetyController
    allocator: CallAllocator
    secret: bytes

    def new_worker(self, worker_id, mode=None):
        pacing = (PredictivePacing if (mode or self.mode) == "predictive" else ProgressivePacing)(self.cfg)
        return Dialer(worker_id, self.db, self.clock, self.cfg, pacing, self.safety, self.allocator, self.providers)

    mode: str = "predictive"

    def pump_events(self):
        """Stand-in for webhooks: drain every provider's delivered events into the idempotent handler."""
        n = 0
        for p in self.providers.values():
            for ev in p.poll_events():
                self.handler.handle(ev)
                n += 1
        return n


def build_system(mode="predictive", cfg=None, clock=None, providers=None, db_path=None):
    cfg = cfg or Config()
    clock = clock or SimClock()
    if db_path is None:
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="smartdialer_")
        os.close(fd)
        os.unlink(db_path)
    db = Database(db_path)
    providers = providers or {"A": provider_a(clock), "B": provider_b(clock)}
    secret = os.urandom(32)       # shared ONLY by SafetyController and CallAllocator; pacing never sees it
    safety = SafetyController(cfg, secret)
    allocator = CallAllocator(cfg, secret)
    pacing = (PredictivePacing if mode == "predictive" else ProgressivePacing)(cfg)
    dialer = Dialer("w1", db, clock, cfg, pacing, safety, allocator, providers)
    handler = EventHandler(db, clock, cfg)
    rec = Reconciler(db, clock, cfg, dialer, handler, providers)
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO campaigns(id) VALUES (?)", (cfg.campaign_id,))
    sysm = System(db, clock, cfg, providers, dialer, handler, rec, safety, allocator, secret)
    sysm.mode = mode
    return sysm


def seed(system, agents=10, borrowers=1000, agents_online=True):
    cfg, now = system.cfg, system.clock.now()
    with system.db.tx() as c:
        c.executemany("INSERT INTO agents(id, campaign_id, state, state_since, last_seen) VALUES (?,?,?,?,?)",
                      [(f"ag{i:04d}", cfg.campaign_id, "AVAILABLE" if agents_online else "OFFLINE", now, now)
                       for i in range(agents)])
        c.executemany("INSERT INTO borrowers(id, campaign_id, phone, priority) VALUES (?,?,?,?)",
                      [(f"b{i:06d}", cfg.campaign_id, f"+9199{i:08d}", i % 3) for i in range(borrowers)])
