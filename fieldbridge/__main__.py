"""CLI: run the fault matrix, local HTTP demonstration and model diagnosis."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from langgraph.checkpoint.sqlite import SqliteSaver

from .assistant import DiagnosticGraph, MLXDiagnostic
from .benchmark import run_matrix
from .core import Bridge
from .fixtures import make_scenario
from .http_api import serve_in_thread


def demo(output: Path, use_model: bool = False) -> dict:
    scenario = make_scenario("atlas", "schema_drift", seed=7, n=100)
    with tempfile.TemporaryDirectory() as tmp:
        bridge = Bridge(Path(tmp) / "bridge.sqlite", scenario["initial"])
        server, thread = serve_in_thread(bridge)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for event in scenario["events"]:
                request = Request(
                    base + "/source/atlas/events", data=json.dumps(event).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request) as response:
                    response.read()
            with urlopen(base + "/integration/status") as response:
                before = json.load(response)
            assistant_result = None
            if use_model:
                with SqliteSaver.from_conn_string(str(Path(tmp) / "diagnosis.sqlite")) as saver:
                    assistant_result = DiagnosticGraph(
                        bridge, saver, MLXDiagnostic(),
                    ).run("atlas")
            bad = bridge.promote(
                scenario["candidate"].__class__(
                    "bad-value", scenario["candidate"].versions, value_delta=1
                ),
                checks=scenario["checks"], approved=True,
            )
            good = bridge.promote(
                scenario["candidate"], checks=scenario["checks"], approved=True,
            )
            with urlopen(base + "/integration/status") as response:
                after = json.load(response)
            result = {
                "before": before, "bad_candidate_rejected": not bad.passed,
                "bad_candidate_reasons": bad.reasons,
                "good_candidate_promoted": good.passed, "after": after,
                "assistant": assistant_result,
                "customer_query_matches": all(
                    bridge.state()[key]["value"] == value
                    for key, value in scenario["checks"].items()
                ),
            }
        finally:
            server.shutdown()
            thread.join()
            bridge.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    handoff = output.parent.parent / "docs" / "handoff_example.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "# Synthetic customer handoff: Atlas invoice events\n\n"
        "## Customer-visible failure\n\n"
        f"The v1 connector dead-lettered {before['counts'].get('dead_letter', 0)} "
        "v2 webhook events after a field rename. This is a local synthetic scenario.\n\n"
        "## Evidence and change\n\n"
        "The durable raw log retained the payloads. Shadow replay tested the versioned "
        "v1+v2 mapping against tenant boundaries and twelve customer query checks. "
        f"A deliberately corrupted mapping was rejected ({', '.join(bad.reasons)}). "
        f"Approved cutover replayed the log; {after['counts'].get('accepted', 0)} "
        "logical events have one destination effect each.\n\n"
        "## Residual risk\n\n"
        "The destination is a local SQLite transaction. An external API would need "
        "staged writes or compensation for true rollback. The customer fixtures are synthetic.\n\n"
        "## Reproduce\n\npython -m fieldbridge demo\n"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--events", type=int, default=1000)
    evaluate.add_argument("--seeds", type=int, default=5)
    evaluate.add_argument("--output", type=Path, default=Path("results/evaluation.json"))
    demonstration = sub.add_parser("demo")
    demonstration.add_argument("--model", action="store_true")
    demonstration.add_argument("--output", type=Path, default=Path("results/demo.json"))
    args = parser.parse_args()
    if args.command == "evaluate":
        result = run_matrix(args.events, range(args.seeds))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result["summary"], indent=2))
    else:
        print(json.dumps(demo(args.output, args.model), indent=2))


if __name__ == "__main__":
    main()
