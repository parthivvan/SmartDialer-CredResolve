"""Discrete-time simulator driving the REAL dialer/safety/event code on a virtual clock.

  python -m smartdialer.sim            # runs all scenarios, writes results/simulation.json + results/simulation.md
"""
import argparse
import json
import os
import random
from dataclasses import replace

from .clock import SimClock
from .config import Config
from .factory import build_system, seed
from .providers.mock import provider_a, provider_b
from .store import check_invariants, agent_offline


def const(x):
    return lambda t: x


class Scenario:
    def __init__(self, name, answer, talk, desc, primary="A", faults=None, agents=100, duration=1800):
        self.name, self.answer, self.talk, self.desc = name, answer, talk, desc
        self.primary, self.faults, self.agents, self.duration = primary, faults or {}, agents, duration


def scenarios():
    # D: answer rate ramps 0.3 -> 0.7 (t=300..600), crashes to 0.1 at t=1100, recovers to 0.5 at t=1400
    def d_answer(t):
        if t < 300: return 0.3
        if t < 600: return 0.3 + 0.4 * (t - 300) / 300
        if t < 1100: return 0.7
        if t < 1400: return 0.1
        return 0.5
    return [
        Scenario("A", const(0.2), 120, "answer 20%, talk 120s"),
        Scenario("B", const(0.5), 90, "answer 50%, talk 90s"),
        Scenario("C", const(0.7), 180, "answer 70%, talk 180s"),
        Scenario("D", d_answer, 100, "changing answer rate + provider-A outage t=800-880 + 40 agents drop at t=1500",
                 faults={"outage": (800, 880), "drop_agents": (1500, 40)}),
        Scenario("E", const(0.5), 90, "flaky provider B as primary: 5% timeouts, 15% duplicate events, 6s reorder jitter",
                 primary="B"),
    ]


class Sim:
    def __init__(self, sc: Scenario, mode: str, cfg: Config = None, seed_=7, warmup=300):
        self.sc, self.mode, self.warmup = sc, mode, warmup
        order = ("A", "B") if sc.primary == "A" else ("B", "A")
        self.cfg = replace(cfg or Config(), provider_order=order, retry_delay=120.0)
        self.clock = SimClock()
        self.t0 = self.clock.now()
        pa = provider_a(self.clock, seed=seed_, answer_fn=lambda t: sc.answer(t - self.t0), talk_mean=sc.talk)
        pb = provider_b(self.clock, seed=seed_ + 1, answer_fn=lambda t: sc.answer(t - self.t0), talk_mean=sc.talk)
        self.system = build_system(mode, self.cfg, self.clock, {"A": pa, "B": pb})
        seed(self.system, sc.agents, 60000)
        self.dialer = self.system.dialer
        self.timeline, self.util_sum, self.util_n, self.violations = [], 0.0, 0, []

    def run(self):
        for t in range(self.sc.duration):
            self.step(t)
        return self.summary()

    def step(self, t):
        sc, sysm = self.sc, self.system
        prov_a = sysm.providers["A"]
        self.clock.advance(1)
        with sysm.db.tx() as c:
            c.execute("UPDATE agents SET last_seen=? WHERE state!='OFFLINE'", (self.clock.now(),))
        if "outage" in sc.faults:
            a, b = sc.faults["outage"]
            prov_a.outage = a <= t < b
        if "drop_agents" in sc.faults and t == sc.faults["drop_agents"][0]:
            self.drop_agents(sc.faults["drop_agents"][1])
        sysm.pump_events()
        if t % 5 == 0:
            sysm.reconciler.run_once()
        dec, _ = self.dialer.run_cycle()
        self._observe(t, dec)
        if t % 20 == 0:
            with sysm.db.tx() as c:
                v = check_invariants(c)
            if v:
                self.violations.extend(v)
        return dec

    def drop_agents(self, n):
        with self.system.db.tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM agents WHERE state!='OFFLINE' ORDER BY RANDOM() LIMIT ?", (n,))]
            for i in ids:
                agent_offline(c, i, self.clock.now(), self.cfg)

    def _observe(self, t, dec):
        with self.system.db.tx() as c:
            by = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) n FROM agents GROUP BY state")}
        online = sum(n for s, n in by.items() if s != "OFFLINE")
        conn = by.get("CONNECTED", 0)
        if t >= self.warmup and online:
            self.util_sum += conn / online
            self.util_n += 1
        if t % 30 == 0:
            self.timeline.append({"t": t, "p_true": round(self.sc.answer(t), 3), "online": online, "connected": conn,
                                  "available": by.get("AVAILABLE", 0), "action": dec.action,
                                  "requested": dec.requested, "approved": dec.approved, "shed": dec.shed})

    def summary(self):
        with self.system.db.tx() as c:
            q = lambda s, *a: c.execute(s, a).fetchone()[0]
            placed = q("SELECT COUNT(*) FROM calls WHERE initiated_at IS NOT NULL")
            answered = q("SELECT COUNT(*) FROM calls WHERE answered_at IS NOT NULL")
            abandoned = q("SELECT COUNT(*) FROM calls WHERE outcome='ABANDONED'")
            connected = q("SELECT COUNT(*) FROM calls WHERE connected_at IS NOT NULL")
            acts = {r[0]: r[1] for r in c.execute("SELECT action, COUNT(*) FROM safety_decisions GROUP BY action")}
            disp = {r[0]: r[1] for r in c.execute("SELECT disposition, COUNT(*) FROM call_events GROUP BY disposition")}
            dupes = q("SELECT COALESCE(SUM(n),0) FROM event_dupes")
            fails = {r[0]: r[1] for r in c.execute("SELECT fail_reason, COUNT(*) FROM calls WHERE state='FAILED' AND fail_reason IS NOT NULL GROUP BY fail_reason")}
            if dupes:
                disp["DUPLICATE"] = dupes
            reasons = {}
            for r in c.execute("SELECT reasons, COUNT(*) n FROM safety_decisions WHERE reasons!='' GROUP BY reasons"):
                for x in r[0].split(","):
                    reasons[x] = reasons.get(x, 0) + r[1]
        return {"scenario": self.sc.name, "mode": self.mode, "desc": self.sc.desc,
                "utilization": round(self.util_sum / max(1, self.util_n), 4),
                "calls_initiated": placed, "calls_answered": answered, "calls_connected": connected,
                "abandoned": abandoned, "abandon_rate": round(abandoned / answered, 4) if answered else 0.0,
                "connected_per_agent_hour": round(connected / (self.sc.agents * self.sc.duration / 3600), 2),
                "safety_actions": acts, "safety_reasons": reasons, "event_dispositions": disp,
                "failed_calls_by_reason": fails, "invariant_violations": len(self.violations),
                "timeline": self.timeline}


