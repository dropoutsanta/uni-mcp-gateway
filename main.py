"""Unified MCP Gateway — multi-plugin MCP server with auth, rate limiting, audit, and external MCP bridging."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html as html_module
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

import auth
import audit
import dashboard
import external_mcp
import media
from plugin_base import (
    MCPPlugin,
    RequestContext,
    ToolDef,
    _current_context,
    ACCESS_HIERARCHY,
)
from plugins import discover_plugins


# ── Configuration ─────────────────────────────────────────────────────────────

_MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "http://localhost:8080")
_STATE_PATH = Path(os.environ.get("OAUTH_STATE_PATH", "/data/oauth_state.json"))
_MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

_RETRYABLE_CODES = {502, 503, 504, 429}
_MAX_RETRIES = 2
_BACKOFF = [1, 3]


# ── OAuth 2.1 in-memory stores (persisted to disk) ───────────────────────────

_registered_clients: dict[str, dict] = {}
_auth_codes: dict[str, dict] = {}
_issued_tokens: dict[str, str] = {}  # access_token -> key_id


def _save_state() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        data = {
            "registered_clients": _registered_clients,
            "issued_tokens": _issued_tokens,
        }
        tmp.write_text(json.dumps(data))
        tmp.rename(_STATE_PATH)
    except Exception as exc:
        print(f"[persist] save failed: {exc}", flush=True)


def _load_state() -> None:
    global _registered_clients, _issued_tokens
    try:
        if _STATE_PATH.exists():
            data = json.loads(_STATE_PATH.read_text())
            _registered_clients.update(data.get("registered_clients", {}))
            _issued_tokens.update(data.get("issued_tokens", {}))
            print(f"[persist] loaded {len(_registered_clients)} clients, {len(_issued_tokens)} tokens", flush=True)
    except Exception as exc:
        print(f"[persist] load failed: {exc}", flush=True)


def _get_base_url() -> str:
    return _MCP_BASE_URL.rstrip("/")


# ── Plugin registry ──────────────────────────────────────────────────────────

_plugins: list[MCPPlugin] = []
_plugin_map: dict[str, MCPPlugin] = {}
_tool_registry: dict[str, tuple[MCPPlugin, ToolDef]] = {}


# ── Resilient HTTP client ────────────────────────────────────────────────────

async def request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(_MAX_RETRIES + 1):
        resp = await client.request(method, url, **kwargs)
        if resp.status_code not in _RETRYABLE_CODES or attempt == _MAX_RETRIES:
            return resp
        await asyncio.sleep(_BACKOFF[attempt])
    return resp


def sync_request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(_MAX_RETRIES + 1):
        resp = httpx.request(method, url, **kwargs)
        if resp.status_code not in _RETRYABLE_CODES or attempt == _MAX_RETRIES:
            return resp
        time.sleep(_BACKOFF[attempt])
    return resp


# ── FastMCP instance ─────────────────────────────────────────────────────────

_GATEWAY_INSTRUCTIONS = """\
Unified MCP Gateway — aggregate multiple MCP servers behind a single endpoint with auth, rate limiting, and audit logging.

## How to use tools

This gateway uses **progressive discovery** to stay lightweight. You have 4 core tools:

1. `list_plugins()` — see available plugins, their accounts, and tool counts
2. `search_tools(query)` — find tools by keyword (e.g. "send message", "list items")
3. `get_tool_schema(tool_name)` — get exact parameters for a specific tool before calling it
4. `call_tool(tool_name, params_json)` — execute any tool

### Typical flow

```
list_plugins()          → see what's available
search_tools("users")   → find the right tool name
get_tool_schema("myapp_users_list") → see parameters
call_tool("myapp_users_list", '{"page": 1}', account="prod") → execute
```

### Multi-account plugins

Some plugins have multiple accounts. Pass `account` to `call_tool` when needed.
If you omit `account` and multiple exist, the call returns an error listing available accounts.
Use `list_plugins()` to see which accounts are configured.

## Admin tools (gateway_* prefix)

