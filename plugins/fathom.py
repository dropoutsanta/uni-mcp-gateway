"""Fathom (AI notetaker) plugin for the MCP Gateway.

List meetings with transcripts/summaries/action items, get recording details,
manage teams/members, and configure webhooks. Multi-account via {account}.api_key.
"""

from __future__ import annotations

from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE_URL = "https://api.fathom.ai/external/v1"


def _list_fathom_accounts() -> list[str]:
    try:
        creds = get_credentials("fathom")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _get_fathom_key(account: str = "") -> dict:
    try:
        creds = get_credentials("fathom")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_fathom_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Fathom accounts configured. Specify the `account` parameter.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Fathom credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No Fathom credentials found for account '{selected}'.",
            "available_accounts": _list_fathom_accounts(),
        }

    return {"account": selected, "api_key": api_key}


def _fathom_request(method: str, path: str, api_key: str, *,
                    params: dict | None = None,
                    json_body: dict | None = None,
                    timeout: float = 30.0) -> dict:
    headers = {"X-Api-Key": api_key}
    try:
        resp = httpx.request(
            method, f"{_BASE_URL}{path}",
            headers=headers, params=params, json=json_body,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return {"error": f"Request timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 204:
        return {"success": True}
    if resp.status_code == 429:
        return {"error": "Rate limited. Wait and retry."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}

    try:
        return resp.json()
    except Exception:
        return {"data": resp.text}


class FathomPlugin(MCPPlugin):
    name = "fathom"

    def __init__(self):
        self.tools = {
            "list_meetings": ToolDef(
                access="read",
                handler=self.list_meetings,
                description=(
                    "List recorded meetings with optional filters. Supports cursor-based pagination.\n\n"
                    "Filters: created_after/created_before (ISO timestamps), "
                    "calendar_invitees_domains (comma-separated domains), "
                    "calendar_invitees_domains_type (all|only_internal|one_or_more_external), "
                    "recorded_by (comma-separated emails), teams (comma-separated team names).\n\n"
                    "Set include_transcript, include_summary, include_action_items, "
                    "include_crm_matches to true to embed that data in results."
                ),
            ),
            "get_summary": ToolDef(
                access="read",
                handler=self.get_summary,
                description=(
                    "Get the AI-generated summary for a specific recording. "
                    "Pass the recording_id from list_meetings results."
                ),
            ),
            "get_transcript": ToolDef(
                access="read",
                handler=self.get_transcript,
                description=(
                    "Get the full transcript for a specific recording. "
                    "Returns speaker names, text, and timestamps. "
                    "Pass the recording_id from list_meetings results."
                ),
            ),
            "list_teams": ToolDef(
                access="read",
                handler=self.list_teams,
                description="List all teams in the Fathom workspace.",
            ),
            "list_team_members": ToolDef(
                access="read",
                handler=self.list_team_members,
                description="List team members, optionally filtered by team name.",
            ),
            "create_webhook": ToolDef(
                access="write",
                handler=self.create_webhook,
                description=(
                    "Create a webhook to receive new meeting content.\n\n"
                    "Required: destination_url, triggered_for (comma-separated from: "
                    "my_recordings, shared_external_recordings, "
                    "my_shared_with_team_recordings, shared_team_recordings).\n\n"
                    "Optional booleans: include_transcript, include_summary, "
                    "include_action_items, include_crm_matches. "
                    "At least one include_* must be true."
                ),
            ),
            "delete_webhook": ToolDef(
                access="write",
                handler=self.delete_webhook,
                description="Delete a webhook by its ID.",
            ),
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts,
                description="List all configured Fathom accounts for the current key.",
            ),
        }

    def list_meetings(
        self,
        account: str = "",
        cursor: str = "",
        created_after: str = "",
        created_before: str = "",
        calendar_invitees_domains: str = "",
        calendar_invitees_domains_type: str = "",
        recorded_by: str = "",
        teams: str = "",
        include_transcript: bool = False,
        include_summary: bool = False,
        include_action_items: bool = False,
        include_crm_matches: bool = False,
    ) -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if created_after:
            params["created_after"] = created_after
        if created_before:
            params["created_before"] = created_before
        if calendar_invitees_domains_type:
            params["calendar_invitees_domains_type"] = calendar_invitees_domains_type
        if include_transcript:
            params["include_transcript"] = "true"
        if include_summary:
            params["include_summary"] = "true"
        if include_action_items:
            params["include_action_items"] = "true"
        if include_crm_matches:
            params["include_crm_matches"] = "true"

        if calendar_invitees_domains:
            for d in calendar_invitees_domains.split(","):
                d = d.strip()
                if d:
                    params.setdefault("calendar_invitees_domains[]", [])
                    if isinstance(params.get("calendar_invitees_domains[]"), list):
                        params["calendar_invitees_domains[]"].append(d)
        if recorded_by:
            for e in recorded_by.split(","):
                e = e.strip()
                if e:
                    params.setdefault("recorded_by[]", [])
                    if isinstance(params.get("recorded_by[]"), list):
                        params["recorded_by[]"].append(e)
        if teams:
            for t in teams.split(","):
                t = t.strip()
                if t:
                    params.setdefault("teams[]", [])
                    if isinstance(params.get("teams[]"), list):
                        params["teams[]"].append(t)

        return _fathom_request("GET", "/meetings", resolved["api_key"],
                               params=params, timeout=30.0)

    def get_summary(self, recording_id: str, account: str = "") -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved
        return _fathom_request("GET", f"/recordings/{recording_id}/summary",
                               resolved["api_key"])

    def get_transcript(self, recording_id: str, account: str = "") -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved
        return _fathom_request("GET", f"/recordings/{recording_id}/transcript",
                               resolved["api_key"])

    def list_teams(self, cursor: str = "", account: str = "") -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved
        params = {}
        if cursor:
            params["cursor"] = cursor
        return _fathom_request("GET", "/teams", resolved["api_key"], params=params)

    def list_team_members(self, team: str = "", cursor: str = "",
                          account: str = "") -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved
        params: dict[str, str] = {}
        if team:
            params["team"] = team
        if cursor:
            params["cursor"] = cursor
        return _fathom_request("GET", "/team_members", resolved["api_key"],
                               params=params)

    def create_webhook(
        self,
        destination_url: str,
        triggered_for: str = "my_recordings",
        include_transcript: bool = True,
        include_summary: bool = True,
        include_action_items: bool = True,
        include_crm_matches: bool = False,
        account: str = "",
    ) -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved

        triggers = [t.strip() for t in triggered_for.split(",") if t.strip()]
        body = {
            "destination_url": destination_url,
            "triggered_for": triggers,
            "include_transcript": include_transcript,
            "include_summary": include_summary,
            "include_action_items": include_action_items,
            "include_crm_matches": include_crm_matches,
        }
        return _fathom_request("POST", "/webhooks", resolved["api_key"],
                               json_body=body)

    def delete_webhook(self, webhook_id: str, account: str = "") -> dict:
        resolved = _get_fathom_key(account)
        if "error" in resolved:
            return resolved
        return _fathom_request("DELETE", f"/webhooks/{webhook_id}",
                               resolved["api_key"])

    def list_accounts(self) -> dict:
        return {"accounts": _list_fathom_accounts()}

    def health_check(self) -> dict[str, Any]:
        try:
            resolved = _get_fathom_key()
            if "error" in resolved:
                return {"status": "no_credentials", "detail": resolved["error"]}
            resp = httpx.get(
                f"{_BASE_URL}/teams",
                headers={"X-Api-Key": resolved["api_key"]},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
