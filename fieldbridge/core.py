"""Durable ingress, shadow replay, gated promotion and local destination state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class MappingError(ValueError):
    pass


class RateLimited(RuntimeError):
    pass


@dataclass(frozen=True)
class Mapping:
    name: str
    versions: dict[str, dict[str, str]]
    tenant_override: str | None = None
    value_delta: int = 0

    def map(self, event: dict) -> dict:
        spec = self.versions.get(event["schema"])
        if spec is None:
            raise MappingError(f"unsupported schema {event['schema']}")
        body = event["body"]
        try:
            tenant = str(body[spec["tenant"]])
            entity = str(body[spec["entity"]])
            seq = int(body[spec["seq"]])
            value = int(body[spec["value"]]) + self.value_delta
        except (KeyError, TypeError, ValueError) as exc:
            raise MappingError(f"missing or invalid field: {exc}") from exc
        if self.tenant_override is not None:
            tenant = self.tenant_override
        if tenant != event["tenant"]:
            raise MappingError("tenant boundary mismatch")
        if seq < 1 or value < 0:
            raise MappingError("invalid sequence or value")
        return {
            "tenant": tenant, "event_id": event["event_id"], "entity": entity,
            "seq": seq, "value": value,
        }

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Mapping":
        return cls(**json.loads(raw))


@dataclass
class ShadowReport:
    passed: bool
    reasons: list[str]
    mapped: int
    dead_letter: int
    state: dict[str, dict]


class Bridge:
    def __init__(self, path: str | Path, initial: Mapping, *, rate_limit: bool = False):
        self.path = str(path)
        # The localhost demo serves requests on a separate thread. Its HTTPServer
        # handles them serially; the benchmark calls the bridge in one thread.
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw(
                event_id TEXT NOT NULL, tenant TEXT NOT NULL, profile TEXT NOT NULL,
                schema TEXT NOT NULL, body TEXT NOT NULL, deliveries INTEGER NOT NULL DEFAULT 1,
                outcome TEXT NOT NULL DEFAULT 'pending', reason TEXT,
                PRIMARY KEY(tenant,event_id));
            CREATE TABLE IF NOT EXISTS effects(
                event_id TEXT NOT NULL, tenant TEXT NOT NULL, entity TEXT NOT NULL,
                seq INTEGER NOT NULL, value INTEGER NOT NULL,
                PRIMARY KEY(tenant,event_id));
            CREATE TABLE IF NOT EXISTS destination(
                tenant TEXT NOT NULL, entity TEXT NOT NULL, seq INTEGER NOT NULL,
                value INTEGER NOT NULL, PRIMARY KEY(tenant,entity));
            CREATE TABLE IF NOT EXISTS mappings(
                version INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                spec TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, phase TEXT NOT NULL,
                detail TEXT NOT NULL);
            """
        )
        row = self.db.execute("SELECT version FROM mappings WHERE active=1").fetchone()
        if row is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO mappings(name,spec,active) VALUES (?,?,1)",
                    (initial.name, initial.to_json()),
                )
        self.rate_limit = rate_limit
        self.attempts: dict[tuple[str, str], int] = {}
        self.retry_count = 0

    def close(self) -> None:
        self.db.close()

    @property
    def mapping(self) -> Mapping:
        row = self.db.execute("SELECT spec FROM mappings WHERE active=1").fetchone()
        return Mapping.from_json(row["spec"])

    def _audit(self, phase: str, detail: dict) -> None:
        self.db.execute(
            "INSERT INTO audit(phase,detail) VALUES (?,?)",
            (phase, json.dumps(detail, sort_keys=True)),
        )

    def _event(self, row: sqlite3.Row) -> dict:
        return {
            "event_id": row["event_id"], "tenant": row["tenant"],
            "profile": row["profile"], "schema": row["schema"],
            "body": json.loads(row["body"]),
        }

    def ingest(self, event: dict) -> str:
        for key in ("event_id", "tenant", "profile", "schema", "body"):
            if key not in event:
                raise ValueError(f"missing envelope key {key}")
        with self.db:
            old = self.db.execute(
                "SELECT outcome FROM raw WHERE tenant=? AND event_id=?",
                (event["tenant"], event["event_id"]),
            ).fetchone()
            if old:
                self.db.execute(
                    "UPDATE raw SET deliveries=deliveries+1 WHERE tenant=? AND event_id=?",
                    (event["tenant"], event["event_id"]),
                )
                return "duplicate_delivery"
            self.db.execute(
                "INSERT INTO raw(event_id,tenant,profile,schema,body) VALUES (?,?,?,?,?)",
                (
                    event["event_id"], event["tenant"], event["profile"],
                    event["schema"], json.dumps(event["body"], sort_keys=True),
                ),
            )
        return self._deliver(event, self.mapping)

    def _apply(self, canonical: dict, *, simulate_rate_limit: bool) -> None:
        event_id = canonical["event_id"]
        throttled = hashlib.sha256(event_id.encode()).digest()[0] % 10 == 0
        if simulate_rate_limit and self.rate_limit and throttled:
            attempt_key = (canonical["tenant"], event_id)
            attempt = self.attempts.get(attempt_key, 0)
            self.attempts[attempt_key] = attempt + 1
            if attempt < 2:
                raise RateLimited("local destination returned 429")
        old = self.db.execute(
            "SELECT 1 FROM effects WHERE tenant=? AND event_id=?",
            (canonical["tenant"], event_id),
        ).fetchone()
        if old:
            return
        self.db.execute(
            "INSERT INTO effects VALUES (?,?,?,?,?)",
            (
                event_id, canonical["tenant"], canonical["entity"],
                canonical["seq"], canonical["value"],
            ),
        )
        state = self.db.execute(
            "SELECT seq FROM destination WHERE tenant=? AND entity=?",
            (canonical["tenant"], canonical["entity"]),
        ).fetchone()
        if state is None or canonical["seq"] > state["seq"]:
            self.db.execute(
                """INSERT INTO destination VALUES (?,?,?,?)
                ON CONFLICT(tenant,entity) DO UPDATE SET seq=excluded.seq,value=excluded.value""",
                (
                    canonical["tenant"], canonical["entity"],
                    canonical["seq"], canonical["value"],
                ),
            )

    def _deliver(self, event: dict, mapping: Mapping) -> str:
        try:
            canonical = mapping.map(event)
        except MappingError as exc:
            with self.db:
                self.db.execute(
                    "UPDATE raw SET outcome='dead_letter',reason=? WHERE tenant=? AND event_id=?",
                    (str(exc), event["tenant"], event["event_id"]),
                )
            return "dead_letter"
        for attempt in range(3):
            try:
                with self.db:
                    self._apply(canonical, simulate_rate_limit=True)
                    self.db.execute(
                        "UPDATE raw SET outcome='accepted',reason=NULL WHERE tenant=? AND event_id=?",
                        (event["tenant"], event["event_id"]),
                    )
                return "accepted"
            except RateLimited:
                self.retry_count += 1
                if attempt == 2:
                    with self.db:
                        self.db.execute(
                            "UPDATE raw SET outcome='dead_letter',reason='429 retry budget' WHERE tenant=? AND event_id=?",
                            (event["tenant"], event["event_id"]),
                        )
                    return "dead_letter"
        raise AssertionError("unreachable")

    def raw_events(self) -> list[dict]:
        return [self._event(row) for row in self.db.execute(
            "SELECT * FROM raw ORDER BY rowid"
        )]

    @staticmethod
    def project(events: list[dict], mapping: Mapping) -> tuple[dict, int, list[str]]:
        state: dict[str, dict] = {}
        seen: set[tuple[str, str]] = set()
        dead = 0
        reasons: list[str] = []
        for event in events:
            event_key = (event["tenant"], event["event_id"])
            if event_key in seen:
                continue
            seen.add(event_key)
            try:
                item = mapping.map(event)
            except MappingError as exc:
                dead += 1
                reasons.append(str(exc))
                continue
            key = f"{item['tenant']}/{item['entity']}"
            if key not in state or item["seq"] > state[key]["seq"]:
                state[key] = item
        return state, dead, reasons

    def shadow(
        self, candidate: Mapping, *, checks: dict[str, int] | None = None,
    ) -> ShadowReport:
        events = self.raw_events()
        before_state, before_dead, _ = self.project(events, self.mapping)
        state, dead, errors = self.project(events, candidate)
        reasons: list[str] = []
        # Existing accepted entities cannot disappear, and a candidate cannot increase
        # dead-letter volume during a repair.
        if dead > before_dead:
            reasons.append("candidate increases dead-letter volume")
        for key, old in before_state.items():
            new = state.get(key)
            if new is None or new["seq"] < old["seq"]:
                reasons.append(f"regressed existing entity {key}")
        if any("tenant boundary mismatch" in error for error in errors):
            reasons.append("tenant boundary mismatch")
        for key, expected in (checks or {}).items():
            if key not in state or state[key]["value"] != expected:
                reasons.append(f"customer query mismatch: {key}")
        # A migration must improve coverage if currently there are dead letters.
        if before_dead and dead >= before_dead:
            reasons.append("no coverage improvement")
        return ShadowReport(
            passed=not reasons, reasons=sorted(set(reasons)),
            mapped=len(events) - dead, dead_letter=dead, state=state,
        )

    def canary(self, candidate: Mapping, checks: dict[str, int] | None = None) -> ShadowReport:
        subset = self.raw_events()[: max(1, len(self.raw_events()) // 20)]
        state, dead, errors = self.project(subset, candidate)
        reasons = ["tenant boundary mismatch"] if any(
            "tenant boundary mismatch" in x for x in errors
        ) else []
        return ShadowReport(not reasons, reasons, len(subset) - dead, dead, state)

    def promote(
        self, candidate: Mapping, *,
        checks: dict[str, int] | None = None, approved: bool = False,
        inject_failure_after: int | None = None,
    ) -> ShadowReport:
        report = self.shadow(candidate, checks=checks)
        if not report.passed:
            with self.db:
                self._audit("rejected", {"mapping": candidate.name, "reasons": report.reasons})
            return report
        canary = self.canary(candidate, checks=checks)
        if not canary.passed or not approved:
            reason = canary.reasons if not canary.passed else ["approval required"]
            with self.db:
                self._audit("rejected", {"mapping": candidate.name, "reasons": reason})
            return ShadowReport(False, reason, report.mapped, report.dead_letter, report.state)
        # The simulated destination is in this same SQLite database. Promotion can
        # therefore atomically replace its state; this is not a claim about external APIs.
        with self.db:
            self.db.execute("UPDATE mappings SET active=0 WHERE active=1")
            self.db.execute(
                "INSERT INTO mappings(name,spec,active) VALUES (?,?,1)",
                (candidate.name, candidate.to_json()),
            )
            self.db.execute("DELETE FROM effects")
            self.db.execute("DELETE FROM destination")
            self.db.execute("UPDATE raw SET outcome='pending',reason=NULL")
            for index, event in enumerate(self.raw_events(), 1):
                try:
                    item = candidate.map(event)
                except MappingError as exc:
                    self.db.execute(
                        "UPDATE raw SET outcome='dead_letter',reason=? WHERE tenant=? AND event_id=?",
                        (str(exc), event["tenant"], event["event_id"]),
                    )
                    continue
                self._apply(item, simulate_rate_limit=False)
                self.db.execute(
                    "UPDATE raw SET outcome='accepted' WHERE tenant=? AND event_id=?",
                    (event["tenant"], event["event_id"]),
                )
                if inject_failure_after == index:
                    raise RuntimeError("injected failure during local promotion")
            self._audit("promoted", {
                "mapping": candidate.name, "mapped": report.mapped,
                "dead_letter": report.dead_letter,
            })
        return report

    def rollback(self) -> None:
        rows = self.db.execute(
            "SELECT version,spec FROM mappings ORDER BY version DESC LIMIT 2"
        ).fetchall()
        if len(rows) < 2:
            raise ValueError("no previous mapping")
        prior = Mapping.from_json(rows[1]["spec"])
        # Rollback is deliberately local and atomically rebuilds the simulated
        # destination. An external destination would need a compensating protocol.
        with self.db:
            self.db.execute("UPDATE mappings SET active=0 WHERE active=1")
            self.db.execute("UPDATE mappings SET active=1 WHERE version=?", (rows[1]["version"],))
            self.db.execute("DELETE FROM effects")
            self.db.execute("DELETE FROM destination")
            for event in self.raw_events():
                try:
                    item = prior.map(event)
                except MappingError as exc:
                    self.db.execute(
                        "UPDATE raw SET outcome='dead_letter',reason=? WHERE tenant=? AND event_id=?",
                        (str(exc), event["tenant"], event["event_id"]),
                    )
                    continue
                self._apply(item, simulate_rate_limit=False)
                self.db.execute(
                    "UPDATE raw SET outcome='accepted',reason=NULL WHERE tenant=? AND event_id=?",
                    (event["tenant"], event["event_id"]),
                )
            self._audit("rolled_back", {"mapping": prior.name})

    def state(self) -> dict[str, dict]:
        return {
            f"{row['tenant']}/{row['entity']}": {
                "seq": row["seq"], "value": row["value"],
            }
            for row in self.db.execute("SELECT * FROM destination")
        }

    def counts(self) -> dict[str, int]:
        result = {
            row["outcome"]: row["count"]
            for row in self.db.execute(
                "SELECT outcome,count(*) AS count FROM raw GROUP BY outcome"
            )
        }
        result["effects"] = self.db.execute("SELECT count(*) FROM effects").fetchone()[0]
        result["deliveries"] = self.db.execute(
            "SELECT COALESCE(sum(deliveries),0) FROM raw"
        ).fetchone()[0]
        result["retries"] = self.retry_count
        return result