def run_all(duration=1800, out_dir="results", caps=(0.01, 0.03, 0.05)):
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for sc in scenarios():
        sc.duration = duration
        for mode in ("progressive", "predictive"):
            r = Sim(sc, mode).run()
            results.append(r)
            print(f"{sc.name} {mode:11s} util={r['utilization']:.1%} abandon={r['abandon_rate']:.2%} "
                  f"initiated={r['calls_initiated']} connected={r['calls_connected']} violations={r['invariant_violations']}")
    sens = []
    sc = scenarios()[1]
    sc.duration = duration
    for cap in caps:
        r = Sim(sc, "predictive", Config(abandon_cap=cap)).run()
        sens.append({"abandon_cap": cap, "utilization": r["utilization"], "abandon_rate": r["abandon_rate"],
                     "connected_per_agent_hour": r["connected_per_agent_hour"]})
        print(f"cap={cap:.0%}: util={r['utilization']:.1%} abandon={r['abandon_rate']:.2%}")
    with open(os.path.join(out_dir, "simulation.json"), "w") as f:
        json.dump({"results": results, "cap_sensitivity_scenario_B": sens}, f, indent=1)
    write_md(results, sens, os.path.join(out_dir, "simulation.md"), duration)
    return results, sens


def write_md(results, sens, path, duration):
    L = [f"# Simulation results ({duration}s virtual time, 100 agents, seed 7)\n",
         "Utilization = mean(CONNECTED agents / online agents) after a 300s warm-up. Abandon rate = abandoned / answered.\n",
         "| Scenario | Mode | Utilization | Abandon rate | Initiated | Connected | Conn/agent-hr | Safety actions | Invariant violations |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r['scenario']} ({r['desc'][:38]}) | {r['mode']} | {r['utilization']:.1%} | {r['abandon_rate']:.2%} | "
                 f"{r['calls_initiated']} | {r['calls_connected']} | {r['connected_per_agent_hour']} | "
                 f"{r['safety_actions']} | {r['invariant_violations']} |")
    L += ["\n## Abandon-cap sensitivity (scenario B, predictive)\n", "| cap | utilization | measured abandon | connects/agent-hr |", "|---|---|---|---|"]
    for s in sens:
        L.append(f"| {s['abandon_cap']:.0%} | {s['utilization']:.1%} | {s['abandon_rate']:.2%} | {s['connected_per_agent_hour']} |")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=1800)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    run_all(a.duration, a.out)
