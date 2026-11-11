"""Reproducible local fault matrix; state is graded independently of assistant text."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import tempfile
import time
from pathlib import Path

from .core import Bridge
from .fixtures import FAULTS, PROFILES, make_scenario, naive_baseline


def run_case(profile: str, fault: str, seed: int, n: int = 1000) -> dict:
    scenario = make_scenario(profile, fault, seed, n)
    with tempfile.TemporaryDirectory() as tmp:
        bridge = Bridge(Path(tmp) / "bridge.sqlite", scenario["initial"], rate_limit=fault == "rate_limits")
        start = time.perf_counter()
        for event in scenario["deliveries"]:
            bridge.ingest(event)
        ingest_seconds = time.perf_counter() - start
        before = bridge.counts()
        report = bridge.promote(
            scenario["candidate"], checks=scenario["checks"], approved=True,
        )
        final = bridge.counts()
        effects = {row[0] for row in bridge.db.execute("SELECT event_id FROM effects")}
        actual = bridge.state()
        expected = {
            key: {"seq": value["seq"], "value": value["value"]}
            for key, value in scenario["true_state"].items()
        }
        valid = scenario["valid_ids"]
        silent_loss = len(valid - effects - {
            row[0] for row in bridge.db.execute(
                "SELECT event_id FROM raw WHERE outcome='dead_letter'"
            )
        })
        wrong_tenant = bridge.db.execute(
            "SELECT count(*) FROM effects WHERE tenant!=?", (profile,)
        ).fetchone()[0]
        result = {
            "profile": profile, "fault": fault, "seed": seed, "logical_events": n,
            "deliveries": len(scenario["deliveries"]),
            "before": before, "after": final,
            "candidate_accepted": report.passed,
            "rejection_reasons": report.reasons,
            "state_mismatches": sum(actual.get(k) != v for k, v in expected.items()),
            "silent_loss": silent_loss,
            "wrong_tenant_writes": wrong_tenant,
            "duplicate_effects": final["effects"] - len(effects),
            "valid_unapplied": len(valid - effects),
            "invalid_visible_in_dlq": final.get("dead_letter", 0) >= scenario["invalid"],
            "ingest_seconds": ingest_seconds,
            "baseline": naive_baseline(scenario),
        }
        bridge.close()
        return result


def run_matrix(n: int = 1000, seeds: range = range(5)) -> dict:
    cases = [
        run_case(profile, fault, seed, n)
        for profile in PROFILES for fault in FAULTS for seed in seeds
    ]
    return {
        "environment": {
            "python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.machine(), "load_average": os.getloadavg(),
        },
        "command": f"python -m fieldbridge evaluate --events {n} --seeds {len(seeds)}",
        "profiles": list(PROFILES), "faults": list(FAULTS),
        "seeds": list(seeds), "cases": cases,
        "summary": {
            "runs": len(cases),
            "logical_events": sum(c["logical_events"] for c in cases),
            "deliveries": sum(c["deliveries"] for c in cases),
            "silent_loss": sum(c["silent_loss"] for c in cases),
            "valid_unapplied": sum(c["valid_unapplied"] for c in cases),
            "state_mismatches": sum(c["state_mismatches"] for c in cases),
            "wrong_tenant_writes": sum(c["wrong_tenant_writes"] for c in cases),
            "bad_mapping_rejected": sum(
                not c["candidate_accepted"]
                for c in cases if c["fault"] == "bad_tenant_mapping"
            ),
            "bad_mapping_total": sum(
                c["fault"] == "bad_tenant_mapping" for c in cases
            ),
            "baseline_state_mismatches": sum(
                c["baseline"]["state_mismatches"] for c in cases
            ),
            "baseline_silent_drops": sum(
                c["baseline"]["silent_drops"] for c in cases
            ),
            "baseline_duplicate_effects": sum(
                c["baseline"]["duplicate_effects"] for c in cases
            ),
            "baseline_wrong_tenant_writes": sum(
                c["baseline"]["wrong_tenant_writes"] for c in cases
            ),
        },
    }
