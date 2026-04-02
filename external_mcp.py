"""External MCP bridge — connect to remote MCP servers and proxy their tools.

Stores configs in SQLite, connects via Streamable HTTP, discovers tools,
and exposes them through the gateway's existing plugin registry.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from plugin_base import MCPPlugin, ToolDef

_DB_PATH = os.environ.get("GATEWAY_DB", "/data/gateway.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_mcps (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    auth_header TEXT,
    extra_headers_json TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_refreshed TEXT,
    last_error TEXT
);
"""

_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ── MCP JSON-RPC client ──────────────────────────────────────────────────────

_REQ_COUNTER = 0
_counter_lock = threading.Lock()


def _next_id() -> int:
    global _REQ_COUNTER
    with _counter_lock:
        _REQ_COUNTER += 1
        return _REQ_COUNTER


def _mcp_request(
    url: str,
    method: str,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    session_id: str | None = None,
    timeout: int = 30,
) -> tuple[Any, str | None]:
    """Send a JSON-RPC request to a Streamable HTTP MCP server.

    Returns (result, new_session_id) on success.
    Raises RuntimeError on protocol errors.
    """
    req_id = _next_id()
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params:
        body["params"] = params

    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        hdrs.update(headers)
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id

    resp = requests.post(url, json=body, headers=hdrs, timeout=timeout)

    new_session = resp.headers.get("Mcp-Session-Id") or session_id

    content_type = resp.headers.get("Content-Type", "")

    if "text/event-stream" in content_type:
        result = _parse_sse_response(resp.text, req_id)
        return result, new_session

    if resp.status_code != 200:
        raise RuntimeError(f"MCP server returned {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"MCP error {err.get('code', '?')}: {err.get('message', 'unknown')}")

    return data.get("result"), new_session


def _parse_sse_response(text: str, expected_id: int) -> Any:
    """Parse SSE events and extract the JSON-RPC result for our request ID."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expected_id:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"MCP error {err.get('code', '?')}: {err.get('message', 'unknown')}")
                return msg.get("result")
    raise RuntimeError("No matching JSON-RPC response in SSE stream")


def _send_notification(
    url: str,
    method: str,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    session_id: str | None = None,
) -> None:
    """Send a JSON-RPC notification (no id, no response expected)."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params:
        body["params"] = params

    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id

    requests.post(url, json=body, headers=hdrs, timeout=10)


# ── External MCP connection ──────────────────────────────────────────────────

@dataclass
class ExternalMCPConnection:
    """Represents a live connection to an external MCP server."""
    name: str
    url: str
    auth_headers: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    tools: dict[str, dict] = field(default_factory=dict)
    initialized: bool = False
    last_error: str | None = None

    def _headers(self) -> dict[str, str]:
        return dict(self.auth_headers)

    def initialize(self) -> None:
        """Perform MCP initialize handshake."""
        try:
            result, sid = _mcp_request(
                self.url, "initialize",
                params={
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0"},
                },
                headers=self._headers(),
                session_id=self.session_id,
            )
            self.session_id = sid
            _send_notification(
                self.url, "notifications/initialized",
                headers=self._headers(), session_id=self.session_id,
            )
            self.initialized = True
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def discover_tools(self) -> dict[str, dict]:
        """Call tools/list and return {tool_name: schema}. Retries once on session expiry."""
        if not self.initialized:
            self.initialize()

        for attempt in range(2):
            try:
                result, sid = _mcp_request(
                    self.url, "tools/list",
                    headers=self._headers(),
                    session_id=self.session_id,
                )
                if sid:
                    self.session_id = sid

                raw_tools = result.get("tools", []) if result else []
                self.tools = {}
                for t in raw_tools:
                    self.tools[t["name"]] = {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                self.last_error = None
                return self.tools
            except Exception as exc:
                if attempt == 0 and self._is_session_error(exc):
                    self.initialized = False
                    self.session_id = None
                    self.initialize()
                    continue
                self.last_error = str(exc)
                raise

    def _extract_result(self, result: Any) -> Any:
        if result and isinstance(result, dict):
            content = result.get("content", [])
            if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(texts) if texts else result
        return result

    def _is_session_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return "session not found" in msg or "session expired" in msg or "invalid session" in msg

    def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Proxy a tool call to the external server. Retries once on session expiry."""
        if not self.initialized:
            self.initialize()

        for attempt in range(2):
            try:
                result, sid = _mcp_request(
                    self.url, "tools/call",
                    params={"name": tool_name, "arguments": arguments},
                    headers=self._headers(),
                    session_id=self.session_id,
                    timeout=120,
                )
                if sid:
                    self.session_id = sid
                return self._extract_result(result)
            except Exception as exc:
                if attempt == 0 and self._is_session_error(exc):
                    self.initialized = False
                    self.session_id = None
                    try:
                        self.initialize()
                        continue
                    except Exception as init_exc:
                        self.last_error = str(init_exc)
                        return {"error": f"Re-init failed: {init_exc}"}
                self.last_error = str(exc)
                return {"error": f"External MCP call failed: {exc}"}


# ── Connection cache ─────────────────────────────────────────────────────────

_connections: dict[str, ExternalMCPConnection] = {}


def _get_connection(name: str, url: str, auth_header: str = "", extra_headers: str = "") -> ExternalMCPConnection:
    with _lock:
        if name in _connections:
            conn = _connections[name]
            if conn.url == url:
                return conn

        auth_headers: dict[str, str] = {}
        if auth_header:
            if ":" in auth_header:
                hname, hval = auth_header.split(":", 1)
                auth_headers[hname.strip()] = hval.strip()
            else:
                auth_headers["Authorization"] = f"Bearer {auth_header}"

        if extra_headers:
            try:
                extras = json.loads(extra_headers)
                if isinstance(extras, dict):
                    auth_headers.update(extras)
            except json.JSONDecodeError:
                pass

        conn = ExternalMCPConnection(name=name, url=url, auth_headers=auth_headers)
        _connections[name] = conn
        return conn


def _invalidate_connection(name: str) -> None:
    with _lock:
        _connections.pop(name, None)


# ── External MCP as Plugin ───────────────────────────────────────────────────

class ExternalPlugin(MCPPlugin):
    """Wraps an external MCP server as a gateway plugin."""

    def __init__(self, name: str, tool_schemas: dict[str, dict], connection: ExternalMCPConnection):
        self.name = name
        self._connection = connection
        self.tools: dict[str, ToolDef] = {}

        for tname, schema in tool_schemas.items():
            proxy = self._make_proxy(tname, schema)
            self.tools[tname] = ToolDef(
                access="read",
                handler=proxy,
                description=schema.get("description", ""),
            )

    def _make_proxy(self, tool_name: str, schema: dict):
        conn = self._connection

        def proxy_handler(**kwargs):
            return conn.call_tool(tool_name, kwargs)

        input_schema = schema.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        required = set(input_schema.get("required", []))

        import inspect
        params = []
        for pname, pschema in properties.items():
            default = inspect.Parameter.empty if pname in required else None
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default))

        if params:
            proxy_handler.__signature__ = inspect.Signature(params)

        proxy_handler.__doc__ = schema.get("description", "")
        return proxy_handler

    def health_check(self) -> dict[str, Any]:
        if self._connection.last_error:
            return {"status": "error", "error": self._connection.last_error}
        return {"status": "ok", "initialized": self._connection.initialized}


# ── CRUD operations ──────────────────────────────────────────────────────────

def add_external_mcp(name: str, url: str, auth_header: str = "", extra_headers: str = "") -> dict:
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO external_mcps (name, url, auth_header, extra_headers_json) VALUES (?,?,?,?)",
            (name, url, auth_header, extra_headers),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"error": f"External MCP '{name}' already exists"}
    conn.close()
    return {"success": True, "name": name, "url": url}


def remove_external_mcp(name: str) -> dict:
    conn = _get_db()
    conn.execute("DELETE FROM external_mcps WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    _invalidate_connection(name)
    return {"success": True, "name": name}


def update_external_mcp(name: str, url: str = "", auth_header: str = "", extra_headers: str = "", enabled: bool = True) -> dict:
    conn = _get_db()
    sets = ["enabled = ?"]
    vals: list[Any] = [1 if enabled else 0]
    if url:
        sets.append("url = ?")
        vals.append(url)
    if auth_header is not None:
        sets.append("auth_header = ?")
        vals.append(auth_header)
    if extra_headers is not None:
        sets.append("extra_headers_json = ?")
        vals.append(extra_headers)
    vals.append(name)
    conn.execute(f"UPDATE external_mcps SET {', '.join(sets)} WHERE name = ?", vals)
    conn.commit()
    conn.close()
    _invalidate_connection(name)
    return {"success": True, "name": name}


def list_external_mcps() -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM external_mcps ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_external_mcp(name: str) -> dict | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM external_mcps WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Load and register external MCPs ──────────────────────────────────────────

def load_external_plugins() -> list[ExternalPlugin]:
    """Load all enabled external MCPs, discover their tools, return as plugins."""
    configs = list_external_mcps()
    plugins: list[ExternalPlugin] = []

    for cfg in configs:
        if not cfg.get("enabled"):
            continue

        name = cfg["name"]
        url = cfg["url"]
        auth = cfg.get("auth_header") or ""
        extras = cfg.get("extra_headers_json") or ""

        try:
            connection = _get_connection(name, url, auth, extras)
            tools = connection.discover_tools()

            conn = _get_db()
            conn.execute(
                "UPDATE external_mcps SET last_refreshed = datetime('now'), last_error = NULL WHERE name = ?",
                (name,),
            )
            conn.commit()
            conn.close()

            plugin = ExternalPlugin(name, tools, connection)
            plugins.append(plugin)
            print(f"[external] loaded {name} from {url} ({len(tools)} tools)", flush=True)

        except Exception as exc:
            print(f"[external] failed to load {name}: {exc}", flush=True)
            conn = _get_db()
            conn.execute(
                "UPDATE external_mcps SET last_error = ? WHERE name = ?",
                (str(exc)[:500], name),
            )
            conn.commit()
            conn.close()

    return plugins


def refresh_external_plugin(name: str) -> tuple[ExternalPlugin | None, str | None]:
    """Re-connect and re-discover tools for a specific external MCP.

    Returns (plugin, error_message).
    """
    cfg = get_external_mcp(name)
    if not cfg:
        return None, f"External MCP '{name}' not found"

    _invalidate_connection(name)

    url = cfg["url"]
    auth = cfg.get("auth_header") or ""
    extras = cfg.get("extra_headers_json") or ""

    try:
        connection = _get_connection(name, url, auth, extras)
        tools = connection.discover_tools()

        conn = _get_db()
        conn.execute(
            "UPDATE external_mcps SET last_refreshed = datetime('now'), last_error = NULL WHERE name = ?",
            (name,),
        )
        conn.commit()
        conn.close()

        plugin = ExternalPlugin(name, tools, connection)
        return plugin, None
    except Exception as exc:
        conn = _get_db()
        conn.execute(
            "UPDATE external_mcps SET last_error = ? WHERE name = ?",
            (str(exc)[:500], name),
        )
        conn.commit()
        conn.close()
        return None, str(exc)
