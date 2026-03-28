"""Notion MCP Gateway plugin.

Wraps the Notion REST API v1 (https://api.notion.com/v1/).
Credentials come from gateway get_credentials("notion") -> {"api_key": "..."}.
Auth header: Authorization: Bearer {api_key}.
"""

import json
from typing import Any, Optional

import requests

from plugin_base import MCPPlugin, ToolDef, get_credentials

_NOTION_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _notion_request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    creds = get_credentials("notion")
    api_key = creds.get("api_key", "")
    if not api_key:
        return {"error": "Not authenticated. Configure Notion API key via gateway credentials."}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }

    url = f"{_NOTION_BASE}{path}"
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=body,
            params=params,
            timeout=30.0,
        )
    except requests.Timeout:
        return {"error": "Request timed out after 30s"}
    except requests.RequestException as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code >= 400:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        return {"error": f"HTTP {resp.status_code}", "details": err_body}

    if resp.status_code == 204:
        return {"success": True}

    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"error": "Invalid JSON response from Notion API"}


def _parse_json_param(value: Optional[str], name: str) -> tuple:
    """Parse a JSON string param. Returns (parsed, error_dict)."""
    if not value:
        return None, None
    try:
        return json.loads(value), None
    except json.JSONDecodeError as exc:
        return None, {"error": f"Invalid JSON for {name}: {exc}"}


# ── Read tools ────────────────────────────────────────────────────────────────


def search(
    query: str = "",
    filter_type: Optional[str] = None,
    start_cursor: Optional[str] = None,
    page_size: int = 20,
) -> dict:
    """Search pages and databases by query. filter_type can be 'page' or 'database'."""
    body: dict[str, Any] = {"page_size": min(page_size, 100)}
    if query:
        body["query"] = query
    if filter_type in ("page", "database"):
        body["filter"] = {"value": filter_type, "property": "object"}
    if start_cursor:
        body["start_cursor"] = start_cursor
    return _notion_request("POST", "/search", body=body)


def get_page(page_id: str) -> dict:
    """Get a page by ID."""
    return _notion_request("GET", f"/pages/{page_id}")


def get_page_content(
    page_id: str,
    start_cursor: Optional[str] = None,
    page_size: int = 100,
) -> dict:
    """Get all blocks (content) of a page with pagination."""
    params: dict[str, Any] = {"page_size": min(page_size, 100)}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return _notion_request("GET", f"/blocks/{page_id}/children", params=params)


def get_database(database_id: str) -> dict:
    """Get a database by ID."""
    return _notion_request("GET", f"/databases/{database_id}")


def query_database(
    database_id: str,
    filter_json: Optional[str] = None,
    sorts_json: Optional[str] = None,
    start_cursor: Optional[str] = None,
    page_size: int = 100,
) -> dict:
    """Query a database with optional filter and sorts (passed as JSON strings)."""
    body: dict[str, Any] = {"page_size": min(page_size, 100)}

    if filter_json:
        parsed, err = _parse_json_param(filter_json, "filter_json")
        if err:
            return err
        body["filter"] = parsed

    if sorts_json:
        parsed, err = _parse_json_param(sorts_json, "sorts_json")
        if err:
            return err
        body["sorts"] = parsed

    if start_cursor:
        body["start_cursor"] = start_cursor

    return _notion_request("POST", f"/databases/{database_id}/query", body=body)


def get_block(block_id: str) -> dict:
    """Get a block by ID."""
    return _notion_request("GET", f"/blocks/{block_id}")


def get_block_children(
    block_id: str,
    start_cursor: Optional[str] = None,
    page_size: int = 100,
) -> dict:
    """Get children blocks of a block or page."""
    params: dict[str, Any] = {"page_size": min(page_size, 100)}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return _notion_request("GET", f"/blocks/{block_id}/children", params=params)


def list_users(
    start_cursor: Optional[str] = None,
    page_size: int = 100,
) -> dict:
    """List all users in the workspace."""
    params: dict[str, Any] = {"page_size": min(page_size, 100)}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return _notion_request("GET", "/users", params=params)


def get_user(user_id: str) -> dict:
    """Get a user by ID."""
    return _notion_request("GET", f"/users/{user_id}")


