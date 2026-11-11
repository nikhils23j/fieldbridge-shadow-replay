"""Read-only LangGraph diagnostic assistant; deterministic verifier owns cutover."""

from __future__ import annotations

import json
import re
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .core import Bridge
from .fixtures import mappings


class DiagnoseState(TypedDict, total=False):
    profile: str
    counts: dict
    schemas: dict
    candidate: str
    reason: str
    passed: bool
    verifier_reasons: list[str]


class MLXDiagnostic:
    def __init__(self, model_id: str = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"):
        from mlx_lm import load

        self.model_id = model_id
        self.model, self.tokenizer = load(model_id)

    def __call__(self, evidence: dict) -> tuple[str, str]:
        from mlx_lm import generate

        prompt = self.tokenizer.apply_chat_template(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a read-only integration diagnostician. Choose one of "
                        "two predeclared mapping candidates: keep or v1-v2. If v2 events "
                        "are dead-lettered because v1 is the only supported schema, "
                        "choose v1-v2. Otherwise choose keep. Return only JSON with "
                        "candidate and reason. You cannot approve deployment."
                    ),
                },
                {"role": "user", "content": json.dumps(evidence, sort_keys=True)},
            ],
            add_generation_prompt=True, tokenize=False,
        )
        raw = generate(
            self.model, self.tokenizer, prompt=prompt, max_tokens=100, verbose=False,
        )
        match = re.search(r"\{.*?\}", raw, re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                choice = value.get("candidate")
                if choice in ("keep", "v1-v2"):
                    return choice, str(value.get("reason", ""))[:300]
            except json.JSONDecodeError:
                pass
        return "keep", "Model response was not a valid candidate."


class DiagnosticGraph:
    def __init__(self, bridge: Bridge, saver: SqliteSaver, diagnostic: MLXDiagnostic):
        self.bridge = bridge
        self.diagnostic = diagnostic
        builder = StateGraph(DiagnoseState)
        builder.add_node("inspect", self._inspect)
        builder.add_node("propose", self._propose)
        builder.add_node("verify", self._verify)
        builder.add_edge(START, "inspect")
        builder.add_edge("inspect", "propose")
        builder.add_edge("propose", "verify")
        builder.add_edge("verify", END)
        self.graph = builder.compile(checkpointer=saver)

    def _inspect(self, state: DiagnoseState) -> dict:
        rows = self.bridge.db.execute(
            "SELECT schema,outcome,count(*) AS n FROM raw GROUP BY schema,outcome"
        ).fetchall()
        return {
            "counts": self.bridge.counts(),
            "schemas": {f"{r['schema']}/{r['outcome']}": r["n"] for r in rows},
        }

    def _propose(self, state: DiagnoseState) -> dict:
        candidate, reason = self.diagnostic({
            "profile": state["profile"],
            "active_mapping": self.bridge.mapping.name,
            "counts": state["counts"], "schema_outcomes": state["schemas"],
        })
        return {"candidate": candidate, "reason": reason}

    def _verify(self, state: DiagnoseState) -> dict:
        initial, good, _ = mappings(state["profile"])
        candidate = good if state["candidate"] == "v1-v2" else initial
        report = self.bridge.shadow(candidate)
        return {"passed": report.passed, "verifier_reasons": report.reasons}

    def run(self, profile: str, thread_id: str = "diagnosis") -> DiagnoseState:
        return self.graph.invoke(
            {"profile": profile},
            {"configurable": {"thread_id": thread_id}},
        )
