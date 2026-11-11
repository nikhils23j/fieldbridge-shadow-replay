"""Four synthetic customer contracts and reproducible webhook faults."""

from __future__ import annotations

import random

from .core import Mapping


PROFILES = {
    "atlas": {
        "outcome": "invoice amount",
        "v1": {"tenant": "tenant", "entity": "invoice_id", "seq": "revision", "value": "amount_cents"},
        "v2": {"tenant": "org", "entity": "invoice", "seq": "rev", "value": "cents"},
    },
    "beacon": {
        "outcome": "stock units",
        "v1": {"tenant": "workspace", "entity": "sku", "seq": "version", "value": "units"},
        "v2": {"tenant": "account", "entity": "product", "seq": "generation", "value": "on_hand"},
    },
    "cinder": {
        "outcome": "active seats",
        "v1": {"tenant": "customer", "entity": "plan_id", "seq": "update_no", "value": "seats"},
        "v2": {"tenant": "org_id", "entity": "plan", "seq": "revision", "value": "licensed_seats"},
    },
    "delta": {
        "outcome": "shipment progress",
        "v1": {"tenant": "shipper", "entity": "shipment_id", "seq": "step", "value": "progress"},
        "v2": {"tenant": "client", "entity": "shipment", "seq": "stage_no", "value": "stage"},
    },
}

FAULTS = (
    "schema_drift", "missing_fields", "retry_duplicates",
    "reordering", "rate_limits", "bad_tenant_mapping",
)


def mappings(profile: str) -> tuple[Mapping, Mapping, Mapping]:
    contract = PROFILES[profile]
    initial = Mapping(f"{profile}-v1", {"v1": contract["v1"]})
    candidate = Mapping(f"{profile}-v1-v2", {
        "v1": contract["v1"], "v2": contract["v2"],
    })
    bad = Mapping(f"{profile}-bad-tenant", {
        "v1": contract["v1"], "v2": contract["v2"],
    }, tenant_override="other-customer")
    return initial, candidate, bad


def make_scenario(profile: str, fault: str, seed: int, n: int = 1000) -> dict:
    if fault not in FAULTS:
        raise ValueError(fault)
    rng = random.Random(seed + 1009 * list(PROFILES).index(profile))
    contract = PROFILES[profile]
    tenant = profile
    seq_by_entity = {f"E{i:02d}": 0 for i in range(40)}
    events: list[dict] = []
    true_state: dict[str, dict] = {}
    valid_ids: set[str] = set()
    invalid = 0
    for i in range(n):
        entity = f"E{rng.randrange(40):02d}"
        seq_by_entity[entity] += 1
        schema = "v2" if fault == "schema_drift" and i >= n // 2 else "v1"
        spec = contract[schema]
        value = rng.randrange(1, 10001)
        body = {
            spec["tenant"]: tenant, spec["entity"]: entity,
            spec["seq"]: seq_by_entity[entity], spec["value"]: value,
        }
        if fault == "missing_fields" and i % 17 == 0:
            del body[spec["value"]]
            invalid += 1
        else:
            true_state[f"{tenant}/{entity}"] = {
                "tenant": tenant, "event_id": f"{seed:04x}{i:08x}",
                "entity": entity, "seq": seq_by_entity[entity], "value": value,
            }
            valid_ids.add(f"{seed:04x}{i:08x}")
        events.append({
            "event_id": f"{seed:04x}{i:08x}", "tenant": tenant,
            "profile": profile, "schema": schema, "body": body,
        })
    deliveries = list(events)
    if fault == "retry_duplicates":
        deliveries += [events[i] for i in range(0, n, 7)]
        rng.shuffle(deliveries)
    elif fault == "reordering":
        rng.shuffle(deliveries)
    initial, good, bad = mappings(profile)
    candidate = bad if fault == "bad_tenant_mapping" else good
    keys = sorted(true_state)
    selected = keys[:: max(1, len(keys) // 12)][:12]
    checks = {key: true_state[key]["value"] for key in selected}
    return {
        "profile": profile, "fault": fault, "seed": seed,
        "events": events, "deliveries": deliveries,
        "initial": initial, "candidate": candidate,
        "true_state": true_state, "invalid": invalid,
        "checks": checks, "valid_ids": valid_ids,
    }


def naive_baseline(scenario: dict) -> dict:
    """Direct hard cutover: no durable log, idempotency, tenant gate or order check."""
    fault = scenario["fault"]
    mapping = scenario["candidate"] if fault == "bad_tenant_mapping" else scenario["initial"]
    spec_by_version = mapping.versions
    state: dict[str, dict] = {}
    seen: set[str] = set()
    duplicate_effects = 0
    silent_drops = 0
    wrong_tenant = 0
    for event in scenario["deliveries"]:
        spec = spec_by_version.get(event["schema"])
        if spec is None:
            silent_drops += 1
            continue
        body = event["body"]
        try:
            tenant = mapping.tenant_override or str(body[spec["tenant"]])
            entity = str(body[spec["entity"]])
            seq = int(body[spec["seq"]])
            value = int(body[spec["value"]]) + mapping.value_delta
        except (KeyError, TypeError, ValueError):
            silent_drops += 1
            continue
        if tenant != event["tenant"]:
            wrong_tenant += 1
        if event["event_id"] in seen:
            duplicate_effects += 1
        seen.add(event["event_id"])
        # Last arrival wins, including stale reordered deliveries.
        state[f"{tenant}/{entity}"] = {"seq": seq, "value": value}
    mismatch = sum(
        state.get(key) != {"seq": value["seq"], "value": value["value"]}
        for key, value in scenario["true_state"].items()
    )
    return {
        "state_mismatches": mismatch, "silent_drops": silent_drops,
        "duplicate_effects": duplicate_effects, "wrong_tenant_writes": wrong_tenant,
    }
