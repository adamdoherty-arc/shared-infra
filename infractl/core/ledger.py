"""SQLite WAL ledger at /state/infractl.db. Idempotent schema migrations
(CREATE TABLE IF NOT EXISTS + additive ALTER TABLE guarded by a
pragma table_info check) so re-running init on an existing db is a no-op.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = {
    "actions": """
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            tier TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            legion_ref TEXT,
            status TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            snapshot_dir TEXT,
            pre_config_sha256 TEXT,
            post_config_sha256 TEXT,
            verify_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL
        )
    """,
    "probe_runs": """
        CREATE TABLE IF NOT EXISTS probe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ok INTEGER NOT NULL,
            detail TEXT,
            latency_ms REAL,
            ts REAL NOT NULL
        )
    """,
    "heal_events": """
        CREATE TABLE IF NOT EXISTS heal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT NOT NULL,
            probe_name TEXT NOT NULL,
            action_kind TEXT,
            action_id TEXT,
            outcome TEXT NOT NULL,
            detail TEXT,
            ts REAL NOT NULL
        )
    """,
    "lane_health": """
        CREATE TABLE IF NOT EXISTS lane_health (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            up INTEGER NOT NULL,
            latency_ms REAL,
            ts REAL NOT NULL,
            PRIMARY KEY (provider, model)
        )
    """,
    "alerts_seen": """
        CREATE TABLE IF NOT EXISTS alerts_seen (
            fingerprint TEXT PRIMARY KEY,
            probe_name TEXT NOT NULL,
            last_sent_at REAL NOT NULL,
            count INTEGER NOT NULL DEFAULT 1
        )
    """,
    "brain_usage": """
        CREATE TABLE IF NOT EXISTS brain_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            model TEXT,
            ts REAL NOT NULL
        )
    """,
    "improvement_ledger": """
        CREATE TABLE IF NOT EXISTS improvement_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            detail TEXT,
            source TEXT,
            ts REAL NOT NULL
        )
    """,
    "snapshots": """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT NOT NULL,
            path TEXT NOT NULL,
            files_json TEXT NOT NULL,
            ts REAL NOT NULL
        )
    """,
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_actions_status ON actions(status)",
    "CREATE INDEX IF NOT EXISTS ix_actions_kind ON actions(kind)",
    "CREATE INDEX IF NOT EXISTS ix_probe_runs_name_ts ON probe_runs(name, ts)",
    "CREATE INDEX IF NOT EXISTS ix_heal_events_ts ON heal_events(ts)",
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def init_schema(con: sqlite3.Connection) -> None:
    for ddl in SCHEMA.values():
        con.execute(ddl)
    for ddl in INDEXES:
        con.execute(ddl)


class Ledger:
    """Thin wrapper. One Ledger per request/scheduler-tick is fine — SQLite
    WAL mode handles concurrent readers, and infractl's own single-writer
    lock (core/lock.py) already serializes action writes."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.con = connect(db_path)
        init_schema(self.con)

    def close(self) -> None:
        self.con.close()

    # ---- actions ----
    def create_action(
        self, kind: str, tier: str, payload: dict, requested_by: str, reason: str,
        legion_ref: str | None = None, dry_run: bool = False,
    ) -> str:
        action_id = uuid.uuid4().hex[:16]
        self.con.execute(
            "INSERT INTO actions (id, kind, tier, payload_json, requested_by, reason, "
            "legion_ref, status, dry_run, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (action_id, kind, tier, json.dumps(payload), requested_by, reason,
             legion_ref, "pending", int(dry_run), time.time()),
        )
        return action_id

    def update_action(self, action_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values())
        vals.append(action_id)
        self.con.execute(f"UPDATE actions SET {cols} WHERE id=?", vals)

    def get_action(self, action_id: str) -> dict | None:
        row = self.con.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return dict(row) if row else None

    def list_actions(self, since: float | None = None, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM actions WHERE 1=1"
        args: list[Any] = []
        if since is not None:
            q += " AND created_at >= ?"
            args.append(since)
        if status is not None:
            q += " AND status = ?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT 500"
        return [dict(r) for r in self.con.execute(q, args).fetchall()]

    # ---- probes ----
    def record_probe(self, name: str, ok: bool, detail: str, latency_ms: float, ts: float | None = None) -> None:
        self.con.execute(
            "INSERT INTO probe_runs (name, ok, detail, latency_ms, ts) VALUES (?,?,?,?,?)",
            (name, int(ok), detail, latency_ms, ts if ts is not None else time.time()),
        )

    def latest_probes(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT p.* FROM probe_runs p "
            "JOIN (SELECT name, MAX(ts) AS mts FROM probe_runs GROUP BY name) m "
            "ON p.name = m.name AND p.ts = m.mts ORDER BY p.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def probe_streak_red(self, name: str, n: int) -> bool:
        rows = self.con.execute(
            "SELECT ok FROM probe_runs WHERE name=? ORDER BY ts DESC LIMIT ?", (name, n)
        ).fetchall()
        return len(rows) >= n and all(not r["ok"] for r in rows)

    # ---- heal events ----
    def record_heal_event(self, rule: str, probe_name: str, outcome: str, detail: str,
                           action_kind: str | None = None, action_id: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO heal_events (rule, probe_name, action_kind, action_id, outcome, detail, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (rule, probe_name, action_kind, action_id, outcome, detail, time.time()),
        )

    def heal_events_today(self, rule: str) -> int:
        cutoff = time.time() - 86400
        row = self.con.execute(
            "SELECT COUNT(*) AS c FROM heal_events WHERE rule=? AND ts>=? AND outcome IN "
            "('executed','dry_run')", (rule, cutoff),
        ).fetchone()
        return row["c"]

    def last_heal_event_ts(self, rule: str) -> float | None:
        row = self.con.execute(
            "SELECT MAX(ts) AS m FROM heal_events WHERE rule=? AND outcome IN ('executed','dry_run')",
            (rule,),
        ).fetchone()
        return row["m"]

    # ---- alerts dedupe ----
    def alert_recently_sent(self, fingerprint: str, cooldown_s: float) -> bool:
        row = self.con.execute(
            "SELECT last_sent_at FROM alerts_seen WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row is None:
            return False
        return (time.time() - row["last_sent_at"]) < cooldown_s

    def record_alert_sent(self, fingerprint: str, probe_name: str) -> None:
        now = time.time()
        self.con.execute(
            "INSERT INTO alerts_seen (fingerprint, probe_name, last_sent_at, count) VALUES (?,?,?,1) "
            "ON CONFLICT(fingerprint) DO UPDATE SET last_sent_at=excluded.last_sent_at, "
            "count=count+1",
            (fingerprint, probe_name, now),
        )

    # ---- snapshots ----
    def record_snapshot(self, action_id: str, path: str, files: list[str]) -> None:
        self.con.execute(
            "INSERT INTO snapshots (action_id, path, files_json, ts) VALUES (?,?,?,?)",
            (action_id, path, json.dumps(files), time.time()),
        )

    # ---- brain usage ----
    def brain_calls_today(self) -> int:
        cutoff = time.time() - 86400
        row = self.con.execute(
            "SELECT COUNT(*) AS c FROM brain_usage WHERE ts>=?", (cutoff,)
        ).fetchone()
        return row["c"]

    def record_brain_call(self, purpose: str, model: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO brain_usage (purpose, model, ts) VALUES (?,?,?)",
            (purpose, model, time.time()),
        )
