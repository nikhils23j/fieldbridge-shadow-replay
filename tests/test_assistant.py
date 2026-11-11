from langgraph.checkpoint.sqlite import SqliteSaver

from fieldbridge.assistant import DiagnosticGraph
from fieldbridge.core import Bridge
from fieldbridge.fixtures import make_scenario


class DeclaredCandidate:
    def __call__(self, evidence):
        assert evidence["schema_outcomes"]["v2/dead_letter"] > 0
        return "v1-v2", "v2 schema is present in the durable log"


def test_read_only_diagnosis_recommends_but_cannot_promote(tmp_path):
    scenario = make_scenario("atlas", "schema_drift", 9, 40)
    bridge = Bridge(tmp_path / "bridge.sqlite", scenario["initial"])
    for event in scenario["events"]:
        bridge.ingest(event)
    before = bridge.state()
    with SqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as saver:
        result = DiagnosticGraph(bridge, saver, DeclaredCandidate()).run("atlas")
    assert result["candidate"] == "v1-v2"
    assert result["passed"]
    assert bridge.mapping.name == "atlas-v1"
    assert bridge.state() == before
    bridge.close()
