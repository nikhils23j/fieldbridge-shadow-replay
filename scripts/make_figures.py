"""Render benchmark and cutover figures from saved result JSON."""

import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
cases = json.loads((ROOT / "results/evaluation.json").read_text())["cases"]
faults = list(dict.fromkeys(case["fault"] for case in cases))
baseline = defaultdict(int)
fieldbridge = defaultdict(int)
for case in cases:
    baseline[case["fault"]] += case["baseline"]["state_mismatches"]
    fieldbridge[case["fault"]] += case["state_mismatches"]

fig, ax = plt.subplots(figsize=(9, 4.5))
x = range(len(faults))
ax.bar([i - 0.2 for i in x], [baseline[f] for f in faults], 0.4, label="Direct connector")
ax.bar([i + 0.2 for i in x], [fieldbridge[f] for f in faults], 0.4, label="FieldBridge")
ax.set_xticks(list(x), [f.replace("_", "\n") for f in faults])
ax.set_ylabel("Final entity state mismatches")
ax.set_title("Local synthetic fault matrix")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=2)
fig.tight_layout()
out = ROOT / "docs/img/fault_matrix.png"
out.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(out, dpi=170)
print(out)
plt.close(fig)

demo = json.loads((ROOT / "results/demo.json").read_text())
stages = ["Before mapping repair", "After approved cutover"]
accepted = [demo["before"]["counts"].get("accepted", 0), demo["after"]["counts"].get("accepted", 0)]
dead_letters = [demo["before"]["counts"].get("dead_letter", 0), demo["after"]["counts"].get("dead_letter", 0)]
fig, ax = plt.subplots(figsize=(9, 3.2))
positions = range(len(stages))
ax.barh(positions, accepted, color="#248a60", label="Accepted effects")
ax.barh(positions, dead_letters, left=accepted, color="#e6a33b", label="Visible dead letters")
for pos, (good, dead) in enumerate(zip(accepted, dead_letters)):
    ax.text(good / 2, pos, str(good), ha="center", va="center", color="white", fontweight="bold")
    if dead:
        ax.text(good + dead / 2, pos, str(dead), ha="center", va="center", color="#292929", fontweight="bold")
ax.set_yticks(list(positions), stages)
ax.invert_yaxis()
ax.set_xlim(0, max(g + d for g, d in zip(accepted, dead_letters)) * 1.08)
ax.set_xlabel("Events in the local 100-event demonstration")
ax.set_title("Schema repair made rejected events visible, then recoverable")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2)
fig.tight_layout()
out = ROOT / "docs/img/demo_recovery.png"
fig.savefig(out, dpi=170)
print(out)
