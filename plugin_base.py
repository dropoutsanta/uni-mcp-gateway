"""Plugin base classes for the MCP Gateway.

Each plugin inherits from MCPPlugin and declares its tools with access levels.
The gateway auto-discovers plugins, registers their tools, and enforces
permissions at the gateway level.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


ACCESS_HIERARCHY = {"read": 0, "write": 1, "admin": 2}


@dataclass
class ToolDef:
    """Definition of a single MCP tool within a plugin."""
    access: str
    handler: Callable
    description: str

    def requires_at_least(self, granted: str) -> bool:
        return ACCESS_HIERARCHY.get(granted, -1) >= ACCESS_HIERARCHY.get(self.access, 99)


@dataclass
class RequestContext:
    """Per-request context set by the gateway auth middleware."""
    key_id: str
    is_admin: bool
    permissions: dict[str, set[str]] = field(default_factory=dict)
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    data_scopes: dict[str, set[str] | None] = field(default_factory=dict)


_current_context: contextvars.ContextVar[Optional[RequestContext]] = (
    contextvars.ContextVar("gateway_ctx", default=None)
)


def get_context() -> RequestContext:
    ctx = _current_context.get()
    if ctx is None:
        raise RuntimeError("No gateway request context available")
    return ctx


def get_credentials(plugin_name: str) -> dict[str, str]:
    """Fetch credentials fresh from the DB every call so changes are picked up
    without needing to reconnect the MCP session."""
    ctx = get_context()
    try:
        import auth as _auth_mod
        all_creds = _auth_mod.get_key_credentials(ctx.key_id)
        return all_creds.get(plugin_name, {})
    except Exception:
        return ctx.credentials.get(plugin_name, {})


def get_data_scopes(plugin_name: str) -> set[str] | None:
    """Return the set of allowed scope values, or None for unrestricted."""
    ctx = get_context()
    if ctx.is_admin:
        return None
    return ctx.data_scopes.get(plugin_name)


class MCPPlugin:
    """Base class for gateway plugins.

    Subclasses must set `name` and populate `tools`.
    """
    name: str = ""
    tools: dict[str, ToolDef] = {}

    def extra_routes(self) -> list:
        """Return additional Starlette Route objects (e.g. webhook endpoints)."""
        return []

    def health_check(self) -> dict[str, Any]:
        """Return health status of this plugin's upstream dependencies."""
        return {"status": "ok"}