def list_comments(
    block_id: Optional[str] = None,
    start_cursor: Optional[str] = None,
    page_size: int = 100,
) -> dict:
    """List comments on a page or block. Provide block_id to scope to a specific page/block."""
    params: dict[str, Any] = {"page_size": min(page_size, 100)}
    if block_id:
        params["block_id"] = block_id
    if start_cursor:
        params["start_cursor"] = start_cursor
    return _notion_request("GET", "/comments", params=params)


# ── Write tools ──────────────────────────────────────────────────────────────


def create_page(
    parent_type: str,
    parent_id: str,
    properties_json: str,
    children_json: Optional[str] = None,
    icon_json: Optional[str] = None,
    cover_json: Optional[str] = None,
) -> dict:
    """Create a page in a database or as a child of another page.
    parent_type: 'database_id' or 'page_id'.
    properties_json: JSON string of page properties.
    children_json: optional JSON array of block objects for page content.
    """
    props, err = _parse_json_param(properties_json, "properties_json")
    if err:
        return err
    if props is None:
        return {"error": "properties_json is required"}

    body: dict[str, Any] = {
        "parent": {parent_type: parent_id},
        "properties": props,
    }

    if children_json:
        parsed, err = _parse_json_param(children_json, "children_json")
        if err:
            return err
        body["children"] = parsed

    if icon_json:
        parsed, err = _parse_json_param(icon_json, "icon_json")
        if err:
            return err
        body["icon"] = parsed

    if cover_json:
        parsed, err = _parse_json_param(cover_json, "cover_json")
        if err:
            return err
        body["cover"] = parsed

    return _notion_request("POST", "/pages", body=body)


def update_page(
    page_id: str,
    properties_json: Optional[str] = None,
    archived: Optional[bool] = None,
    icon_json: Optional[str] = None,
    cover_json: Optional[str] = None,
) -> dict:
    """Update page properties. properties_json is a JSON string of properties to update."""
    body: dict[str, Any] = {}

    if properties_json:
        parsed, err = _parse_json_param(properties_json, "properties_json")
        if err:
            return err
        body["properties"] = parsed

    if archived is not None:
        body["archived"] = archived

    if icon_json:
        parsed, err = _parse_json_param(icon_json, "icon_json")
        if err:
            return err
        body["icon"] = parsed

    if cover_json:
        parsed, err = _parse_json_param(cover_json, "cover_json")
        if err:
            return err
        body["cover"] = parsed

    if not body:
        return {"error": "No fields to update"}

    return _notion_request("PATCH", f"/pages/{page_id}", body=body)


def append_blocks(
    block_id: str,
    children_json: str,
) -> dict:
    """Append block children to a page or block. children_json is a JSON array of block objects."""
    parsed, err = _parse_json_param(children_json, "children_json")
    if err:
        return err
    if not parsed:
        return {"error": "children_json is required"}
    return _notion_request("PATCH", f"/blocks/{block_id}/children", body={"children": parsed})


def update_block(
    block_id: str,
    block_json: str,
) -> dict:
    """Update a block. block_json is a JSON string of the block update payload (e.g. type-specific content, archived flag)."""
    parsed, err = _parse_json_param(block_json, "block_json")
    if err:
        return err
    if not parsed:
        return {"error": "block_json is required"}
    return _notion_request("PATCH", f"/blocks/{block_id}", body=parsed)


def create_database(
    parent_page_id: str,
    title_json: str,
    properties_json: str,
) -> dict:
    """Create a database as a child of a page.
    title_json: JSON array of rich text objects for the database title.
    properties_json: JSON object defining database properties/schema.
    """
    title, err = _parse_json_param(title_json, "title_json")
    if err:
        return err
    props, err = _parse_json_param(properties_json, "properties_json")
    if err:
        return err
    if not props:
        return {"error": "properties_json is required"}

    body: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "properties": props,
    }
    if title:
        body["title"] = title

    return _notion_request("POST", "/databases", body=body)


