"""Calendly MCP Gateway plugin.

Exposes the full Calendly API as tools. Supports multiple accounts via the
`account` parameter (e.g. "personal", "work").

Credentials from get_credentials("calendly"):
  Single account:  {"api_key": "..."}
  Multi-account:   {"personal.api_key": "...", "work.api_key": "..."}
"""


import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials


_CALENDLY_BASE = "https://api.calendly.com"

_user_uri_cache: dict[str, str] = {}
_org_uri_cache: dict[str, str] = {}


def _list_calendly_accounts() -> list[str]:
    try:
        creds = get_credentials("calendly")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve_api_key(account: str = "") -> tuple[str, str | None]:
    """Return (api_key, error_message). Resolves multi-account credentials."""
    creds = get_credentials("calendly")

    selected = account
    if not selected:
        available = _list_calendly_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return "", f"Multiple Calendly accounts configured: {available}. You MUST specify the `account` parameter."
        else:
            return "", "No Calendly credentials configured for this key."

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return "", f"No Calendly credentials found for account '{selected}'. Available: {_list_calendly_accounts()}"

    return api_key, None


def _calendly_request(
    method: str, path: str, params: dict | None = None, body: dict | None = None,
    account: str = "",
) -> dict:
    """Make an authenticated request to the Calendly API."""
    api_key, err = _resolve_api_key(account)
    if err:
        return {"error": err}

    url = path if path.startswith("http") else f"{_CALENDLY_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req_kwargs: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers,
        "timeout": 30.0,
    }
    if params:
        req_kwargs["params"] = {k: v for k, v in params.items() if v is not None}
    if body:
        req_kwargs["json"] = body

    try:
        resp = httpx.request(**req_kwargs)
    except httpx.TimeoutException:
        return {"error": "Request timed out after 30s"}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code >= 400:
        try:
            error_body = resp.json()
        except Exception:
            error_body = resp.text
        return {"error": f"HTTP {resp.status_code}", "details": error_body}

    text = resp.text
    if not text:
        return {"success": True}
    try:
        return resp.json()
    except Exception:
        return {"data": text}