Only available to admin keys:
- `gateway_add_account` — add a named account for any plugin
- `gateway_set_credentials` — set/replace credentials for a key+plugin
- `gateway_create_key`, `gateway_delete_key`, `gateway_list_keys` — key management
- `gateway_set_permissions`, `gateway_set_tool_override` — access control
- `gateway_manage_scopes` — data-level scoping (e.g. allowed IDs per plugin)
- `gateway_audit_log` — query the audit trail
- `gateway_plugin_health` — check upstream service health
"""

mcp = FastMCP(
    "mcp-gateway",
    instructions=_GATEWAY_INSTRUCTIONS,
    host="0.0.0.0",
    port=_MCP_PORT,
    stateless_http=True,
)


import inspect as _inspect

_tool_handlers: dict[str, Any] = {}
_tool_schemas: dict[str, dict] = {}


def _register_plugins():
    """Discover plugins, build internal registry. Tools are NOT registered on MCP directly."""
    global _plugins, _plugin_map, _tool_registry

    _plugins = discover_plugins()
    _plugin_map = {p.name: p for p in _plugins}

    for plugin in _plugins:
        for tool_name, tool_def in plugin.tools.items():
            prefixed = f"{plugin.name}_{tool_name}"
            _tool_registry[prefixed] = (plugin, tool_def)

            handler = tool_def.handler

            def _make_wrapper(p=plugin, tn=tool_name, td=tool_def, h=handler):
                def wrapper(**kwargs):
                    ctx = _current_context.get()
                    if ctx:
                        account = kwargs.get("account", "")
                        rl_err = auth.check_granular_rate_limit(ctx.key_id, p.name, f"{p.name}_{tn}", account)
                        if rl_err:
                            return {"error": rl_err}
                    t0 = time.time()
                    try:
                        result = h(**kwargs)
                        duration = int((time.time() - t0) * 1000)
                        if ctx:
                            audit.log_tool_call(ctx.key_id, f"{p.name}_{tn}", p.name, args=kwargs, result=result, success=True, duration_ms=duration)
                        return result
                    except Exception as exc:
                        duration = int((time.time() - t0) * 1000)
                        error_msg = f"{p.name}.{tn} failed: {str(exc)}"
                        if ctx:
                            audit.log_tool_call(ctx.key_id, f"{p.name}_{tn}", p.name, args=kwargs, result={"error": error_msg}, success=False, error=error_msg, duration_ms=duration)
                        return {"error": error_msg}
                return wrapper

            _tool_handlers[prefixed] = _make_wrapper()

            sig = _inspect.signature(handler)
            params = {}
            for pname, param in sig.parameters.items():
                ptype = param.annotation if param.annotation != _inspect.Parameter.empty else "str"
                if hasattr(ptype, "__name__"):
                    ptype = ptype.__name__
                else:
                    ptype = str(ptype)
                required = param.default is _inspect.Parameter.empty
                params[pname] = {"type": ptype, "required": required}
            _tool_schemas[prefixed] = {
                "name": prefixed,
                "plugin": plugin.name,
                "access": tool_def.access,
                "description": tool_def.description or "",
                "parameters": params,
            }

    # Load external MCP bridges
    ext_plugins = external_mcp.load_external_plugins()
    for ep in ext_plugins:
        _register_single_plugin(ep)
        _plugins.append(ep)
        _plugin_map[ep.name] = ep

    print(f"[gateway] indexed {len(_tool_registry)} tools from {len(_plugins)} plugins (meta-tool mode)", flush=True)


def _register_single_plugin(plugin: MCPPlugin):
    """Register a single plugin's tools into the gateway registry."""
    import inspect as _insp

    for tool_name, tool_def in plugin.tools.items():
        prefixed = f"{plugin.name}_{tool_name}"
        _tool_registry[prefixed] = (plugin, tool_def)

        handler = tool_def.handler

        def _make_wrapper(p=plugin, tn=tool_name, td=tool_def, h=handler):
            def wrapper(**kwargs):
                ctx = _current_context.get()
                if ctx:
                    account = kwargs.get("account", "")
                    rl_err = auth.check_granular_rate_limit(ctx.key_id, p.name, f"{p.name}_{tn}", account)
                    if rl_err:
                        return {"error": rl_err}
                t0 = time.time()
                try:
                    result = h(**kwargs)
                    duration = int((time.time() - t0) * 1000)
                    if ctx:
                        audit.log_tool_call(ctx.key_id, f"{p.name}_{tn}", p.name, args=kwargs, result=result, success=True, duration_ms=duration)
                    return result
                except Exception as exc:
                    duration = int((time.time() - t0) * 1000)
                    error_msg = f"{p.name}.{tn} failed: {str(exc)}"
                    if ctx:
                        audit.log_tool_call(ctx.key_id, f"{p.name}_{tn}", p.name, args=kwargs, result={"error": error_msg}, success=False, error=error_msg, duration_ms=duration)
                    return {"error": error_msg}
            return wrapper

        _tool_handlers[prefixed] = _make_wrapper()

        sig = _insp.signature(handler)
        params = {}
        for pname, param in sig.parameters.items():
            ptype = param.annotation if param.annotation != _insp.Parameter.empty else "str"
            if hasattr(ptype, "__name__"):
                ptype = ptype.__name__
            else:
                ptype = str(ptype)
            required = param.default is _insp.Parameter.empty
            params[pname] = {"type": ptype, "required": required}
        _tool_schemas[prefixed] = {
            "name": prefixed,
            "plugin": plugin.name,
            "access": tool_def.access,
            "description": tool_def.description or "",
            "parameters": params,
        }


def _visible_tools(ctx: RequestContext) -> list[str]:
    """Return tool names visible to the given context."""
    if ctx.is_admin:
        return list(_tool_registry.keys())
    visible = []
    for name, (plugin, tool_def) in _tool_registry.items():
        plugin_perms = ctx.permissions.get(plugin.name)
        if not plugin_perms:
            continue
        best = max(plugin_perms, key=lambda l: ACCESS_HIERARCHY.get(l, -1))
        if tool_def.requires_at_least(best):
            visible.append(name)
    return visible


def _search_index(query: str, tools: list[str], plugin_filter: str = "") -> list[dict]:
    """Simple keyword search over tool names and descriptions."""
    query_lower = query.lower()
    terms = query_lower.split()
    results = []
    for name in tools:
        schema = _tool_schemas.get(name)
        if not schema:
            continue
        if plugin_filter and schema["plugin"] != plugin_filter:
            continue
        text = f"{name} {schema['description']}".lower()
        score = sum(1 for t in terms if t in text)
        if score > 0:
            results.append((score, schema))
    results.sort(key=lambda x: -x[0])
    return [r[1] for r in results]


# ── Meta-tools (progressive discovery) ──────────────────────────────────────

@mcp.tool()
def rest_api_guide() -> dict:
    """Get instructions for using this MCP gateway as a plain REST API from scripts and code.

    Use this when you need to write deterministic scripts (Python, Node, bash, etc.)
    that call gateway tools via standard HTTP instead of the MCP protocol.
    Same auth, same tools, same permissions — just plain JSON over HTTP.
    """
    return {
        "base_url": f"{_MCP_BASE_URL}/api/v1",
        "auth": {
            "method": "Bearer token in Authorization header OR ?token= query parameter",
            "header_example": "Authorization: Bearer YOUR_API_KEY",
            "url_example": f"{_MCP_BASE_URL}/api/v1/plugins?token=YOUR_API_KEY",
        },
        "endpoints": {
            "GET /api/v1/plugins": "List available plugins, tool counts, and configured accounts",
            "GET /api/v1/tools?q=QUERY&plugin=PLUGIN&limit=N": "Search/list tools by keyword",
            "GET /api/v1/tools/{tool_name}": "Get full parameter schema for a specific tool",
            "POST /api/v1/call": "Execute a single tool. Body: {\"tool\": \"...\", \"params\": {...}, \"account\": \"...\"}",
            "POST /api/v1/batch": "Execute multiple tools in one request. Body: {\"calls\": [{\"tool\": \"...\", \"params\": {...}}, ...]}  (max 50)",
        },
        "security": "Unauthorized requests return 404 (endpoint appears to not exist). Tools you lack permission for also return 404.",
        "examples": {
            "python": (
                "import requests\n"
                "\n"
                f"BASE = \"{_MCP_BASE_URL}/api/v1\"\n"
                "HEADERS = {\"Authorization\": \"Bearer YOUR_API_KEY\"}\n"
                "\n"
                "# Discover tools\n"
                "plugins = requests.get(f\"{BASE}/plugins\", headers=HEADERS).json()\n"
                "schema = requests.get(f\"{BASE}/tools/myapp_users_list\", headers=HEADERS).json()\n"
                "\n"
                "# Execute a tool\n"
                "result = requests.post(f\"{BASE}/call\", headers=HEADERS, json={\n"
                "    \"tool\": \"myapp_users_list\",\n"
                "    \"params\": {\"page\": 1},\n"
                "    \"account\": \"prod\"\n"
                "}).json()\n"
                "\n"
                "# Batch (multiple tools, one HTTP request)\n"
                "batch = requests.post(f\"{BASE}/batch\", headers=HEADERS, json={\n"
                "    \"calls\": [\n"
                "        {\"tool\": \"myapp_get_current_user\"},\n"
                "        {\"tool\": \"myapp_users_list\", \"params\": {\"page\": 1}, \"account\": \"prod\"}\n"
                "    ]\n"
                "}).json()"
            ),
            "curl": (
                f"# List plugins\n"
                f"curl -H 'Authorization: Bearer YOUR_API_KEY' {_MCP_BASE_URL}/api/v1/plugins\n"
                f"\n"
                f"# Call a tool\n"
                f"curl -X POST -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' \\\n"
                f"  -d '{{\"tool\": \"myapp_users_list\", \"params\": {{\"page\": 1}}, \"account\": \"prod\"}}' \\\n"
                f"  {_MCP_BASE_URL}/api/v1/call"
            ),
        },
        "notes": [
            "Same API key works for both MCP and REST API access.",
            "Multi-account plugins: pass 'account' at the top level of the call body (not inside params).",
            "Batch endpoint runs calls sequentially and returns all results. Max 50 calls per batch.",
            "Image results include a temporary 'media_url' (valid 5 minutes) alongside base64 data.",
        ],
    }


