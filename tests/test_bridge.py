import pytest

from fieldbridge.core import Bridge, Mapping
from fieldbridge.fixtures import FAULTS, PROFILES, make_scenario


def _run(tmp_path, profile, fault, n=100):
    scenario = make_scenario(profile, fault, 2, n)
    bridge = Bridge(tmp_path / "bridge.sqlite", scenario["initial"], rate_limit=fault == "rate_limits")
    for event in scenario["deliveries"]:
        bridge.ingest(event)
    report = bridge.promote(scenario["candidate"], checks=scenario["checks"], approved=True)
    return bridge, scenario, report


def test_schema_drift_recovered_by_shadow_cutover(tmp_path):
    bridge, scenario, report = _run(tmp_path, "atlas", "schema_drift")
    assert report.passed
    assert bridge.counts()["effects"] == 100
    assert bridge.counts().get("dead_letter", 0) == 0
    assert all(
        bridge.state()[k] == {"seq": value["seq"], "value": value["value"]}
        for k, value in scenario["true_state"].items()
    )
    bridge.close()


def test_bad_tenant_mapping_rejected_before_cutover(tmp_path):
    bridge, scenario, report = _run(tmp_path, "beacon", "bad_tenant_mapping")
    assert not report.passed
    assert "tenant boundary mismatch" in report.reasons
    assert bridge.mapping.name == scenario["initial"].name
    assert bridge.db.execute(
        "SELECT count(*) FROM effects WHERE tenant!=?", ("beacon",)
    ).fetchone()[0] == 0
    bridge.close()


def test_duplicate_and_out_of_order_events_converge(tmp_path):
    for fault in ("retry_duplicates", "reordering"):
        path = tmp_path / (fault + ".sqlite")
        scenario = make_scenario("cinder", fault, 3, 100)
        bridge = Bridge(path, scenario["initial"])
        for event in scenario["deliveries"]:
            bridge.ingest(event)
        assert bridge.counts()["effects"] == 100
        assert all(
            bridge.state()[k] == {"seq": value["seq"], "value": value["value"]}
            for k, value in scenario["true_state"].items()
        )
        bridge.close()


def test_same_external_id_in_two_tenants_is_not_a_duplicate(tmp_path):
    scenario = make_scenario("atlas", "retry_duplicates", 3, 20)
    bridge = Bridge(tmp_path / "db.sqlite", scenario["initial"])
    first = scenario["events"][0]
    other = {
        **first, "tenant": "second-tenant",
        "body": {**first["body"], "tenant": "second-tenant"},
    }
    assert bridge.ingest(first) == "accepted"
    assert bridge.ingest(other) == "accepted"
    assert bridge.counts()["effects"] == 2
    assert len(bridge.state()) == 2
    bridge.close()


def test_missing_fields_are_visible_in_dead_letter(tmp_path):
    bridge, scenario, report = _run(tmp_path, "delta", "missing_fields")
    assert not report.passed
    assert bridge.counts()["dead_letter"] == scenario["invalid"]
    assert bridge.counts()["effects"] + bridge.counts()["dead_letter"] == 100
    bridge.close()


def test_429_retries_and_rollback(tmp_path):
    bridge, scenario, report = _run(tmp_path, "atlas", "rate_limits")
    assert report.passed
    assert bridge.counts()["retries"] > 0
    assert bridge.counts()["effects"] == 100
    assert bridge.mapping.name == scenario["candidate"].name
    bridge.rollback()
    assert bridge.mapping.name == scenario["initial"].name
    assert bridge.counts()["effects"] == 100
    bridge.close()


def test_approval_required_and_atomic_rejection(tmp_path):
    scenario = make_scenario("atlas", "schema_drift", 4, 100)
    bridge = Bridge(tmp_path / "db.sqlite", scenario["initial"])
    for event in scenario["events"]:
        bridge.ingest(event)
    old_state = bridge.state()
    unapproved = bridge.promote(scenario["candidate"], checks=scenario["checks"])
    assert not unapproved.passed
    assert bridge.state() == old_state
    corrupt = Mapping(
        "bad-value", scenario["candidate"].versions, value_delta=1,
    )
    bad = bridge.promote(corrupt, checks=scenario["checks"], approved=True)
    assert not bad.passed
    assert bridge.state() == old_state
    bridge.close()


def test_failed_promotion_rolls_back_entire_local_transaction(tmp_path):
    scenario = make_scenario("atlas", "schema_drift", 5, 100)
    bridge = Bridge(tmp_path / "db.sqlite", scenario["initial"])
    for event in scenario["events"]:
        bridge.ingest(event)
    old_state = bridge.state()
    old_counts = bridge.counts()
    with pytest.raises(RuntimeError, match="injected failure"):
        bridge.promote(
            scenario["candidate"], checks=scenario["checks"],
            approved=True, inject_failure_after=75,
        )
    assert bridge.mapping.name == scenario["initial"].name
    assert bridge.state() == old_state
    assert bridge.counts() == old_counts
    bridge.close()