def _ensure_user_context(account: str = "") -> tuple[str, str]:
    """Return (user_uri, org_uri), fetching from /users/me if not already cached."""
    api_key, err = _resolve_api_key(account)
    if err or not api_key:
        return "", ""

    if api_key in _user_uri_cache:
        return _user_uri_cache[api_key], _org_uri_cache.get(api_key, "")

    try:
        resp = httpx.get(
            f"{_CALENDLY_BASE}/users/me",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json().get("resource", {})
            user_uri = data.get("uri", "")
            org_uri = data.get("current_organization", "")
            _user_uri_cache[api_key] = user_uri
            _org_uri_cache[api_key] = org_uri
            return user_uri, org_uri
    except Exception:
        pass
    return "", ""


# ── Tool handlers ─────────────────────────────────────────────────────────────

def get_oauth_url(redirect_uri: str, state: Optional[str] = None, account: Optional[str] = None) -> dict:
    """Generate a Calendly OAuth authorization URL. Use this to start the OAuth flow when building an app that authenticates users via Calendly OAuth 2.0. Returns a URL the user should visit to grant access. Requires CALENDLY_CLIENT_ID to be set."""
    api_key, err = _resolve_api_key(account or "")
    if err:
        return {"error": err}
    params: dict[str, str] = {"client_id": "", "response_type": "code", "redirect_uri": redirect_uri}
    if state:
        params["state"] = state
    return {"oauth_url": f"https://auth.calendly.com/oauth/authorize?{'&'.join(f'{k}={v}' for k, v in params.items())}"}


def exchange_code_for_tokens(code: str, redirect_uri: str, account: Optional[str] = None) -> dict:
    """Exchange a Calendly OAuth authorization code for access and refresh tokens. Call this after the user completes the OAuth flow and you receive the authorization code. Requires CALENDLY_CLIENT_ID and CALENDLY_CLIENT_SECRET."""
    return _calendly_request(
        "POST",
        "https://auth.calendly.com/oauth/token",
        body={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        account=account or "",
    )


def refresh_access_token(refresh_token: str, account: Optional[str] = None) -> dict:
    """Refresh an expired Calendly OAuth access token using a refresh token. Use when API calls return 401 Unauthorized due to token expiration."""
    return _calendly_request(
        "POST",
        "https://auth.calendly.com/oauth/token",
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        account=account or "",
    )


def get_current_user(account: Optional[str] = None) -> dict:
    """Get the authenticated Calendly user's profile — name, email, timezone, scheduling URL, avatar, and their user/organization URIs. Call this first to verify the connection and retrieve URIs needed by other tools."""
    return _calendly_request("GET", "/users/me", account=account or "")


def list_events(
    user_uri: Optional[str] = None,
    organization_uri: Optional[str] = None,
    status: Optional[str] = None,
    min_start_time: Optional[str] = None,
    max_start_time: Optional[str] = None,
    count: Optional[int] = None,
    account: Optional[str] = None,
) -> dict:
    """List scheduled Calendly events (meetings). Returns event name, start/end times, status, location, and event URI. Supports filtering by date range (ISO 8601) and status ('active' or 'canceled'). user_uri and organization_uri are auto-filled from your authenticated account if not provided."""
    auto_user, auto_org = _ensure_user_context(account or "")
    params = {
        "user": user_uri or auto_user or None,
        "organization": organization_uri or auto_org or None,
        "status": status,
        "min_start_time": min_start_time,
        "max_start_time": max_start_time,
        "count": str(count) if count else None,
    }
    return _calendly_request("GET", "/scheduled_events", params=params, account=account or "")


def get_event(event_uuid: str, account: Optional[str] = None) -> dict:
    """Get full details of a single Calendly event by its UUID. Returns event name, description, start/end time, location details, conferencing info, and invitee count. Use list_events first to find event UUIDs."""
    return _calendly_request("GET", f"/scheduled_events/{event_uuid}", account=account or "")


def list_event_invitees(
    event_uuid: str,
    status: Optional[str] = None,
    email: Optional[str] = None,
    count: Optional[int] = None,
    account: Optional[str] = None,
) -> dict:
    """List invitees for a specific Calendly event. Returns each invitee's name, email, status, timezone, and booking form answers. Useful for seeing who booked a meeting."""
    params = {
        "status": status,
        "email": email,
        "count": str(count) if count else None,
    }
    return _calendly_request("GET", f"/scheduled_events/{event_uuid}/invitees", params=params, account=account or "")


def cancel_event(event_uuid: str, reason: Optional[str] = None, account: Optional[str] = None) -> dict:
    """Cancel a scheduled Calendly event. WARNING: irreversible — the event cannot be rescheduled via API, only cancelled. Notifies all invitees. Always confirm with the user before calling."""
    body = {"reason": reason or "Canceled via API"}
    return _calendly_request("POST", f"/scheduled_events/{event_uuid}/cancellation", body=body, account=account or "")


def list_organization_memberships(
    user_uri: Optional[str] = None,
    organization_uri: Optional[str] = None,
    email: Optional[str] = None,
    count: Optional[int] = None,
    account: Optional[str] = None,
) -> dict:
    """List Calendly organization memberships. Returns member URIs, roles, and user details. Useful for discovering team members."""
    auto_user, auto_org = _ensure_user_context(account or "")
    params = {
        "user": user_uri or auto_user or None,
        "organization": organization_uri or auto_org or None,
        "email": email,
        "count": str(count) if count else None,
    }
    return _calendly_request("GET", "/organization_memberships", params=params, account=account or "")


def list_event_types(
    user: Optional[str] = None,
    organization: Optional[str] = None,
    count: Optional[int] = None,
    account: Optional[str] = None,
) -> dict:
    """List available Calendly event types (meeting templates) — e.g. '30 Minute Meeting', 'Sales Demo'. Returns each event type's name, duration, scheduling URL, and URI. You MUST call this before get_event_type_availability or schedule_event, because those require an event_type URI from this tool's output. user and organization are auto-filled from your authenticated account if not provided."""
    auto_user, auto_org = _ensure_user_context(account or "")
    params = {
        "user": user or auto_user or None,
        "organization": organization or auto_org or None,
        "count": str(count) if count else None,
    }
    return _calendly_request("GET", "/event_types", params=params, account=account or "")


def get_event_type_availability(
    event_type: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    account: Optional[str] = None,
) -> dict:
    """Check available time slots for a specific Calendly event type within a date range. Returns bookable start times. Use when asked 'when are you free?', 'what slots are available?', or before scheduling. Requires an event_type URI — call list_event_types first."""
    params = {
        "event_type": event_type,
        "start_time": start_time,
        "end_time": end_time,
    }
    return _calendly_request("GET", "/event_type_available_times", params=params, account=account or "")


def schedule_event(
    event_type: str,
    start_time: str,
    invitee_email: str,
    invitee_timezone: str,
    invitee_name: Optional[str] = None,
    invitee_first_name: Optional[str] = None,
    invitee_last_name: Optional[str] = None,
    invitee_phone: Optional[str] = None,
    location_kind: Optional[str] = None,
    location_details: Optional[str] = None,
    event_guests: Optional[list | str] = None,
    questions_and_answers: Optional[list | str] = None,
    utm_source: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    utm_medium: Optional[str] = None,
    account: Optional[str] = None,
) -> dict:
    """Book a meeting on Calendly by creating an invitee for a specific event type and time slot. Creates the event, syncs calendars, sends confirmation emails. Requires a paid Calendly plan (Standard+). Workflow: (1) list_event_types, (2) get_event_type_availability, (3) schedule_event. Always confirm the time with the user before booking. start_time must be ISO 8601 UTC (e.g. '2026-03-12T18:30:00Z'). invitee_timezone is IANA format (e.g. 'America/New_York'). If the event type has required custom questions, you MUST pass questions_and_answers. To find the required questions, check a past invitee's questions_and_answers using list_event_invitees on a previous event of that type. Pass questions_and_answers as a JSON string array of objects, each with 'question' (exact text), 'answer', and 'position' (0-indexed). Example: '[{"question":"What is your brand?","answer":"Acme Co","position":0}]'."""
    body: dict[str, Any] = {
        "event_type": event_type,
        "start_time": start_time,
        "invitee": {
            "email": invitee_email,
            "timezone": invitee_timezone,
        },
    }

    if invitee_name:
        body["invitee"]["name"] = invitee_name
    elif invitee_first_name or invitee_last_name:
        if invitee_first_name:
            body["invitee"]["first_name"] = invitee_first_name
        if invitee_last_name:
            body["invitee"]["last_name"] = invitee_last_name

    if invitee_phone:
        body["invitee"]["text_reminder_number"] = invitee_phone

    if location_kind:
        body["location"] = {"kind": location_kind}
        if location_details:
            body["location"]["location"] = location_details

    if event_guests:
        try:
            guests = json.loads(event_guests) if isinstance(event_guests, str) else event_guests
            body["event_guests"] = guests
        except (json.JSONDecodeError, ValueError):
            body["event_guests"] = [event_guests]

    if questions_and_answers:
        if isinstance(questions_and_answers, list):
            body["questions_and_answers"] = questions_and_answers
        elif isinstance(questions_and_answers, str):
            try:
                body["questions_and_answers"] = json.loads(questions_and_answers)
            except (json.JSONDecodeError, ValueError):
                pass

    if utm_source or utm_campaign or utm_medium:
        body["tracking"] = {
            "utm_source": utm_source,
            "utm_campaign": utm_campaign,
            "utm_medium": utm_medium,
        }

    return _calendly_request("POST", "/invitees", body=body, account=account or "")


# ── Plugin definition ─────────────────────────────────────────────────────────

class CalendlyPlugin(MCPPlugin):
    """Calendly API plugin for MCP Gateway."""
    name = "calendly"
    tools = {
        "get_oauth_url": ToolDef(
            access="admin",
            handler=get_oauth_url,
            description="Generate a Calendly OAuth authorization URL.",
        ),
        "exchange_code_for_tokens": ToolDef(
            access="admin",
            handler=exchange_code_for_tokens,
            description="Exchange a Calendly OAuth authorization code for tokens.",
        ),
        "refresh_access_token": ToolDef(
            access="admin",
            handler=refresh_access_token,
            description="Refresh an expired Calendly OAuth access token.",
        ),
        "get_current_user": ToolDef(
            access="read",
            handler=get_current_user,
            description="Get the authenticated Calendly user's profile. Pass account for multi-account setups.",
        ),
        "list_events": ToolDef(
            access="read",
            handler=list_events,
            description="List scheduled Calendly events. Supports date range and status filtering. Pass account for multi-account setups.",
        ),
        "get_event": ToolDef(
            access="read",
            handler=get_event,
            description="Get full details of a single Calendly event by UUID.",
        ),
        "list_event_invitees": ToolDef(
            access="read",
            handler=list_event_invitees,
            description="List invitees for a Calendly event. Returns name, email, status, answers.",
        ),
        "list_organization_memberships": ToolDef(
            access="read",
            handler=list_organization_memberships,
            description="List Calendly organization memberships.",
        ),
        "list_event_types": ToolDef(
            access="read",
            handler=list_event_types,
            description="List available Calendly event types (meeting templates). Pass account for multi-account setups.",
        ),
        "get_event_type_availability": ToolDef(
            access="read",
            handler=get_event_type_availability,
            description="Check available time slots for a Calendly event type.",
        ),
        "cancel_event": ToolDef(
            access="write",
            handler=cancel_event,
            description="Cancel a scheduled Calendly event. Irreversible.",
        ),
        "schedule_event": ToolDef(
            access="write",
            handler=schedule_event,
            description="Book a meeting on Calendly. Workflow: list_event_types → get_event_type_availability → schedule_event. Pass account for multi-account setups.",
        ),
    }
