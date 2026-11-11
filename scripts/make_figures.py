"""Render benchmark figures from results/evaluation.json; no hard-coded measurements."""

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
ax.legend()
fig.tight_layout()
out = ROOT / "docs/img/fault_matrix.png"
out.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(out, dpi=170)
print(out)
