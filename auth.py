"""Gateway auth — SQLite-backed key management, permission checking, rate limiting."""

from __future__ import annotations

import ipaddress
import os
import secrets
import sqlite3
import time
from typing import Any, Optional


ADMIN_KEY_ID = os.environ.get("ADMIN_KEY_ID", "admin")

_DB_PATH = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id TEXT PRIMARY KEY,
    api_key TEXT UNIQUE NOT NULL,
    label TEXT,
    is_admin INTEGER DEFAULT 0,
    rate_limit INTEGER DEFAULT 100,
    expires_at TEXT,
    allowed_ips TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS key_permissions (
    key_id TEXT REFERENCES keys(id) ON DELETE CASCADE,
    plugin TEXT NOT NULL,
    access_level TEXT NOT NULL,
    PRIMARY KEY (key_id, plugin, access_level)
);

CREATE TABLE IF NOT EXISTS key_tool_overrides (
    key_id TEXT REFERENCES keys(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    access_level TEXT NOT NULL,
    PRIMARY KEY (key_id, tool_name, access_level)
);

CREATE TABLE IF NOT EXISTS key_credentials (
    key_id TEXT REFERENCES keys(id) ON DELETE CASCADE,
    plugin TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    credential_value TEXT NOT NULL,
    PRIMARY KEY (key_id, plugin, credential_key)
);

CREATE TABLE IF NOT EXISTS key_plugin_scopes (
    key_id TEXT REFERENCES keys(id) ON DELETE CASCADE,
    plugin TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    PRIMARY KEY (key_id, plugin, scope_type, scope_value)
);

CREATE TABLE IF NOT EXISTS key_rate_limits (
    key_id TEXT REFERENCES keys(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    rate_limit INTEGER NOT NULL,
    PRIMARY KEY (key_id, scope)
);
"""


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_db()
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    _ensure_admin_key()


def _ensure_admin_key() -> None:
    """Seed the admin key from env if it doesn't exist yet."""
    master_token = os.environ.get("MCP_AUTH_TOKEN", "")
    if not master_token:
        return
    conn = _get_db()
    existing = conn.execute("SELECT id FROM keys WHERE id = ?", (ADMIN_KEY_ID,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO keys (id, api_key, label, is_admin) VALUES (?, ?, ?, 1)",
            (ADMIN_KEY_ID, master_token, "Admin (master)"),
        )
        conn.commit()
        print(f"[auth] seeded admin key '{ADMIN_KEY_ID}'", flush=True)
    else:
        conn.execute("UPDATE keys SET api_key = ? WHERE id = ?", (master_token, ADMIN_KEY_ID))
        conn.commit()
    conn.close()


# ── Key lookup ────────────────────────────────────────────────────────────────

def resolve_key(bearer_token: str) -> Optional[dict[str, Any]]:
    """Look up a key by its bearer token. Returns key info dict or None."""
    conn = _get_db()
    row = conn.execute("SELECT * FROM keys WHERE api_key = ?", (bearer_token,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def is_key_expired(key_info: dict) -> bool:
    exp = key_info.get("expires_at")
    if not exp:
        return False
    try:
        from datetime import datetime, timezone
        expiry = datetime.fromisoformat(exp)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expiry
    except Exception:
        return False


def check_ip_allowed(key_info: dict, client_ip: str) -> bool:
    allowed = key_info.get("allowed_ips")
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
        for cidr in allowed.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if addr in net:
                    return True
            except ValueError:
                if cidr == client_ip:
                    return True
        return False
    except ValueError:
        return True


# ── Rate limiting (in-memory sliding window) ──────────────────────────────────

_rate_windows: dict[str, list[float]] = {}


def _check_window(window_key: str, limit: int) -> bool:
    if limit <= 0:
        return True
    now = time.time()
    window = _rate_windows.setdefault(window_key, [])
    cutoff = now - 60.0
    _rate_windows[window_key] = [t for t in window if t > cutoff]
    if len(_rate_windows[window_key]) >= limit:
        return False
    _rate_windows[window_key].append(now)
    return True


def check_rate_limit(key_id: str, limit: int) -> bool:
    """Check the global per-key rate limit. Return True if allowed."""
    return _check_window(key_id, limit)


def check_granular_rate_limit(key_id: str, plugin: str, tool_name: str, account: str = "") -> str | None:
    """Check plugin/account/tool rate limits. Returns error message or None if allowed.

    Scope hierarchy checked (most specific wins):
      tool:<tool_name>  >  account:<plugin>:<account>  >  plugin:<plugin>
    """
    limits = get_rate_limits(key_id)
    if not limits:
        return None

    checks = [
        (f"plugin:{plugin}", f"{key_id}:plugin:{plugin}"),
    ]
    if account:
        checks.append((f"account:{plugin}:{account}", f"{key_id}:account:{plugin}:{account}"))
    checks.append((f"tool:{tool_name}", f"{key_id}:tool:{tool_name}"))

    for scope, window_key in checks:
        limit = limits.get(scope)
        if limit is not None and not _check_window(window_key, limit):
            return f"Rate limit exceeded for {scope} ({limit}/min)"

    return None


# ── Granular rate limit CRUD ──────────────────────────────────────────────────

def get_rate_limits(key_id: str) -> dict[str, int]:
    """Return {scope: rate_limit} for a key."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT scope, rate_limit FROM key_rate_limits WHERE key_id = ?", (key_id,)
    ).fetchall()
    conn.close()
    return {r["scope"]: r["rate_limit"] for r in rows}


def set_rate_limit(key_id: str, scope: str, rate_limit: int) -> dict:
    """Set a granular rate limit. scope format: 'plugin:<name>', 'account:<plugin>:<account>', or 'tool:<tool_name>'."""
    conn = _get_db()
    conn.execute(
        "INSERT INTO key_rate_limits (key_id, scope, rate_limit) VALUES (?,?,?) "
        "ON CONFLICT(key_id, scope) DO UPDATE SET rate_limit = excluded.rate_limit",
        (key_id, scope, rate_limit),
    )
    conn.commit()
    conn.close()
    return {"success": True, "key_id": key_id, "scope": scope, "rate_limit": rate_limit}


def delete_rate_limit(key_id: str, scope: str) -> dict:
    conn = _get_db()
    conn.execute("DELETE FROM key_rate_limits WHERE key_id = ? AND scope = ?", (key_id, scope))
    conn.commit()
    conn.close()
    return {"success": True, "key_id": key_id, "scope": scope}


# ── Permissions ───────────────────────────────────────────────────────────────

def get_key_permissions(key_id: str) -> dict[str, set[str]]:
    """Return {plugin_name: {access_levels}} for a key."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT plugin, access_level FROM key_permissions WHERE key_id = ?", (key_id,)
    ).fetchall()
    conn.close()
    perms: dict[str, set[str]] = {}
    for r in rows:
        perms.setdefault(r["plugin"], set()).add(r["access_level"])
    return perms


def get_tool_overrides(key_id: str) -> dict[str, set[str]]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT tool_name, access_level FROM key_tool_overrides WHERE key_id = ?", (key_id,)
    ).fetchall()
    conn.close()
    overrides: dict[str, set[str]] = {}
    for r in rows:
        overrides.setdefault(r["tool_name"], set()).add(r["access_level"])
    return overrides


def get_key_credentials(key_id: str) -> dict[str, dict[str, str]]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT plugin, credential_key, credential_value FROM key_credentials WHERE key_id = ?",
        (key_id,),
    ).fetchall()
    conn.close()
    creds: dict[str, dict[str, str]] = {}
    for r in rows:
        creds.setdefault(r["plugin"], {})[r["credential_key"]] = r["credential_value"]
    return creds


def get_key_data_scopes(key_id: str) -> dict[str, set[str]]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT plugin, scope_value FROM key_plugin_scopes WHERE key_id = ?",
        (key_id,),
    ).fetchall()
    conn.close()
    scopes: dict[str, set[str]] = {}
    for r in rows:
        scopes.setdefault(r["plugin"], set()).add(r["scope_value"])
    return scopes


# ── Key CRUD (admin operations) ───────────────────────────────────────────────

def create_key(
    key_id: str,
    label: str = "",
    is_admin: bool = False,
    rate_limit: int = 100,
    expires_at: str | None = None,
    allowed_ips: str | None = None,
) -> dict:
    api_key = secrets.token_urlsafe(32)
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO keys (id, api_key, label, is_admin, rate_limit, expires_at, allowed_ips) VALUES (?,?,?,?,?,?,?)",
            (key_id, api_key, label, int(is_admin), rate_limit, expires_at, allowed_ips),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        return {"error": f"Key '{key_id}' already exists: {e}"}
    conn.close()
    return {"success": True, "key_id": key_id, "api_key": api_key, "label": label}


def delete_key(key_id: str) -> dict:
    if key_id == ADMIN_KEY_ID:
        return {"error": "Cannot delete the admin key"}
    conn = _get_db()
    conn.execute("DELETE FROM keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    _rate_windows.pop(key_id, None)
    return {"success": True, "deleted": key_id}


def list_keys() -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT id, label, is_admin, rate_limit, expires_at, allowed_ips, created_at FROM keys").fetchall()
    conn.close()
    result = []
    for r in rows:
        key_dict = dict(r)
        key_dict["permissions"] = get_key_permissions(r["id"])
        result.append(key_dict)
    return result


def set_permissions(key_id: str, plugin: str, access_levels: list[str]) -> dict:
    conn = _get_db()
    conn.execute("DELETE FROM key_permissions WHERE key_id = ? AND plugin = ?", (key_id, plugin))
    for level in access_levels:
        conn.execute(
            "INSERT INTO key_permissions (key_id, plugin, access_level) VALUES (?,?,?)",
            (key_id, plugin, level),
        )
    conn.commit()
    conn.close()
    return {"success": True, "key_id": key_id, "plugin": plugin, "access_levels": access_levels}


def set_tool_override(key_id: str, tool_name: str, access_levels: list[str]) -> dict:
    conn = _get_db()
    conn.execute("DELETE FROM key_tool_overrides WHERE key_id = ? AND tool_name = ?", (key_id, tool_name))
    for level in access_levels:
        conn.execute(
            "INSERT INTO key_tool_overrides (key_id, tool_name, access_level) VALUES (?,?,?)",
            (key_id, tool_name, level),
        )
    conn.commit()
    conn.close()
    return {"success": True, "key_id": key_id, "tool_name": tool_name, "access_levels": access_levels}


def set_credentials(key_id: str, plugin: str, credentials: dict[str, str]) -> dict:
    """Replace ALL credentials for a key+plugin. Use upsert_credentials to add without wiping."""
    conn = _get_db()
    conn.execute("DELETE FROM key_credentials WHERE key_id = ? AND plugin = ?", (key_id, plugin))
    for k, v in credentials.items():
        conn.execute(
            "INSERT INTO key_credentials (key_id, plugin, credential_key, credential_value) VALUES (?,?,?,?)",
            (key_id, plugin, k, v),
        )
    conn.commit()
    conn.close()
    return {"success": True, "key_id": key_id, "plugin": plugin}


def upsert_credentials(key_id: str, plugin: str, credentials: dict[str, str]) -> dict:
    """Add or update specific credential keys without deleting others."""
    conn = _get_db()
    for k, v in credentials.items():
        conn.execute(
            "INSERT INTO key_credentials (key_id, plugin, credential_key, credential_value) "
            "VALUES (?,?,?,?) ON CONFLICT(key_id, plugin, credential_key) DO UPDATE SET credential_value = excluded.credential_value",
            (key_id, plugin, k, v),
        )
    conn.commit()
    existing = conn.execute(
        "SELECT credential_key FROM key_credentials WHERE key_id = ? AND plugin = ?",
        (key_id, plugin),
    ).fetchall()
    conn.close()
    return {"success": True, "key_id": key_id, "plugin": plugin, "credential_keys": [r["credential_key"] for r in existing]}


def manage_scopes(key_id: str, plugin: str, scope_type: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    conn = _get_db()
    if add:
        for val in add:
            conn.execute(
                "INSERT OR IGNORE INTO key_plugin_scopes (key_id, plugin, scope_type, scope_value) VALUES (?,?,?,?)",
                (key_id, plugin, scope_type, val),
            )
    if remove:
        for val in remove:
            conn.execute(
                "DELETE FROM key_plugin_scopes WHERE key_id=? AND plugin=? AND scope_type=? AND scope_value=?",
                (key_id, plugin, scope_type, val),
            )
    conn.commit()
    remaining = conn.execute(
        "SELECT scope_value FROM key_plugin_scopes WHERE key_id=? AND plugin=? AND scope_type=?",
        (key_id, plugin, scope_type),
    ).fetchall()
    conn.close()
    return {"success": True, "key_id": key_id, "plugin": plugin, "scope_type": scope_type, "values": [r["scope_value"] for r in remaining]}


def db_health() -> bool:
    try:
        conn = _get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return True
    except Exception:
        return False
