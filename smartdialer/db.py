import contextlib
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, cycle_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, state TEXT NOT NULL, call_id TEXT,
  version INTEGER NOT NULL DEFAULT 0, state_since REAL NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0,
  lease_expires REAL, wrap_until REAL, pending TEXT);
CREATE INDEX IF NOT EXISTS ix_agents ON agents(campaign_id, state, state_since);
CREATE TABLE IF NOT EXISTS borrowers(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, phone TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, dial_seq INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0, callback INTEGER NOT NULL DEFAULT 0, lease_expires REAL);
CREATE INDEX IF NOT EXISTS ix_borrowers_pick ON borrowers(campaign_id, state, callback DESC, priority DESC, next_attempt_at, id);
CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, borrower_id TEXT NOT NULL, agent_id TEXT,
  provider TEXT NOT NULL, provider_call_id TEXT, state TEXT NOT NULL, bound INTEGER NOT NULL,
  idem_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, initiated_at REAL, answered_at REAL,
  connected_at REAL, resolved_at REAL, ended_at REAL, lease_expires REAL, fail_reason TEXT, outcome TEXT);
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_calls_resolved ON calls(campaign_id, resolved_at);
CREATE INDEX IF NOT EXISTS ix_calls_answered ON calls(campaign_id, answered_at);
CREATE TABLE IF NOT EXISTS call_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, call_ref TEXT NOT NULL,
  provider TEXT NOT NULL, event_type TEXT NOT NULL, provider_ts REAL, received_at REAL NOT NULL, disposition TEXT);
CREATE TABLE IF NOT EXISTS event_dupes(dedupe_key TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS safety_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, ts REAL, mode TEXT, action TEXT, requested INTEGER,
  approved INTEGER, shed INTEGER, bind_agents INTEGER, provider TEXT, reasons TEXT, trace TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, call_ref TEXT NOT NULL, provider TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_log(id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, ts REAL, ok INTEGER);
CREATE INDEX IF NOT EXISTS ix_dlog ON dispatch_log(provider, ts);
CREATE TABLE IF NOT EXISTS breaker(provider TEXT PRIMARY KEY, state TEXT, opened_at REAL);
"""


class Database:
    """One connection per thread. All writes go through tx(): BEGIN IMMEDIATE ... COMMIT.

    Portability: every state change in store.py is a compare-and-set (UPDATE ... WHERE state=expected), so it
    stays correct under PostgreSQL READ COMMITTED as well; BEGIN IMMEDIATE is replaced there by the
    per-campaign row lock taken in Dialer.run_cycle (SELECT ... FOR UPDATE).
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self.conn().executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=OFF")
            self._local.c = c
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")
