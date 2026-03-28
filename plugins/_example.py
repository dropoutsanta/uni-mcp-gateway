"""Example plugin — copy this file to create your own plugin.

Rename the file (e.g. `my_service.py`), update the class name and `name` field,
and define your tools. The gateway auto-discovers all MCPPlugin subclasses in
the plugins/ directory on startup.
"""

from __future__ import annotations

from typing import Optional

import requests

from plugin_base import MCPPlugin, ToolDef, get_credentials


def _get_api_key() -> str:
    creds = get_credentials("example")
    api_key = creds.get("api_key", "")
    if not api_key:
        raise RuntimeError("Example plugin: api_key not configured. Set it via gateway_set_credentials.")
    return api_key


def list_items(query: Optional[str] = None, limit: int = 10) -> dict:
    """List items from the example API.

    Args:
        query: Optional search query
        limit: Max results to return (default 10)
    """
    api_key = _get_api_key()
    params = {"limit": limit}
    if query:
        params["q"] = query
    resp = requests.get(
        "https://api.example.com/v1/items",
        headers={"Authorization": f"Bearer {api_key}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_item(item_id: str) -> dict:
    """Get a single item by ID.

    Args:
        item_id: The item identifier
    """
    api_key = _get_api_key()
    resp = requests.get(
        f"https://api.example.com/v1/items/{item_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def create_item(name: str, description: str = "") -> dict:
    """Create a new item.

    Args:
        name: Item name (required)
        description: Optional description
    """
    api_key = _get_api_key()
    resp = requests.post(
        "https://api.example.com/v1/items",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"name": name, "description": description},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


class ExamplePlugin(MCPPlugin):
    name = "example"
    tools = {
        "list_items": ToolDef(access="read", handler=list_items, description=list_items.__doc__ or ""),
        "get_item": ToolDef(access="read", handler=get_item, description=get_item.__doc__ or ""),
        "create_item": ToolDef(access="write", handler=create_item, description=create_item.__doc__ or ""),
    }