@mcp.tool()
def list_plugins() -> dict:
    """List all available plugins, their tool counts, and configured accounts. Start here to see what's available."""
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    visible = _visible_tools(ctx)
    plugin_info: dict[str, dict] = {}
    for name in visible:
        schema = _tool_schemas[name]
        pname = schema["plugin"]
        if pname not in plugin_info:
            plugin_info[pname] = {"plugin": pname, "tool_count": 0, "access_levels": set(), "accounts": []}
        plugin_info[pname]["tool_count"] += 1
        plugin_info[pname]["access_levels"].add(schema["access"])

    from plugin_base import get_credentials
    result = []
    for pname, info in sorted(plugin_info.items()):
        try:
            creds = get_credentials(pname)
            accounts = set()
            for k in creds:
                if "." in k:
                    accounts.add(k.split(".")[0])
            if "api_key" in creds and not accounts:
                accounts.add("default")
            info["accounts"] = sorted(accounts)
        except Exception:
            info["accounts"] = []
        info["access_levels"] = sorted(info["access_levels"])
        result.append(info)
    return {"plugins": result, "total_tools": len(visible)}


@mcp.tool()
def search_tools(query: str, plugin: Optional[str] = None, limit: int = 20) -> dict:
    """Search for tools by keyword. Returns tool names and one-line descriptions.

    Args:
        query: Search keywords (e.g. "send message", "list items", "create record")
        plugin: Optional plugin name to filter by
        limit: Max results to return (default 20)
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    visible = _visible_tools(ctx)
    matches = _search_index(query, visible, plugin or "")
    trimmed = [{"name": m["name"], "description": m["description"][:120], "plugin": m["plugin"], "access": m["access"]} for m in matches[:limit]]
    return {"results": trimmed, "total_matches": len(matches), "showing": len(trimmed)}


@mcp.tool()
def get_tool_schema(tool_name: str) -> dict:
    """Get the full parameter schema for a specific tool. Call this before call_tool to see exact parameters.

    Args:
        tool_name: The full tool name (e.g. "myplugin_do_thing")
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    if tool_name not in _tool_schemas:
        return {"error": f"Tool '{tool_name}' not found. Use search_tools to find available tools."}
    visible = _visible_tools(ctx)
    if tool_name not in visible:
        return {"error": f"Tool '{tool_name}' not accessible with your permissions."}
    return _tool_schemas[tool_name]


def _to_mcp_image_content(result: dict) -> list:
    """Convert a dict with image_base64 into MCP native Image + Text content blocks."""
    from mcp.types import ImageContent, TextContent

    b64_data = result["image_base64"]
    content_type = result.get("content_type", "image/jpeg")
    file_path = result.get("file_path", "")

    media_url = None
    if file_path:
        token = media.create_token(file_path)
        media_url = f"{_MCP_BASE_URL}/media/{token}"

    return [
        ImageContent(type="image", data=b64_data, mimeType=content_type),
        TextContent(type="text", text=json.dumps({
            "success": True,
            "media_url": media_url,
            "content_type": content_type,
            "note": "media_url is valid for 5 minutes. Use it to pass this image to other tools/APIs.",
        })),
    ]


@mcp.tool()
def call_tool(tool_name: str, params_json: str = "{}", account: Optional[str] = None) -> Any:
    """Execute a tool by name. Use get_tool_schema first to see required parameters.

    Args:
        tool_name: The full tool name (e.g. "myplugin_do_thing")
        params_json: JSON object of parameters (e.g. '{"page": 1}')
        account: Account name for multi-account plugins. Required when multiple accounts exist.
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}

    entry = _tool_registry.get(tool_name)
    if not entry:
        return {"error": f"Tool '{tool_name}' not found. Use search_tools to find available tools."}

    plugin, tool_def = entry
    if not ctx.is_admin:
        plugin_perms = ctx.permissions.get(plugin.name)
        if not plugin_perms:
            return {"error": f"No access to plugin '{plugin.name}'."}
        best = max(plugin_perms, key=lambda l: ACCESS_HIERARCHY.get(l, -1))
        if not tool_def.requires_at_least(best):
            return {"error": f"Insufficient permissions. Tool requires '{tool_def.access}' access."}

    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in params_json"}

    if account:
        params["account"] = account

    handler = _tool_handlers.get(tool_name)
    if not handler:
        return {"error": f"No handler for '{tool_name}'."}

    result = handler(**params)

    if isinstance(result, dict) and result.get("image_base64") and result.get("success"):
        return _to_mcp_image_content(result)

    return result


# ── Admin tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def gateway_list_keys() -> list[dict]:
    """List all gateway API keys with their permissions. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return [{"error": "Admin access required"}]
    return auth.list_keys()


