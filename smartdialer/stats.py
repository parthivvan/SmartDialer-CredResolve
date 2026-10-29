from math import sqrt

from .models import Stats


def wilson_upper(k, n, z):
    if n == 0:
        return 1.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + adj) / d)


def compute_stats(c, cfg) -> Stats:
    rows = c.execute("SELECT (answered_at IS NOT NULL) AS ans, answered_at - initiated_at AS setup FROM calls "
                     "WHERE campaign_id=? AND resolved_at IS NOT NULL AND initiated_at IS NOT NULL "
                     "ORDER BY resolved_at DESC LIMIT ?", (cfg.campaign_id, cfg.stats_window)).fetchall()
    n = len(rows)
    k = sum(r["ans"] for r in rows)
    short = rows[:cfg.short_window]
    ns, ks = len(short), sum(r["ans"] for r in short)
    p_long = (k + cfg.prior_p * cfg.prior_strength) / (n + cfg.prior_strength)
    # pacing estimate: blend long and short window so it reacts to regime changes
    p_hat = p_long if ns < 15 else 0.5 * p_long + 0.5 * (ks / ns)
    # safety estimate: the DANGEROUS direction is answer rate going UP, so take the larger upper bound
    p_up = wilson_upper(k, n, 1.645)
    if ns >= 10:
        p_up = max(p_up, wilson_upper(ks, ns, 1.28))
    p_up = max(p_up, p_hat)
    setups = [r["setup"] for r in rows if r["ans"] and r["setup"] is not None]
    avg_setup = sum(setups) / len(setups) if setups else 10.0
    t = c.execute("SELECT AVG(d) a FROM (SELECT ended_at - connected_at AS d FROM calls WHERE campaign_id=? "
                  "AND state='COMPLETED' AND connected_at IS NOT NULL ORDER BY ended_at DESC LIMIT 50)",
                  (cfg.campaign_id,)).fetchone()
    avg_talk = t["a"] if t["a"] else 90.0
    ab = c.execute("SELECT COALESCE(SUM(outcome='ABANDONED'),0) a, COUNT(*) n FROM (SELECT outcome FROM calls "
                   "WHERE campaign_id=? AND answered_at IS NOT NULL ORDER BY answered_at DESC LIMIT ?)",
                   (cfg.campaign_id, cfg.abandon_window)).fetchone()
    return Stats(n=n, answered_n=k, p_hat=max(p_hat, cfg.min_p), p_up=p_up, avg_setup=avg_setup, avg_talk=avg_talk,
                 abandon_rate=(ab["a"] / ab["n"]) if ab["n"] else 0.0, abandon_n=ab["n"])
