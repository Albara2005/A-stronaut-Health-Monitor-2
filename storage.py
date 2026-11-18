"""SQLite persistence for decision history (thread-safe, one connection)."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    matched_rule_ids TEXT NOT NULL,
    telemetry_json TEXT NOT NULL,
    decision_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_device ON decisions(device_id, id DESC);
"""


class Storage:
    def __init__(self, path: str = "orion.db"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)

    def save_decision(self, device_id: str, telemetry: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """Store a decision. Returns False (and stores nothing) when it has the same fingerprint as
        the device's previous decision - history records changes, not every repeated sample."""
        with self._lock:
            last = self._db.execute(
                "SELECT decision_id FROM decisions WHERE device_id=? ORDER BY id DESC LIMIT 1",
                (device_id,)).fetchone()
            if last and last["decision_id"] == decision["decision_id"]:
                return False
            self._db.execute(
                "INSERT INTO decisions(device_id, created_at, decision_id, overall_status,"
                " matched_rule_ids, telemetry_json, decision_json) VALUES (?,?,?,?,?,?,?)",
                (device_id, decision["created_at"], decision["decision_id"], decision["overall_status"],
                 ",".join(r["rule_id"] for r in decision["matched_rules"]),
                 json.dumps(telemetry), json.dumps(decision)))
            self._db.commit()
            return True

    def history(self, device_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        q = ("SELECT id, device_id, created_at, decision_id, overall_status, matched_rule_ids "
             "FROM decisions")
        args: tuple = ()
        if device_id:
            q += " WHERE device_id=?"
            args = (device_id,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(q, args + (limit,)).fetchall()
        return [dict(r, matched_rule_ids=[x for x in r["matched_rule_ids"].split(",") if x]) for r in rows]

    def latest(self, device_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        q = "SELECT decision_json FROM decisions"
        args: tuple = ()
        if device_id:
            q += " WHERE device_id=?"
            args = (device_id,)
        q += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(q, args).fetchone()
        return json.loads(row["decision_json"]) if row else None

    def devices(self) -> List[str]:
        """Device ids that have stored decisions, most recently active first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT device_id FROM decisions GROUP BY device_id ORDER BY MAX(id) DESC").fetchall()
        return [r["device_id"] for r in rows]

    def latest_telemetry(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT telemetry_json FROM decisions WHERE device_id=? ORDER BY id DESC LIMIT 1",
                (device_id,)).fetchone()
        return json.loads(row["telemetry_json"]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