@mcp.tool()
def gateway_create_key(
    key_id: str,
    label: str = "",
    is_admin: bool = False,
    rate_limit: int = 100,
    expires_at: Optional[str] = None,
    allowed_ips: Optional[str] = None,
) -> dict:
    """Create a new gateway API key. Admin only.

    Args:
        key_id: Unique key identifier (e.g. "marketing_team")
        label: Human-readable label
        is_admin: Whether this key has admin access
        rate_limit: Max calls per minute (default 100)
        expires_at: ISO-8601 expiry date or null for never
        allowed_ips: Comma-separated CIDRs or null for any
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    return auth.create_key(key_id, label, is_admin, rate_limit, expires_at, allowed_ips)


@mcp.tool()
def gateway_delete_key(key_id: str) -> dict:
    """Delete a gateway API key. Admin only.

    Args:
        key_id: The key ID to delete
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    return auth.delete_key(key_id)


@mcp.tool()
def gateway_set_permissions(key_id: str, plugin: str, access_levels: str) -> dict:
    """Set plugin access levels for a key. Admin only.

    Args:
        key_id: The key ID
        plugin: Plugin name
        access_levels: Comma-separated access levels (e.g. "read,write" or "read,write,admin")
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    levels = [l.strip() for l in access_levels.split(",") if l.strip()]
    return auth.set_permissions(key_id, plugin, levels)


@mcp.tool()
def gateway_set_tool_override(key_id: str, tool_name: str, access_levels: str) -> dict:
    """Override access level for a specific tool on a key. Admin only.

    Args:
        key_id: The key ID
        tool_name: Full tool name (e.g. "myplugin_do_thing")
        access_levels: Comma-separated access levels
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    levels = [l.strip() for l in access_levels.split(",") if l.strip()]
    return auth.set_tool_override(key_id, tool_name, levels)


