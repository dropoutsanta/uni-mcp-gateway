"""Audit logging — records every tool call with key, plugin, tool, args, and result."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional


_DB_PATH = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    key_id TEXT,
    tool_name TEXT,
    plugin TEXT,
    args_json TEXT,
    success INTEGER,
    error TEXT,
    duration_ms INTEGER,
    result_json TEXT
);
"""

_MIGRATION = "ALTER TABLE audit_log ADD COLUMN result_json TEXT"


def init_audit_db() -> None:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_AUDIT_SCHEMA)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    if "result_json" not in cols:
        try:
            conn.execute(_MIGRATION)
            conn.commit()
            print("[audit] migrated: added result_json column", flush=True)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def log_tool_call(
    key_id: str,
    tool_name: str,
    plugin: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    success: bool = True,
    error: str | None = None,
    duration_ms: int = 0,
) -> None:
    try:
        args_str = json.dumps(args, default=str)[:4000] if args else None
        result_str = None
        if result is not None:
            try:
                result_str = json.dumps(result, default=str)[:8000]
            except (TypeError, ValueError):
                result_str = str(result)[:8000]
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO audit_log (key_id, tool_name, plugin, args_json, success, error, duration_ms, result_json) VALUES (?,?,?,?,?,?,?,?)",
            (key_id, tool_name, plugin, args_str, int(success), error, duration_ms, result_str),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[audit] write failed: {exc}", flush=True)


def query_audit_log(
    key_id: Optional[str] = None,
    plugin: Optional[str] = None,
    tool_name: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row

    clauses = []
    params: list[Any] = []
    if key_id:
        clauses.append("key_id = ?")
        params.append(key_id)
    if plugin:
        clauses.append("plugin = ?")
        params.append(plugin)
    if tool_name:
        clauses.append("tool_name = ?")
        params.append(tool_name)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_audit_entry(entry_id: int) -> dict | None:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
