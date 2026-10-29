"""Provider circuit breaker, stored in the DB so every worker sees the same state."""


def log_dispatch(c, provider, now, ok):
    c.execute("INSERT INTO dispatch_log(provider, ts, ok) VALUES(?,?,?)", (provider, now, 1 if ok else 0))


def refresh_breaker(c, provider, now, cfg):
    b = c.execute("SELECT opened_at FROM breaker WHERE provider=?", (provider,)).fetchone()
    if b is None:
        rows = c.execute("SELECT ok FROM dispatch_log WHERE provider=? AND ts>=?",
                         (provider, now - cfg.breaker_window)).fetchall()
        n, fails = len(rows), sum(1 for r in rows if not r["ok"])
        if n >= cfg.breaker_min_samples and fails / n >= cfg.breaker_fail_rate:
            c.execute("INSERT OR REPLACE INTO breaker(provider, state, opened_at) VALUES(?,?,?)",
                      (provider, "OPEN", now))
            return "OPEN"
        return "CLOSED"
    opened = b["opened_at"]
    if now - opened < cfg.breaker_cooldown:
        return "OPEN"
    probes = c.execute("SELECT ok FROM dispatch_log WHERE provider=? AND ts>=?",
                       (provider, opened + cfg.breaker_cooldown)).fetchall()
    if not probes:
        return "HALF_OPEN"                              # allow a single probe call
    if any(p["ok"] for p in probes):                     # probe succeeded -> close and forget old failures
        c.execute("DELETE FROM breaker WHERE provider=?", (provider,))
        c.execute("DELETE FROM dispatch_log WHERE provider=?", (provider,))
        return "CLOSED"
    c.execute("UPDATE breaker SET opened_at=? WHERE provider=?", (now, provider))   # probe failed -> re-open
    return "OPEN"
