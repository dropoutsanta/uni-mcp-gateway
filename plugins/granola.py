"""Granola (AI meeting notetaker) plugin for the MCP Gateway.

Read meeting notes, transcripts, summaries, and attendees.
Multi-account via {account}.api_key, with optional folder-level scoping
via key_plugin_scopes (granola: allowed_folders = comma-separated folder IDs).

When folder scoping is active, list_notes enriches each note with folder data
and filters to only return notes in allowed folders. get_note also enforces
the scope check before returning.
"""

from __future__ import annotations

from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials, get_data_scopes

_BASE = "https://public-api.granola.ai/v1"


def _list_granola_accounts() -> list[str]:
    try:
        creds = get_credentials("granola")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str = "") -> dict:
    try:
        creds = get_credentials("granola")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_granola_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Granola accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Granola credentials configured for this key."}

    key_name = "api_key" if selected == "default" else f"{selected}.api_key"
    api_key = creds.get(key_name, "")
    if not api_key:
        return {
            "error": f"No Granola credentials for account '{selected}'.",
            "available_accounts": _list_granola_accounts(),
        }
    return {"account": selected, "api_key": api_key}


def _req(method: str, path: str, token: str, *,
         params: dict | None = None, timeout: float = 30.0) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.request(method, f"{_BASE}{path}",
                             headers=headers, params=params, timeout=timeout)
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 429:
        return {"error": "Rate limited (25 burst / 5 per sec). Wait and retry."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}
    try:
        return resp.json()
    except Exception:
        return {"data": resp.text}


def _get_allowed_folders() -> set[str] | None:
    """Return set of allowed folder IDs from data scopes, or None if unrestricted."""
    return get_data_scopes("granola")


def _note_in_allowed_folders(note_detail: dict, allowed: set[str]) -> bool:
    """Check if a note belongs to any of the allowed folders."""
    memberships = note_detail.get("folder_membership", [])
    if not memberships:
        return False
    for folder in memberships:
        if folder.get("id") in allowed:
            return True
    return False


class GranolaPlugin(MCPPlugin):
    name = "granola"

    def __init__(self):
        self.tools = {
            "list_notes": ToolDef(
                access="read",
                handler=self.list_notes,
                description=(
                    "List meeting notes with pagination. Returns title, owner, timestamps.\n\n"
                    "Filters: created_after, created_before, updated_after (ISO date or datetime).\n"
                    "Pagination: page_size (1-30, default 10), cursor (from previous response).\n\n"
                    "If folder scoping is configured for this key, notes are automatically "
                    "filtered to only those in allowed folders."
                ),
            ),
            "get_note": ToolDef(
                access="read",
                handler=self.get_note,
                description=(
                    "Get a single note by ID (not_XXXXXXXXXXXXXX format).\n"
                    "Returns: title, owner, summary (text + markdown), attendees, "
                    "calendar event details, folder membership.\n"
                    "Set include_transcript=true to also get the full transcript with "
                    "speaker labels and timestamps."
                ),
            ),
            "search_notes": ToolDef(
                access="read",
                handler=self.search_notes,
                description=(
                    "Search notes by keyword in titles. Fetches recent notes and filters "
                    "by title match. Optional: query (search term), created_after, page_size.\n"
                    "For more precise searching, use list_notes with date filters and "
                    "inspect individual notes with get_note."
                ),
            ),
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts,
                description="List configured Granola accounts for the current key.",
            ),
        }

    def list_notes(
        self,
        account: str = "",
        created_after: str = "",
        created_before: str = "",
        updated_after: str = "",
        page_size: int = 10,
        cursor: str = "",
    ) -> dict:
        r = _resolve(account)
        if "error" in r:
            return r

        params: dict[str, Any] = {}
        if created_after:
            params["created_after"] = created_after
        if created_before:
            params["created_before"] = created_before
        if updated_after:
            params["updated_after"] = updated_after
        if page_size:
            params["page_size"] = min(page_size, 30)
        if cursor:
            params["cursor"] = cursor

        result = _req("GET", "/notes", r["api_key"], params=params)
        if "error" in result:
            return result

        allowed = _get_allowed_folders()
        if allowed is None:
            return result

        notes = result.get("notes", [])
        filtered = []
        for note_summary in notes:
            note_id = note_summary.get("id")
            if not note_id:
                continue
            detail = _req("GET", f"/notes/{note_id}", r["api_key"])
            if "error" in detail:
                continue
            if _note_in_allowed_folders(detail, allowed):
                filtered.append(note_summary)

        result["notes"] = filtered
        result["_scope"] = f"Filtered to folders: {', '.join(allowed)}"
        return result

    def get_note(
        self,
        note_id: str,
        include_transcript: bool = False,
        account: str = "",
    ) -> dict:
        r = _resolve(account)
        if "error" in r:
            return r

        params: dict[str, str] = {}
        if include_transcript:
            params["include"] = "transcript"

        result = _req("GET", f"/notes/{note_id}", r["api_key"], params=params)
        if "error" in result:
            return result

        allowed = _get_allowed_folders()
        if allowed is not None:
            if not _note_in_allowed_folders(result, allowed):
                return {"error": "Access denied: note is not in your allowed folders."}

        return result

    def search_notes(
        self,
        query: str = "",
        account: str = "",
        created_after: str = "",
        page_size: int = 30,
    ) -> dict:
        r = _resolve(account)
        if "error" in r:
            return r

        params: dict[str, Any] = {"page_size": min(page_size, 30)}
        if created_after:
            params["created_after"] = created_after

        result = _req("GET", "/notes", r["api_key"], params=params)
        if "error" in result:
            return result

        notes = result.get("notes", [])
        allowed = _get_allowed_folders()

        if query:
            q = query.lower()
            notes = [n for n in notes if q in (n.get("title") or "").lower()]

        if allowed is not None:
            filtered = []
            for note_summary in notes:
                note_id = note_summary.get("id")
                if not note_id:
                    continue
                detail = _req("GET", f"/notes/{note_id}", r["api_key"])
                if "error" in detail:
                    continue
                if _note_in_allowed_folders(detail, allowed):
                    filtered.append(note_summary)
            notes = filtered

        return {"notes": notes, "total": len(notes)}

    def list_accounts(self) -> dict:
        return {"accounts": _list_granola_accounts()}

    def health_check(self) -> dict[str, Any]:
        try:
            r = _resolve()
            if "error" in r:
                return {"status": "no_credentials", "detail": r["error"]}
            resp = httpx.get(
                f"{_BASE}/notes",
                headers={"Authorization": f"Bearer {r['api_key']}"},
                params={"page_size": 1},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