@mcp.tool()
def gateway_set_credentials(key_id: str, plugin: str, credentials_json: str) -> dict:
    """Set upstream API credentials for a key+plugin. Admin only. Replaces ALL existing credentials for this key+plugin.

    Args:
        key_id: The key ID
        plugin: Plugin name
        credentials_json: JSON object of credential key/value pairs (e.g. '{"api_key": "xxx", "base_url": "https://..."}')
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    try:
        creds = json.loads(credentials_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in credentials_json"}
    return auth.set_credentials(key_id, plugin, creds)


@mcp.tool()
def gateway_add_account(key_id: str, plugin: str, account_name: str, credentials_json: str) -> dict:
    """Add a named account for a plugin (supports multiple accounts of the same type). Admin only.

    This prefixes credential keys with the account name. For example, adding account "prod"
    with {"api_key": "xxx", "base_url": "https://..."} stores "prod.api_key" and "prod.base_url".
    Existing accounts are NOT removed — use this to add additional accounts.

    Args:
        key_id: The key ID
        plugin: Plugin name
        account_name: Short name for this account (e.g. "prod", "staging") — lowercase, no dots
        credentials_json: JSON object of credential key/value pairs for this account
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    if "." in account_name:
        return {"error": "account_name must not contain dots"}
    try:
        raw = json.loads(credentials_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in credentials_json"}
    prefixed = {f"{account_name}.{k}": v for k, v in raw.items()}
    return auth.upsert_credentials(key_id, plugin, prefixed)


@mcp.tool()
def gateway_set_own_credentials(plugin: str, credentials_json: str) -> dict:
    """Set your own API credentials for a plugin. Non-admin keys can configure their own accounts.

    Args:
        plugin: Plugin name (e.g. "gmail", "slack", "calendly")
        credentials_json: JSON object of credential key/value pairs (e.g. '{"api_key": "xxx"}')
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    if not ctx.is_admin:
        perms = ctx.permissions.get(plugin)
        if not perms:
            return {"error": f"You don't have access to plugin '{plugin}'"}
    try:
        creds = json.loads(credentials_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in credentials_json"}
    return auth.set_credentials(ctx.key_id, plugin, creds)


@mcp.tool()
def gateway_add_own_account(plugin: str, account_name: str, credentials_json: str) -> dict:
    """Add a named account for a plugin using your own credentials.

    This prefixes credential keys with the account name. For example, adding account "work"
    with {"api_key": "xxx"} stores "work.api_key". Existing accounts are NOT removed.

    Args:
        plugin: Plugin name (e.g. "calendly", "slack")
        account_name: Short name for this account (e.g. "work", "personal") — lowercase, no dots
        credentials_json: JSON object of credential key/value pairs for this account
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    if not ctx.is_admin:
        perms = ctx.permissions.get(plugin)
        if not perms:
            return {"error": f"You don't have access to plugin '{plugin}'"}
    if "." in account_name:
        return {"error": "account_name must not contain dots"}
    try:
        raw = json.loads(credentials_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in credentials_json"}
    prefixed = {f"{account_name}.{k}": v for k, v in raw.items()}
    return auth.upsert_credentials(ctx.key_id, plugin, prefixed)


@mcp.tool()
def gateway_list_own_credentials() -> dict:
    """List your configured credentials (keys only, not values) and which plugins you have access to."""
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    all_creds = auth.get_key_credentials(ctx.key_id)
    result: dict[str, Any] = {"key_id": ctx.key_id, "plugins": {}}
    accessible = set(ctx.permissions.keys()) if not ctx.is_admin else set(_plugin_map.keys())
    for plugin_name in sorted(accessible):
        plugin_creds = all_creds.get(plugin_name, {})
        cred_keys = sorted(plugin_creds.keys())
        accounts: list[str] = []
        seen = set()
        for k in cred_keys:
            if "." in k:
                acct = k.split(".")[0]
                if acct not in seen:
                    accounts.append(acct)
                    seen.add(acct)
        result["plugins"][plugin_name] = {
            "configured": bool(cred_keys),
            "credential_keys": cred_keys,
            "accounts": accounts,
        }
    return result


@mcp.tool()
def gateway_remove_own_credentials(plugin: str, account: Optional[str] = None) -> dict:
    """Remove your own credentials for a plugin, or a specific account within a plugin.

    Args:
        plugin: Plugin name
        account: If provided, only remove credentials for this account (e.g. "work"). If omitted, removes ALL credentials for the plugin.
    """
    ctx = _current_context.get()
    if not ctx:
        return {"error": "No request context"}
    if not ctx.is_admin:
        perms = ctx.permissions.get(plugin)
        if not perms:
            return {"error": f"You don't have access to plugin '{plugin}'"}
    conn = auth._get_db()
    if account:
        conn.execute(
            "DELETE FROM key_credentials WHERE key_id = ? AND plugin = ? AND credential_key LIKE ?",
            (ctx.key_id, plugin, f"{account}.%"),
        )
    else:
        conn.execute(
            "DELETE FROM key_credentials WHERE key_id = ? AND plugin = ?",
            (ctx.key_id, plugin),
        )
    conn.commit()
    conn.close()
    return {"success": True, "key_id": ctx.key_id, "plugin": plugin, "account_removed": account or "all"}


@mcp.tool()
def gateway_manage_scopes(
    key_id: str,
    plugin: str,
    scope_type: str,
    add: Optional[str] = None,
    remove: Optional[str] = None,
) -> dict:
    """Add or remove data scopes for a key+plugin. Admin only.

    Args:
        key_id: The key ID
        plugin: Plugin name
        scope_type: Scope type (e.g. "id", "channel")
        add: Comma-separated scope values to add
        remove: Comma-separated scope values to remove
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    add_list = [v.strip() for v in (add or "").split(",") if v.strip()] or None
    remove_list = [v.strip() for v in (remove or "").split(",") if v.strip()] or None
    return auth.manage_scopes(key_id, plugin, scope_type, add_list, remove_list)


@mcp.tool()
def gateway_set_rate_limit(
    key_id: str,
    scope: str,
    rate_limit: int,
) -> dict:
    """Set a granular rate limit for a key. Admin only.

    Args:
        key_id: The key ID
        scope: Scope string — 'plugin:<name>', 'account:<plugin>:<account>', or 'tool:<tool_name>'
        rate_limit: Max requests per minute (0 to remove the limit)
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    if rate_limit <= 0:
        return auth.delete_rate_limit(key_id, scope)
    return auth.set_rate_limit(key_id, scope, rate_limit)


@mcp.tool()
def gateway_get_rate_limits(key_id: str) -> dict:
    """Get all granular rate limits for a key. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    global_info = {}
    conn = auth._get_db()
    row = conn.execute("SELECT rate_limit FROM keys WHERE id = ?", (key_id,)).fetchone()
    conn.close()
    if row:
        global_info["global"] = row["rate_limit"]
    granular = auth.get_rate_limits(key_id)
    return {"key_id": key_id, "global_rate_limit": global_info.get("global", 100), "granular_limits": granular}


@mcp.tool()
def gateway_add_external_mcp(
    name: str,
    url: str,
    auth_header: Optional[str] = None,
    extra_headers_json: Optional[str] = None,
) -> dict:
    """Bridge an external MCP server through the gateway. Admin only.

    Args:
        name: Plugin namespace (e.g. "myservice"). Tools will be prefixed as myservice_<tool>
        url: Streamable HTTP MCP endpoint URL (e.g. "https://example.com/mcp")
        auth_header: Auth value — either "Bearer <token>" / "x-api-key: <key>" / or just the token
        extra_headers_json: Optional JSON object of additional HTTP headers
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}

    result = external_mcp.add_external_mcp(name, url, auth_header or "", extra_headers_json or "")
    if result.get("error"):
        return result

    plugin, err = external_mcp.refresh_external_plugin(name)
    if err:
        return {"warning": f"Saved but failed to connect: {err}", "name": name}

    _register_single_plugin(plugin)
    _plugins.append(plugin)
    _plugin_map[plugin.name] = plugin

    dashboard.init_dashboard(_plugin_map, _tool_registry)

    return {"success": True, "name": name, "url": url, "tools_discovered": len(plugin.tools)}


@mcp.tool()
def gateway_remove_external_mcp(name: str) -> dict:
    """Remove a bridged external MCP server. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}

    result = external_mcp.remove_external_mcp(name)

    for key in list(_tool_registry.keys()):
        if key.startswith(f"{name}_"):
            _tool_registry.pop(key, None)
            _tool_handlers.pop(key, None)
            _tool_schemas.pop(key, None)

    _plugin_map.pop(name, None)
    global _plugins
    _plugins = [p for p in _plugins if p.name != name]

    dashboard.init_dashboard(_plugin_map, _tool_registry)

    return result


@mcp.tool()
def gateway_list_external_mcps() -> list[dict]:
    """List all configured external MCP bridges. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return [{"error": "Admin access required"}]
    configs = external_mcp.list_external_mcps()
    for cfg in configs:
        if cfg.get("auth_header"):
            val = cfg["auth_header"]
            cfg["auth_header"] = val[:12] + "..." if len(val) > 15 else "***"
    return configs


@mcp.tool()
def gateway_refresh_external_mcp(name: str) -> dict:
    """Re-connect and re-discover tools from an external MCP. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}

    for key in list(_tool_registry.keys()):
        if key.startswith(f"{name}_"):
            _tool_registry.pop(key, None)
            _tool_handlers.pop(key, None)
            _tool_schemas.pop(key, None)

    _plugin_map.pop(name, None)
    global _plugins
    _plugins = [p for p in _plugins if p.name != name]

    plugin, err = external_mcp.refresh_external_plugin(name)
    if err:
        return {"error": err}

    _register_single_plugin(plugin)
    _plugins.append(plugin)
    _plugin_map[plugin.name] = plugin

    dashboard.init_dashboard(_plugin_map, _tool_registry)

    return {"success": True, "name": name, "tools_discovered": len(plugin.tools)}


@mcp.tool()
def gateway_audit_log(
    key_id: Optional[str] = None,
    plugin: Optional[str] = None,
    tool_name: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Query the audit log. Admin only.

    Args:
        key_id: Filter by key ID
        plugin: Filter by plugin name
        tool_name: Filter by tool name
        limit: Max results (default 50)
    """
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return [{"error": "Admin access required"}]
    return audit.query_audit_log(key_id=key_id, plugin=plugin, tool_name=tool_name, limit=limit)


@mcp.tool()
def gateway_plugin_health() -> dict:
    """Check health of all plugin upstream dependencies. Admin only."""
    ctx = _current_context.get()
    if not ctx or not ctx.is_admin:
        return {"error": "Admin access required"}
    result = {}
    for plugin in _plugins:
        try:
            result[plugin.name] = plugin.health_check()
        except Exception as exc:
            result[plugin.name] = {"status": "error", "error": str(exc)}
    return result


# ── OAuth 2.1 endpoints ──────────────────────────────────────────────────────

async def oauth_protected_resource(request: Request):
    base = _get_base_url()
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
    })


async def oauth_authorization_server(request: Request):
    base = _get_base_url()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": ["mcp:tools"],
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def register(request: Request):
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    info = {
        "client_id": client_id,
        "client_name": body.get("client_name", "MCP Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    _registered_clients[client_id] = info
    _save_state()
    return JSONResponse(info, status_code=201)


_AUTHORIZE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Gateway - Connect</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;display:flex;
  justify-content:center;align-items:center;min-height:100vh}}