def add_comment(
    parent_id: str,
    rich_text_json: str,
    discussion_id: Optional[str] = None,
) -> dict:
    """Add a comment to a page or discussion thread.
    parent_id: page ID (used when starting a new discussion).
    rich_text_json: JSON array of rich text objects for the comment body.
    discussion_id: optional, to reply to an existing discussion thread (overrides parent_id).
    """
    rt, err = _parse_json_param(rich_text_json, "rich_text_json")
    if err:
        return err
    if not rt:
        return {"error": "rich_text_json is required"}

    body: dict[str, Any] = {"rich_text": rt}
    if discussion_id:
        body["discussion_id"] = discussion_id
    else:
        body["parent"] = {"page_id": parent_id}

    return _notion_request("POST", "/comments", body=body)


# ── Admin/Delete tools ───────────────────────────────────────────────────────


def delete_block(block_id: str) -> dict:
    """Delete (archive) a block."""
    return _notion_request("DELETE", f"/blocks/{block_id}")


def archive_page(page_id: str) -> dict:
    """Archive a page (sets archived=true)."""
    return _notion_request("PATCH", f"/pages/{page_id}", body={"archived": True})


# ── Plugin definition ────────────────────────────────────────────────────────


class NotionPlugin(MCPPlugin):
    """Notion API plugin for MCP Gateway."""

    name = "notion"
    tools = {
        "search": ToolDef(
            access="read",
            handler=search,
            description="Search pages and databases by query. Optional filter_type: 'page' or 'database'.",
        ),
        "get_page": ToolDef(
            access="read",
            handler=get_page,
            description="Get a page by ID.",
        ),
        "get_page_content": ToolDef(
            access="read",
            handler=get_page_content,
            description="Get all blocks (content) of a page with pagination.",
        ),
        "get_database": ToolDef(
            access="read",
            handler=get_database,
            description="Get a database schema/metadata by ID.",
        ),
        "query_database": ToolDef(
            access="read",
            handler=query_database,
            description="Query a database with optional filter_json and sorts_json (Notion filter/sort objects as JSON strings).",
        ),
        "get_block": ToolDef(
            access="read",
            handler=get_block,
            description="Get a block by ID.",
        ),
        "get_block_children": ToolDef(
            access="read",
            handler=get_block_children,
            description="Get children blocks of a block or page.",
        ),
        "list_users": ToolDef(
            access="read",
            handler=list_users,
            description="List all users in the workspace.",
        ),
        "get_user": ToolDef(
            access="read",
            handler=get_user,
            description="Get a user by ID.",
        ),
        "list_comments": ToolDef(
            access="read",
            handler=list_comments,
            description="List comments on a page or block. Pass block_id to scope.",
        ),
        "create_page": ToolDef(
            access="write",
            handler=create_page,
            description="Create a page. parent_type: 'database_id' or 'page_id'. properties_json: page properties as JSON string. children_json: optional content blocks.",
        ),
        "update_page": ToolDef(
            access="write",
            handler=update_page,
            description="Update page properties. properties_json: properties to update as JSON string.",
        ),
        "append_blocks": ToolDef(
            access="write",
            handler=append_blocks,
            description="Append block children to a page or block. children_json: JSON array of Notion block objects.",
        ),
        "update_block": ToolDef(
            access="write",
            handler=update_block,
            description="Update a block. block_json: JSON string of the block update payload.",
        ),
        "create_database": ToolDef(
            access="write",
            handler=create_database,
            description="Create a database as a child of a page. title_json: rich text array. properties_json: schema definition.",
        ),
        "add_comment": ToolDef(
            access="write",
            handler=add_comment,
            description="Add a comment to a page or discussion. rich_text_json: JSON array of rich text objects. discussion_id: optional, to reply to a thread.",
        ),
        "delete_block": ToolDef(
            access="admin",
            handler=delete_block,
            description="Delete (archive) a block by ID.",
        ),
        "archive_page": ToolDef(
            access="admin",
            handler=archive_page,
            description="Archive a page (sets archived=true).",
        ),
    }

    def health_check(self) -> dict:
        creds = get_credentials("notion")
        api_key = creds.get("api_key", "")
        if not api_key:
            return {"status": "error", "message": "No API key configured"}
        data = _notion_request("GET", "/users/me")
        if "error" in data:
            return {"status": "error", "message": data.get("error", "Unknown error")}
        return {"status": "ok", "bot": data.get("name", data.get("id", "authenticated"))}
