# Simulation results (1800s virtual time, 100 agents, seed 7)

Utilization = mean(CONNECTED agents / online agents) after a 300s warm-up. Abandon rate = abandoned / answered.

| Scenario | Mode | Utilization | Abandon rate | Initiated | Connected | Conn/agent-hr | Safety actions | Invariant violations |
|---|---|---|---|---|---|---|---|---|
| A (answer 20%, talk 120s) | progressive | 49.3% | 0.00% | 3827 | 783 | 15.66 | {'APPROVE': 1800} | 0 |
| A (answer 20%, talk 120s) | predictive | 70.0% | 0.00% | 8672 | 1093 | 21.86 | {'FALLBACK_PROGRESSIVE': 18, 'REDUCE': 1254, 'REJECT': 528} | 0 |
| B (answer 50%, talk 90s) | progressive | 63.5% | 0.00% | 2516 | 1304 | 26.08 | {'APPROVE': 1800} | 0 |
| B (answer 50%, talk 90s) | predictive | 74.4% | 0.59% | 3263 | 1515 | 30.3 | {'FALLBACK_PROGRESSIVE': 221, 'REDUCE': 1057, 'REJECT': 522} | 0 |
| C (answer 70%, talk 180s) | progressive | 81.9% | 0.00% | 1236 | 857 | 17.14 | {'APPROVE': 1800} | 0 |
| C (answer 70%, talk 180s) | predictive | 84.4% | 2.23% | 1315 | 875 | 17.5 | {'FALLBACK_PROGRESSIVE': 1067, 'REDUCE': 363, 'REJECT': 370} | 0 |
| D (changing answer rate + provider-A outa) | progressive | 61.4% | 0.00% | 2693 | 1020 | 20.4 | {'APPROVE': 1800} | 0 |
| D (changing answer rate + provider-A outa) | predictive | 66.9% | 1.03% | 3469 | 1156 | 23.12 | {'FALLBACK_PROGRESSIVE': 602, 'REDUCE': 718, 'REJECT': 480} | 0 |
| E (flaky provider B as primary: 5% timeou) | progressive | 56.6% | 0.00% | 2381 | 1172 | 23.44 | {'APPROVE': 1800} | 0 |
| E (flaky provider B as primary: 5% timeou) | predictive | 68.7% | 1.61% | 3246 | 1406 | 28.12 | {'FALLBACK_PROGRESSIVE': 648, 'REDUCE': 790, 'REJECT': 362} | 0 |

## Abandon-cap sensitivity (scenario B, predictive)

| cap | utilization | measured abandon | connects/agent-hr |
|---|---|---|---|
| 1% | 72.2% | 0.20% | 29.52 |
| 3% | 74.4% | 0.59% | 30.3 |
| 5% | 75.0% | 0.58% | 30.78 |