.card{{background:#1e293b;border-radius:16px;padding:40px;max-width:420px;width:100%;
  box-shadow:0 4px 24px rgba(0,0,0,.3);border:1px solid #334155}}
.logo{{font-size:28px;font-weight:700;margin-bottom:4px;color:#f8fafc}}
.logo span{{color:#38bdf8}}
.subtitle{{color:#94a3b8;margin-bottom:28px;font-size:14px;line-height:1.5}}
label{{display:block;font-size:13px;font-weight:600;color:#cbd5e1;margin-bottom:6px;margin-top:16px}}
input[type=password]{{width:100%;padding:12px 14px;border:1px solid #475569;background:#0f172a;color:#f8fafc;
  border-radius:10px;font-size:14px;transition:border-color .2s}}
input:focus{{outline:none;border-color:#38bdf8;box-shadow:0 0 0 3px rgba(56,189,248,.15)}}
.hint{{font-size:12px;color:#64748b;margin-top:4px}}
button{{width:100%;padding:14px;background:#38bdf8;color:#0f172a;border:none;
  border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;margin-top:24px;
  transition:background .2s}}
button:hover{{background:#0ea5e9}}
.err{{color:#f87171;margin-bottom:16px;font-size:14px;padding:10px;
  background:rgba(248,113,113,.1);border-radius:8px;border:1px solid rgba(248,113,113,.2)}}
.plugins{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}}
.pill{{background:#334155;color:#94a3b8;padding:4px 12px;border-radius:20px;font-size:12px}}
</style></head><body>
<div class="card">
<div class="logo">MCP <span>Gateway</span></div>
<p class="subtitle">Unified access to all your MCP tools.</p>
<div class="plugins">{plugin_pills}</div>
{error_html}
<form method="POST" action="/authorize">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="hidden" name="response_type" value="{response_type}">
  <label for="api_key">Gateway API Key</label>
  <input type="password" id="api_key" name="api_key" placeholder="Your gateway key" required autofocus>
  <div class="hint">Provided by your admin</div>
  <button type="submit">Connect</button>
</form>
</div></body></html>"""


def _make_plugin_pills() -> str:
    return "".join(f'<span class="pill">{p.name}</span>' for p in _plugins)


async def authorize(request: Request):
    if request.method == "GET":
        qp = dict(request.query_params)
        client = _registered_clients.get(qp.get("client_id", ""), {})
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_name=html_module.escape(client.get("client_name", "Unknown")),
            client_id=html_module.escape(qp.get("client_id", "")),
            redirect_uri=html_module.escape(qp.get("redirect_uri", "")),
            state=html_module.escape(qp.get("state", "")),
            code_challenge=html_module.escape(qp.get("code_challenge", "")),
            code_challenge_method=html_module.escape(qp.get("code_challenge_method", "")),
            scope=html_module.escape(qp.get("scope", "")),
            response_type=html_module.escape(qp.get("response_type", "")),
            error_html="",
            plugin_pills=_make_plugin_pills(),
        ))

    form = await request.form()
    api_key = str(form.get("api_key", "")).strip()

    def _rerender(error_msg: str):
        client = _registered_clients.get(str(form.get("client_id", "")), {})
        return HTMLResponse(_AUTHORIZE_HTML.format(
            client_name=html_module.escape(client.get("client_name", "Unknown")),
            client_id=html_module.escape(str(form.get("client_id", ""))),
            redirect_uri=html_module.escape(str(form.get("redirect_uri", ""))),
            state=html_module.escape(str(form.get("state", ""))),
            code_challenge=html_module.escape(str(form.get("code_challenge", ""))),
            code_challenge_method=html_module.escape(str(form.get("code_challenge_method", ""))),
            scope=html_module.escape(str(form.get("scope", ""))),
            response_type=html_module.escape(str(form.get("response_type", ""))),
            error_html=f'<p class="err">{html_module.escape(error_msg)}</p>',
            plugin_pills=_make_plugin_pills(),
        ))

    if not api_key:
        return _rerender("Gateway API key is required.")

    key_info = auth.resolve_key(api_key)
    if not key_info:
        return _rerender("Invalid gateway key.")

    if auth.is_key_expired(key_info):
        return _rerender("This key has expired.")

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": str(form.get("client_id", "")),
        "redirect_uri": str(form.get("redirect_uri", "")),
        "code_challenge": str(form.get("code_challenge", "")),
        "code_challenge_method": str(form.get("code_challenge_method", "")),
        "created_at": time.time(),
        "key_id": key_info["id"],
    }

    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


async def token_endpoint(request: Request):
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        raw = await request.json()
    else:
        form_data = await request.form()
        raw = {k: v for k, v in form_data.items()}

    grant_type = raw.get("grant_type", "")
    code = str(raw.get("code", ""))
    code_verifier = str(raw.get("code_verifier", ""))

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    if code not in _auth_codes:
        return JSONResponse({"error": "invalid_grant", "error_description": "Code not found"}, status_code=400)

    code_data = _auth_codes.pop(code)

    if time.time() - code_data["created_at"] > 600:
        return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)

    challenge = code_data.get("code_challenge", "")
    if challenge and code_verifier:
        digest = hashlib.sha256(code_verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if expected != challenge:
            print(f"[token] PKCE mismatch — accepting anyway", flush=True)

    access_token = secrets.token_urlsafe(48)
    _issued_tokens[access_token] = code_data["key_id"]
    _save_state()
    print(f"[token] issued token for key={code_data['key_id']}", flush=True)

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 86400 * 365,
        "scope": "mcp:tools",
    })


# ── Health endpoint ──────────────────────────────────────────────────────────

_health_cache: dict[str, Any] = {}
_health_cache_time: float = 0


async def health_endpoint(request: Request):
    global _health_cache, _health_cache_time

    now = time.time()
    if now - _health_cache_time < 10:
        return JSONResponse(_health_cache)

    checks: dict[str, Any] = {"gateway": "healthy"}

    if auth.db_health():
        checks["sqlite"] = "healthy"
    else:
        checks["sqlite"] = "unhealthy"
        return JSONResponse(checks, status_code=503)

    for label, url in [("whatsapp_bridge", "http://localhost:7481"), ("whatsapp_bridge_2", "http://localhost:7482")]:
        try:
            resp = httpx.get(f"{url}/api/backfill-contacts", timeout=3.0, headers={"Content-Type": "application/json"})
            checks[label] = "healthy" if resp.status_code < 500 else "unhealthy"
        except Exception:
            checks[label] = "unavailable"

    _health_cache = checks
    _health_cache_time = now
    status = 200 if checks.get("sqlite") == "healthy" else 503
    return JSONResponse(checks, status_code=status)


# ── Auth middleware ───────────────────────────────────────────────────────────

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Mcp-Session-Id",
    "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
}

_PUBLIC_PREFIXES = (
    "/.well-known/",
    "/authorize",
    "/token",
    "/register",
    "/health",
    "/dash",
    "/webhook/",
    "/media/",
)


class GatewayAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            response = JSONResponse({}, status_code=204, headers=_CORS_HEADERS)
            await response(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/" or any(path.startswith(pfx) for pfx in _PUBLIC_PREFIXES):
            await self._with_cors(scope, receive, send)
            return

        master_token = os.environ.get("MCP_AUTH_TOKEN", "")
        if not master_token:
            credentials = auth.get_key_credentials(auth.ADMIN_KEY_ID)
            data_scopes: dict[str, set[str] | None] = {p: None for p in _plugin_map}
            ctx = RequestContext(key_id=auth.ADMIN_KEY_ID, is_admin=True, credentials=credentials, data_scopes=data_scopes)
            _current_context.set(ctx)
            await self._with_cors(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()
        bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""

        if not bearer:
            query = scope.get("query_string", b"").decode()
            for part in query.split("&"):
                if part.startswith("token="):
                    bearer = part[6:]
                    break

        key_id = _issued_tokens.get(bearer)
        if not key_id:
            key_info = auth.resolve_key(bearer)
            if key_info:
                key_id = key_info["id"]

        _stealth = path.startswith("/api/")

        if not key_id:
            if _stealth:
                response = JSONResponse({"detail": "Not Found"}, status_code=404, headers=_CORS_HEADERS)
            else:
                base = _get_base_url()
                cors_plus_auth = {
                    **_CORS_HEADERS,
                    "WWW-Authenticate": f'Bearer realm="MCP Gateway", resource_metadata="{base}/.well-known/oauth-protected-resource"',
                }
                response = JSONResponse({"error": "unauthorized"}, status_code=401, headers=cors_plus_auth)
            await response(scope, receive, send)
            return

        key_info = auth.resolve_key(bearer) if not _issued_tokens.get(bearer) else None
        if key_info is None:
            conn_key = None
            import sqlite3
            conn = sqlite3.connect(os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db"), timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM keys WHERE id = ?", (key_id,)).fetchone()
            conn.close()
            if row:
                key_info = dict(row)

        if key_info and auth.is_key_expired(key_info):
            if _stealth:
                response = JSONResponse({"detail": "Not Found"}, status_code=404, headers=_CORS_HEADERS)
            else:
                response = JSONResponse({"error": "key expired"}, status_code=401, headers=_CORS_HEADERS)
            await response(scope, receive, send)
            return

        if key_info:
            client_ip = ""
            for header_name in [b"x-forwarded-for", b"x-real-ip"]:
                val = headers.get(header_name, b"").decode()
                if val:
                    client_ip = val.split(",")[0].strip()
                    break
            if client_ip and not auth.check_ip_allowed(key_info, client_ip):
                if _stealth:
                    response = JSONResponse({"detail": "Not Found"}, status_code=404, headers=_CORS_HEADERS)
                else:
                    response = JSONResponse({"error": "IP not allowed"}, status_code=403, headers=_CORS_HEADERS)
                await response(scope, receive, send)
                return

            rate_limit = key_info.get("rate_limit", 100)
            if not auth.check_rate_limit(key_id, rate_limit):
                response = JSONResponse({"error": "rate limit exceeded"}, status_code=429, headers=_CORS_HEADERS)
                await response(scope, receive, send)
                return

        is_admin = bool(key_info and key_info.get("is_admin"))
        permissions = auth.get_key_permissions(key_id)
        credentials = auth.get_key_credentials(key_id)
        data_scopes_raw = auth.get_key_data_scopes(key_id)
        data_scopes: dict[str, set[str] | None] = {}
        for plugin_name in _plugin_map:
            if is_admin:
                data_scopes[plugin_name] = None
            else:
                data_scopes[plugin_name] = data_scopes_raw.get(plugin_name)

        ctx = RequestContext(
            key_id=key_id,
            is_admin=is_admin,
            permissions=permissions,
            credentials=credentials,
            data_scopes=data_scopes,
        )
        _current_context.set(ctx)
        await self._with_cors(scope, receive, send)

    async def _with_cors(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def cors_send(message):
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                for k, v in _CORS_HEADERS.items():
                    hdrs.append((k.lower().encode(), v.encode()))
                message = {**message, "headers": hdrs}
            await send(message)
        await self.app(scope, receive, cors_send)


# ── REST API Bridge ──────────────────────────────────────────────────────────
# Plain JSON endpoints — same auth, same plugins, no MCP protocol needed.
# Unauthorized requests receive 404 (stealth).

_API_NOT_FOUND = {"detail": "Not Found"}


async def api_list_plugins(request: Request):
    ctx = _current_context.get()
    if not ctx:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    return JSONResponse(list_plugins())


async def api_search_tools(request: Request):
    ctx = _current_context.get()
    if not ctx:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    q = request.query_params.get("q", "")
    plugin_filter = request.query_params.get("plugin", "")
    limit = min(int(request.query_params.get("limit", "50")), 200)
    visible = _visible_tools(ctx)
    if q:
        matches = _search_index(q, visible, plugin_filter)
    else:
        matches = [_tool_schemas[n] for n in visible
                   if not plugin_filter or _tool_schemas[n]["plugin"] == plugin_filter]
    trimmed = [{"name": m["name"], "description": m["description"][:120],
                "plugin": m["plugin"], "access": m["access"]} for m in matches[:limit]]
    return JSONResponse({"results": trimmed, "total": len(matches), "showing": len(trimmed)})


async def api_get_tool_schema(request: Request):
    ctx = _current_context.get()
    if not ctx:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    tool_name = request.path_params["tool_name"]
    if tool_name not in _tool_schemas:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    visible = _visible_tools(ctx)
    if tool_name not in visible:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    return JSONResponse(_tool_schemas[tool_name])


async def api_call_tool(request: Request):
    ctx = _current_context.get()
    if not ctx:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    tool_name = body.get("tool", "")
    params = body.get("params", {})
    account = body.get("account")

    entry = _tool_registry.get(tool_name)
    if not entry:
        return JSONResponse(_API_NOT_FOUND, status_code=404)

    plugin, tool_def = entry
    if not ctx.is_admin:
        plugin_perms = ctx.permissions.get(plugin.name)
        if not plugin_perms:
            return JSONResponse(_API_NOT_FOUND, status_code=404)
        best = max(plugin_perms, key=lambda l: ACCESS_HIERARCHY.get(l, -1))
        if not tool_def.requires_at_least(best):
            return JSONResponse(_API_NOT_FOUND, status_code=404)

    if account:
        params["account"] = account

    handler = _tool_handlers.get(tool_name)
    if not handler:
        return JSONResponse({"error": "Internal error"}, status_code=500)

    result = handler(**params)

    if isinstance(result, dict) and result.get("image_base64") and result.get("file_path"):
        token = media.create_token(result["file_path"])
        result["media_url"] = f"{_MCP_BASE_URL}/media/{token}"

    return JSONResponse(result)


async def api_batch(request: Request):
    ctx = _current_context.get()
    if not ctx:
        return JSONResponse(_API_NOT_FOUND, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    calls = body.get("calls", [])
    if not isinstance(calls, list) or len(calls) > 50:
        return JSONResponse({"error": "'calls' must be an array of max 50 items"}, status_code=400)

    results = []
    for call_spec in calls:
        tool_name = call_spec.get("tool", "")
        params = call_spec.get("params", {})
        account = call_spec.get("account")

        entry = _tool_registry.get(tool_name)
        if not entry:
            results.append({"tool": tool_name, "error": "Not found"})
            continue

        plugin, tool_def = entry
        if not ctx.is_admin:
            plugin_perms = ctx.permissions.get(plugin.name)
            if not plugin_perms:
                results.append({"tool": tool_name, "error": "Not found"})
                continue
            best = max(plugin_perms, key=lambda l: ACCESS_HIERARCHY.get(l, -1))
            if not tool_def.requires_at_least(best):
                results.append({"tool": tool_name, "error": "Not found"})
                continue

        if account:
            params["account"] = account

        handler = _tool_handlers.get(tool_name)
        if not handler:
            results.append({"tool": tool_name, "error": "Internal error"})
            continue

        result = handler(**params)
        if isinstance(result, dict) and result.get("image_base64") and result.get("file_path"):
            token = media.create_token(result["file_path"])
            result["media_url"] = f"{_MCP_BASE_URL}/media/{token}"

        results.append({"tool": tool_name, "result": result})

    return JSONResponse({"results": results})


# ── App assembly ──────────────────────────────────────────────────────────────

_SELF_SERVICE_TOOLS = {
    "gateway_set_own_credentials",
    "gateway_add_own_account",
    "gateway_list_own_credentials",
    "gateway_remove_own_credentials",
}


def _is_mcp_tool_visible(tool_name: str, ctx: RequestContext) -> bool:
    """Filter the MCP-level tool listing (meta-tools + admin tools)."""
    if ctx.is_admin:
        return True
    if tool_name in _SELF_SERVICE_TOOLS:
        return True
    if tool_name.startswith("gateway_"):
        return False
    return True


def _patch_tool_listing():
    """Monkey-patch the MCP server's tools/list handler to filter by key permissions."""
    from mcp.types import ListToolsRequest

    server = mcp._mcp_server
    original_handler = server.request_handlers.get(ListToolsRequest)
    if not original_handler:
        print("[gateway] WARNING: could not find tools/list handler to patch", flush=True)
        return

    async def filtered_list_tools(req):
        from mcp.types import ServerResult, ListToolsResult

        result = await original_handler(req)
        ctx = _current_context.get()
        if not ctx or ctx.is_admin:
            return result

        inner = result.root if hasattr(result, "root") else result
        if not isinstance(inner, ListToolsResult):
            return result

        inner.tools = [t for t in inner.tools if _is_mcp_tool_visible(t.name, ctx)]
        return ServerResult(inner)

    server.request_handlers[ListToolsRequest] = filtered_list_tools
    print(f"[gateway] patched tools/list for per-key filtering", flush=True)


def _build_app() -> ASGIApp:
    from mcp.server.sse import SseServerTransport

    auth.init_db()
    audit.init_audit_db()
    _load_state()
    _register_plugins()
    _patch_tool_listing()

    def _ext_register(plugin):
        _register_single_plugin(plugin)
        _plugins.append(plugin)
        _plugin_map[plugin.name] = plugin
        dashboard.init_dashboard(_plugin_map, _tool_registry, _ext_register, _ext_unregister)

    def _ext_unregister(name):
        for key in list(_tool_registry.keys()):
            if key.startswith(f"{name}_"):
                _tool_registry.pop(key, None)
                _tool_handlers.pop(key, None)
                _tool_schemas.pop(key, None)
        _plugin_map.pop(name, None)
        global _plugins
        _plugins = [p for p in _plugins if p.name != name]
        dashboard.init_dashboard(_plugin_map, _tool_registry, _ext_register, _ext_unregister)

    dashboard.init_dashboard(_plugin_map, _tool_registry, _ext_register, _ext_unregister)

    http_app = mcp.streamable_http_app()
    mcp_handler = http_app.routes[0].app

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_handler.session_manager.run():
            yield

    sse = SseServerTransport("/messages")
    mcp_server = mcp._mcp_server

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())

    routes = [
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/.well-known/oauth-authorization-server/{path:path}", oauth_authorization_server),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token_endpoint, methods=["POST"]),
        Route("/health", health_endpoint),
        Route("/media/{token}", media.serve_media),
        Route("/mcp", mcp_handler),
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages", app=sse.handle_post_message),
        Route("/api/v1/plugins", api_list_plugins),
        Route("/api/v1/tools", api_search_tools),
        Route("/api/v1/tools/{tool_name}", api_get_tool_schema),
        Route("/api/v1/call", api_call_tool, methods=["POST"]),
        Route("/api/v1/batch", api_batch, methods=["POST"]),
    ]

    for route in dashboard.get_dashboard_routes():
        routes.append(route)

    for plugin in _plugins:
        for route in plugin.extra_routes():
            routes.append(route)

    app = Starlette(routes=routes, lifespan=lifespan)
    app = GatewayAuthMiddleware(app)
    return app


if __name__ == "__main__":
    import uvicorn

    app = _build_app()
    config = uvicorn.Config(app, host="0.0.0.0", port=_MCP_PORT, log_level="info")
    server = uvicorn.Server(config)
    server.run()
