"""Bison plugin for MCP Gateway.

Pure HTTP proxy to the EmailBison REST API. Credentials are per-request via
get_credentials("bison") which returns {"api_key": "...", "base_url": "..."}.
"""


import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials


# ─── COMMANDS definition (from bison-mcp/commands.py) ──────────────────────────

COMMANDS: list[dict] = []


def cmd(name: str, desc: str, method: str, path: str, params=None, mappings=None):
    COMMANDS.append({
        "name": name,
        "description": desc,
        "method": method,
        "path": path,
        "params": params or [],
        "field_mappings": mappings or {},
    })


def p(name: str, t: str = "str", req: bool = True, d: str = ""):
    return {"name": name, "type": t, "required": req, "description": d}


# Admin-level tools (user/workspace management)
ADMIN_TOOLS = frozenset({
    "users_update_password",
    "users_headless_ui_token",
    "users_update_profile_picture",
    "workspaces_create",
    "workspaces_switch",
    "workspaces_invite_members",
    "workspaces_remove_member",
    "workspaces_accept_invite",
    "workspaces_update_member",
    "workspaces_v11_create",
    "workspaces_v11_switch",
    "workspaces_v11_invite_members",
    "workspaces_v11_remove_member",
    "workspaces_v11_create_user",
    "workspaces_v11_create_api_token",
    "workspaces_v11_delete",
    "workspaces_v11_update",
    "workspaces_v11_accept_invite",
})


def _list_bison_accounts() -> list[str]:
    """Return the list of configured Bison account names for the current key."""
    try:
        creds = get_credentials("bison")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _get_bison_account_credentials(account: str = "") -> dict:
    """Resolve the selected Bison account and return its credentials."""
    try:
        creds = get_credentials("bison")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_bison_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Bison accounts configured. You MUST specify the `account` parameter.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Bison credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
        base_url = creds.get("base_url", "https://send.topoffunnel.com")
    else:
        api_key = creds.get(f"{selected}.api_key", "")
        base_url = creds.get(f"{selected}.base_url", "https://send.topoffunnel.com")

    if not api_key:
        return {
            "error": f"No Bison credentials found for account '{selected}'.",
            "available_accounts": _list_bison_accounts(),
        }

    return {
        "account": selected,
        "api_key": api_key,
        "base_url": base_url,
    }


def _bison_request(method: str, path: str, params: dict, field_mappings: dict, account: str = "") -> dict:
    """Make an HTTP request to the Bison API using per-request credentials.
    
    When multiple accounts are configured, pass `account` to select which one.
    """
    resolved = _get_bison_account_credentials(account)
    if "error" in resolved:
        return resolved
    account = resolved["account"]
    api_key = resolved["api_key"]
    base_url = resolved["base_url"]

    url_path = path
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}

    for field, location in field_mappings.items():
        value = params.get(field)
        if value is None:
            continue
        if location == "path":
            url_path = url_path.replace(f"{{{field}}}", str(value))
        elif location == "query":
            query[field] = value
        elif location == "body":
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, (list, dict)):
                        body[field] = parsed
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
            body[field] = value

    url = f"{base_url.rstrip('/')}{url_path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    req_kwargs: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers,
        "timeout": 30.0,
    }
    if query:
        req_kwargs["params"] = query
    if body:
        headers["Content-Type"] = "application/json"
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


def _access_for_command(cmd_def: dict) -> str:
    """Determine access level for a command: read, write, or admin."""
    name = cmd_def["name"]
    method = cmd_def["method"]

    if name in ADMIN_TOOLS:
        return "admin"
    if method == "GET":
        return "read"
    return "write"


# ─── ACCOUNTS (sender email accounts) ────────────────────────────────────────

cmd("accounts_list", "List all sender email accounts in the workspace.", "GET", "/api/sender-emails",
    [p("page", "int", False, "Page number for pagination")],
    {"page": "query"})

cmd("accounts_get", "Get a sender email account by ID.", "GET", "/api/sender-emails/{senderEmailId}",
    [p("senderEmailId", d="Sender email account ID")],
    {"senderEmailId": "path"})

cmd("accounts_update", "Update a sender email account (name, daily limit, signature).", "PATCH", "/api/sender-emails/{senderEmailId}",
    [p("senderEmailId", d="Sender email account ID"),
     p("from_name", req=False, d="Display name for the sender"),
     p("daily_limit", "int", False, "Maximum emails per day"),
     p("signature", req=False, d="HTML email signature")],
    {"senderEmailId": "path", "from_name": "body", "daily_limit": "body", "signature": "body"})

cmd("accounts_delete", "Delete a sender email account.", "DELETE", "/api/sender-emails/{senderEmailId}",
    [p("senderEmailId", d="Sender email account ID")],
    {"senderEmailId": "path"})

cmd("accounts_create_imap_smtp", "Create a sender email account via IMAP/SMTP credentials.", "POST", "/api/sender-emails/imap-smtp",
    [p("email", d="Email address"),
     p("from_name", d="Display name"),
     p("imap_host", d="IMAP server hostname"),
     p("imap_port", "int", True, "IMAP server port"),
     p("imap_username", d="IMAP username"),
     p("imap_password", d="IMAP password"),
     p("smtp_host", d="SMTP server hostname"),
     p("smtp_port", "int", True, "SMTP server port"),
     p("smtp_username", d="SMTP username"),
     p("smtp_password", d="SMTP password")],
    {"email": "body", "from_name": "body", "imap_host": "body", "imap_port": "body",
     "imap_username": "body", "imap_password": "body", "smtp_host": "body", "smtp_port": "body",
     "smtp_username": "body", "smtp_password": "body"})

cmd("accounts_bulk_create", "Bulk create sender email accounts from a JSON array.", "POST", "/api/sender-emails/bulk",
    [p("sender_emails", d="JSON array of sender email objects")],
    {"sender_emails": "body"})

cmd("accounts_campaigns", "List campaigns attached to a sender email account.", "GET", "/api/sender-emails/{senderEmailId}/campaigns",
    [p("senderEmailId", d="Sender email account ID"),
     p("page", "int", False, "Page number")],
    {"senderEmailId": "path", "page": "query"})

cmd("accounts_replies", "List replies for a sender email account.", "GET", "/api/sender-emails/{senderEmailId}/replies",
    [p("senderEmailId", d="Sender email account ID"),
     p("page", "int", False, "Page number")],
    {"senderEmailId": "path", "page": "query"})

cmd("accounts_oauth_token", "Get OAuth access token for a sender email account.", "GET", "/api/sender-emails/{senderEmailId}/oauth-access-token",
    [p("senderEmailId", d="Sender email account ID")],
    {"senderEmailId": "path"})

cmd("accounts_check_mx", "Check MX records for a sender email account.", "POST", "/api/sender-emails/{senderEmailId}/check-mx-records",
    [p("senderEmailId", d="Sender email account ID")],
    {"senderEmailId": "path"})

cmd("accounts_bulk_check_mx", "Bulk check missing MX records for all sender emails.", "POST", "/api/sender-emails/bulk-check-missing-mx-records",
    [], {})

cmd("accounts_bulk_daily_limits", "Bulk update daily sending limits for multiple sender emails.", "PATCH", "/api/sender-emails/daily-limits/bulk",
    [p("sender_email_ids", d="JSON array of sender email IDs"),
     p("daily_limit", "int", True, "New daily sending limit")],
    {"sender_email_ids": "body", "daily_limit": "body"})

cmd("accounts_bulk_signatures", "Bulk update signatures for multiple sender emails.", "PATCH", "/api/sender-emails/signatures/bulk",
    [p("sender_email_ids", d="JSON array of sender email IDs"),
     p("signature", d="HTML signature to apply")],
    {"sender_email_ids": "body", "signature": "body"})


# ─── CAMPAIGN EVENTS ─────────────────────────────────────────────────────────

cmd("campaign_events_stats", "Get campaign event statistics with optional filters.", "GET", "/api/campaign-events/stats",
    [p("campaign_id", req=False, d="Filter by campaign ID"),
     p("sender_email_id", req=False, d="Filter by sender email ID"),
     p("start_date", req=False, d="Start date (YYYY-MM-DD)"),
     p("end_date", req=False, d="End date (YYYY-MM-DD)"),
     p("page", "int", False, "Page number")],
    {"campaign_id": "query", "sender_email_id": "query", "start_date": "query", "end_date": "query", "page": "query"})


# ─── CAMPAIGNS ────────────────────────────────────────────────────────────────

cmd("campaigns_list", "List all campaigns in the workspace.", "GET", "/api/campaigns",
    [p("page", "int", False, "Page number for pagination")],
    {"page": "query"})

cmd("campaigns_get", "Get a campaign by ID.", "GET", "/api/campaigns/{id}",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_create", "Create a new campaign.", "POST", "/api/campaigns",
    [p("name", d="Campaign name")],
    {"name": "body"})

cmd("campaigns_update", "Update campaign settings.", "PATCH", "/api/campaigns/{id}/update",
    [p("id", d="Campaign ID"),
     p("max_emails_per_day", "int", False, "Max emails per day"),
     p("max_new_leads_per_day", "int", False, "Max new leads per day"),
     p("plain_text", "bool", False, "Send as plain text"),
     p("open_tracking", "bool", False, "Enable open tracking"),
     p("reputation_building", "bool", False, "Enable reputation building"),
     p("can_unsubscribe", "bool", False, "Include unsubscribe link"),
     p("unsubscribe_text", req=False, d="Custom unsubscribe text")],
    {"id": "path", "max_emails_per_day": "body", "max_new_leads_per_day": "body",
     "plain_text": "body", "open_tracking": "body", "reputation_building": "body",
     "can_unsubscribe": "body", "unsubscribe_text": "body"})

cmd("campaigns_duplicate", "Duplicate a campaign.", "POST", "/api/campaigns/{campaign_id}/duplicate",
    [p("campaign_id", d="Campaign ID to duplicate")],
    {"campaign_id": "path"})

cmd("campaigns_pause", "Pause a running campaign.", "PATCH", "/api/campaigns/{campaign_id}/pause",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_resume", "Resume a paused campaign.", "PATCH", "/api/campaigns/{campaign_id}/resume",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_archive", "Archive a campaign.", "PATCH", "/api/campaigns/{campaign_id}/archive",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_delete", "Delete a campaign.", "DELETE", "/api/campaigns/{campaign_id}",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_bulk_delete", "Delete multiple campaigns.", "DELETE", "/api/campaigns/bulk",
    [p("campaign_ids", d="JSON array of campaign IDs to delete")],
    {"campaign_ids": "body"})

cmd("campaigns_stats", "Get campaign statistics (sent, opened, replied, bounced, etc.).", "POST", "/api/campaigns/{campaign_id}/stats",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_line_area_chart_stats", "Get line/area chart statistics for a campaign.", "GET", "/api/campaigns/{campaign_id}/line-area-chart-stats",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_sender_emails", "List sender email accounts attached to a campaign.", "GET", "/api/campaigns/{campaign_id}/sender-emails",
    [p("campaign_id", d="Campaign ID"),
     p("page", "int", False, "Page number")],
    {"campaign_id": "path", "page": "query"})

cmd("campaigns_attach_sender_emails", "Attach sender email accounts to a campaign.", "POST", "/api/campaigns/{campaign_id}/attach-sender-emails",
    [p("campaign_id", d="Campaign ID"),
     p("sender_email_ids", d="JSON array of sender email IDs")],
    {"campaign_id": "path", "sender_email_ids": "body"})

cmd("campaigns_remove_sender_emails", "Remove sender email accounts from a campaign.", "DELETE", "/api/campaigns/{campaign_id}/remove-sender-emails",
    [p("campaign_id", d="Campaign ID"),
     p("sender_email_ids", d="JSON array of sender email IDs")],
    {"campaign_id": "path", "sender_email_ids": "body"})

cmd("campaigns_leads", "List leads attached to a campaign.", "GET", "/api/campaigns/{campaign_id}/leads",
    [p("campaign_id", d="Campaign ID"),
     p("page", "int", False, "Page number")],
    {"campaign_id": "path", "page": "query"})

cmd("campaigns_attach_leads", "Attach leads to a campaign.", "POST", "/api/campaigns/{campaign_id}/leads/attach-leads",
    [p("campaign_id", d="Campaign ID"),
     p("lead_ids", d="JSON array of lead IDs")],
    {"campaign_id": "path", "lead_ids": "body"})

cmd("campaigns_attach_lead_list", "Attach a lead list to a campaign.", "POST", "/api/campaigns/{campaign_id}/leads/attach-lead-list",
    [p("campaign_id", d="Campaign ID"),
     p("lead_list_id", d="Lead list ID")],
    {"campaign_id": "path", "lead_list_id": "body"})

cmd("campaigns_remove_leads", "Remove leads from a campaign.", "DELETE", "/api/campaigns/{campaign_id}/leads",
    [p("campaign_id", d="Campaign ID"),
     p("lead_ids", d="JSON array of lead IDs")],
    {"campaign_id": "path", "lead_ids": "body"})

cmd("campaigns_move_leads", "Move leads from one campaign to another.", "POST", "/api/campaigns/{campaign_id}/leads/move-to-another-campaign",
    [p("campaign_id", d="Source campaign ID"),
     p("lead_ids", d="JSON array of lead IDs"),
     p("to_campaign_id", d="Destination campaign ID")],
    {"campaign_id": "path", "lead_ids": "body", "to_campaign_id": "body"})

cmd("campaigns_stop_future_emails", "Stop future emails for specific leads in a campaign.", "POST", "/api/campaigns/{campaign_id}/leads/stop-future-emails",
    [p("campaign_id", d="Campaign ID"),
     p("lead_ids", d="JSON array of lead IDs")],
    {"campaign_id": "path", "lead_ids": "body"})

cmd("campaigns_replies", "List replies for a campaign.", "GET", "/api/campaigns/{campaign_id}/replies",
    [p("campaign_id", d="Campaign ID"),
     p("page", "int", False, "Page number")],
    {"campaign_id": "path", "page": "query"})

cmd("campaigns_scheduled_emails", "List scheduled emails for a campaign.", "GET", "/api/campaigns/{campaign_id}/scheduled-emails",
    [p("campaign_id", d="Campaign ID"),
     p("page", "int", False, "Page number")],
    {"campaign_id": "path", "page": "query"})

cmd("campaigns_sequence_steps_list", "List sequence steps for a campaign.", "GET", "/api/campaigns/{campaign_id}/sequence-steps",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_sequence_steps_create", "Create sequence steps for a campaign.", "POST", "/api/campaigns/{campaign_id}/sequence-steps",
    [p("campaign_id", d="Campaign ID"),
     p("title", d="Sequence title"),
     p("sequence_steps", d="JSON array of step objects with email_subject, email_body, wait_in_days, order")],
    {"campaign_id": "path", "title": "body", "sequence_steps": "body"})

cmd("campaigns_sequence_steps_update", "Update a sequence step.", "PUT", "/api/campaigns/sequence-steps/{sequence_id}",
    [p("sequence_id", d="Sequence step ID"),
     p("email_subject", req=False, d="Email subject line"),
     p("email_body", req=False, d="Email body HTML"),
     p("wait_in_days", "int", False, "Days to wait before sending"),
     p("variant", req=False, d="A/B variant label"),
     p("variant_from_step", req=False, d="Step ID to variant from"),
     p("thread_reply", "bool", False, "Send as thread reply")],
    {"sequence_id": "path", "email_subject": "body", "email_body": "body",
     "wait_in_days": "body", "variant": "body", "variant_from_step": "body", "thread_reply": "body"})

cmd("campaigns_sequence_steps_delete", "Delete a sequence step.", "DELETE", "/api/campaigns/sequence-steps/{sequence_step_id}",
    [p("sequence_step_id", d="Sequence step ID")],
    {"sequence_step_id": "path"})

cmd("campaigns_sequence_steps_toggle", "Activate or deactivate a sequence step.", "PATCH", "/api/campaigns/sequence-steps/{sequence_step_id}/activate-or-deactivate",
    [p("sequence_step_id", d="Sequence step ID")],
    {"sequence_step_id": "path"})

cmd("campaigns_sequence_steps_test_email", "Send a test email for a sequence step.", "POST", "/api/campaigns/sequence-steps/{sequence_step_id}/test-email",
    [p("sequence_step_id", d="Sequence step ID"),
     p("to_email", d="Email address to send test to")],
    {"sequence_step_id": "path", "to_email": "body"})

cmd("campaigns_schedule_get", "Get the schedule for a campaign.", "GET", "/api/campaigns/{campaign_id}/schedule",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_schedule_create", "Create a schedule for a campaign.", "POST", "/api/campaigns/{campaign_id}/schedule",
    [p("campaign_id", d="Campaign ID"),
     p("monday", "bool", False, "Send on Mondays"),
     p("tuesday", "bool", False, "Send on Tuesdays"),
     p("wednesday", "bool", False, "Send on Wednesdays"),
     p("thursday", "bool", False, "Send on Thursdays"),
     p("friday", "bool", False, "Send on Fridays"),
     p("saturday", "bool", False, "Send on Saturdays"),
     p("sunday", "bool", False, "Send on Sundays"),
     p("start_time", req=False, d="Start time (HH:MM)"),
     p("end_time", req=False, d="End time (HH:MM)"),
     p("timezone", req=False, d="IANA timezone (e.g. America/New_York)"),
     p("save_as_template", "bool", False, "Save this schedule as a template")],
    {"campaign_id": "path", "monday": "body", "tuesday": "body", "wednesday": "body",
     "thursday": "body", "friday": "body", "saturday": "body", "sunday": "body",
     "start_time": "body", "end_time": "body", "timezone": "body", "save_as_template": "body"})

cmd("campaigns_schedule_update", "Update the schedule for a campaign.", "PUT", "/api/campaigns/{campaign_id}/schedule",
    [p("campaign_id", d="Campaign ID"),
     p("monday", "bool", False, "Send on Mondays"),
     p("tuesday", "bool", False, "Send on Tuesdays"),
     p("wednesday", "bool", False, "Send on Wednesdays"),
     p("thursday", "bool", False, "Send on Thursdays"),
     p("friday", "bool", False, "Send on Fridays"),
     p("saturday", "bool", False, "Send on Saturdays"),
     p("sunday", "bool", False, "Send on Sundays"),
     p("start_time", req=False, d="Start time (HH:MM)"),
     p("end_time", req=False, d="End time (HH:MM)"),
     p("timezone", req=False, d="IANA timezone"),
     p("save_as_template", "bool", False, "Save as template")],
    {"campaign_id": "path", "monday": "body", "tuesday": "body", "wednesday": "body",
     "thursday": "body", "friday": "body", "saturday": "body", "sunday": "body",
     "start_time": "body", "end_time": "body", "timezone": "body", "save_as_template": "body"})

cmd("campaigns_schedule_from_template", "Create a schedule from a saved template.", "POST", "/api/campaigns/{campaign_id}/create-schedule-from-template",
    [p("campaign_id", d="Campaign ID"),
     p("schedule_id", d="Schedule template ID")],
    {"campaign_id": "path", "schedule_id": "body"})

cmd("campaigns_schedule_templates", "List available schedule templates.", "GET", "/api/campaigns/schedule/templates",
    [], {})

cmd("campaigns_schedule_timezones", "List available schedule timezones.", "GET", "/api/campaigns/schedule/available-timezones",
    [], {})

cmd("campaigns_sending_schedules", "List all sending schedules.", "GET", "/api/campaigns/sending-schedules",
    [p("page", "int", False, "Page number")],
    {"page": "query"})


# ─── CAMPAIGNS v1.1 ──────────────────────────────────────────────────────────

cmd("campaigns_v11_sequence_steps_list", "List sequence steps for a campaign (v1.1 API).", "GET", "/api/campaigns/v1.1/{campaign_id}/sequence-steps",
    [p("campaign_id", d="Campaign ID")],
    {"campaign_id": "path"})

cmd("campaigns_v11_sequence_steps_create", "Create sequence steps for a campaign (v1.1 API).", "POST", "/api/campaigns/v1.1/{campaign_id}/sequence-steps",
    [p("campaign_id", d="Campaign ID"),
     p("title", d="Sequence title"),
     p("sequence_steps", d="JSON array of step objects")],
    {"campaign_id": "path", "title": "body", "sequence_steps": "body"})

cmd("campaigns_v11_sequence_steps_update", "Update a sequence step (v1.1 API).", "PUT", "/api/campaigns/v1.1/sequence-steps/{sequence_id}",
    [p("sequence_id", d="Sequence step ID"),
     p("email_subject", req=False, d="Email subject"),
     p("email_body", req=False, d="Email body HTML"),
     p("wait_in_days", "int", False, "Days to wait"),
     p("variant", req=False, d="A/B variant label"),
     p("variant_from_step", req=False, d="Step ID to variant from"),
     p("thread_reply", "bool", False, "Send as thread reply")],
    {"sequence_id": "path", "email_subject": "body", "email_body": "body",
     "wait_in_days": "body", "variant": "body", "variant_from_step": "body", "thread_reply": "body"})


# ─── CUSTOM VARIABLES ────────────────────────────────────────────────────────

cmd("custom_variables_list", "List custom lead variables.", "GET", "/api/custom-variables",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("custom_variables_create", "Create a custom lead variable.", "POST", "/api/custom-variables",
    [p("name", d="Variable name")],
    {"name": "body"})


# ─── DOMAIN BLACKLIST ────────────────────────────────────────────────────────

cmd("domain_blacklist_list", "List blacklisted domains.", "GET", "/api/blacklisted-domains",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("domain_blacklist_get", "Get a blacklisted domain by ID.", "GET", "/api/blacklisted-domains/{blacklisted_domain_id}",
    [p("blacklisted_domain_id", d="Blacklisted domain ID")],
    {"blacklisted_domain_id": "path"})

cmd("domain_blacklist_create", "Add a domain to the blacklist.", "POST", "/api/blacklisted-domains",
    [p("domain", d="Domain to blacklist")],
    {"domain": "body"})

cmd("domain_blacklist_bulk_create", "Bulk add domains to the blacklist.", "POST", "/api/blacklisted-domains/bulk",
    [p("domains", d="JSON array of domain strings")],
    {"domains": "body"})

cmd("domain_blacklist_delete", "Remove a domain from the blacklist.", "DELETE", "/api/blacklisted-domains/{blacklisted_domain_id}",
    [p("blacklisted_domain_id", d="Blacklisted domain ID")],
    {"blacklisted_domain_id": "path"})


# ─── EMAIL BLACKLIST ─────────────────────────────────────────────────────────

cmd("email_blacklist_list", "List blacklisted email addresses.", "GET", "/api/blacklisted-emails",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("email_blacklist_get", "Get a blacklisted email by ID.", "GET", "/api/blacklisted-emails/{blacklisted_email_id}",
    [p("blacklisted_email_id", d="Blacklisted email ID")],
    {"blacklisted_email_id": "path"})

cmd("email_blacklist_create", "Add an email address to the blacklist.", "POST", "/api/blacklisted-emails",
    [p("email", d="Email address to blacklist")],
    {"email": "body"})

cmd("email_blacklist_bulk_create", "Bulk add email addresses to the blacklist.", "POST", "/api/blacklisted-emails/bulk",
    [p("emails", d="JSON array of email address strings")],
    {"emails": "body"})

cmd("email_blacklist_delete", "Remove an email address from the blacklist.", "DELETE", "/api/blacklisted-emails/{blacklisted_email_id}",
    [p("blacklisted_email_id", d="Blacklisted email ID")],
    {"blacklisted_email_id": "path"})


# ─── IGNORE PHRASES ──────────────────────────────────────────────────────────

cmd("ignore_phrases_list", "List ignore phrases (auto-reply filters).", "GET", "/api/ignore-phrases",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("ignore_phrases_get", "Get an ignore phrase by ID.", "GET", "/api/ignore-phrases/{ignore_phrase_id}",
    [p("ignore_phrase_id", d="Ignore phrase ID")],
    {"ignore_phrase_id": "path"})

cmd("ignore_phrases_create", "Create an ignore phrase.", "POST", "/api/ignore-phrases",
    [p("phrase", d="Phrase to ignore in replies")],
    {"phrase": "body"})

cmd("ignore_phrases_delete", "Delete an ignore phrase.", "DELETE", "/api/ignore-phrases/{ignore_phrase_id}",
    [p("ignore_phrase_id", d="Ignore phrase ID")],
    {"ignore_phrase_id": "path"})


# ─── LEADS ────────────────────────────────────────────────────────────────────

cmd("leads_list", "List all leads in the workspace.", "GET", "/api/leads",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("leads_get", "Get a lead by ID.", "GET", "/api/leads/{lead_id}",
    [p("lead_id", d="Lead ID")],
    {"lead_id": "path"})

cmd("leads_create", "Create a new lead.", "POST", "/api/leads",
    [p("email", d="Lead email address"),
     p("first_name", req=False, d="First name"),
     p("last_name", req=False, d="Last name"),
     p("company_name", req=False, d="Company name"),
     p("title", req=False, d="Job title"),
     p("phone", req=False, d="Phone number"),
     p("website", req=False, d="Website URL"),
     p("custom_variables", req=False, d="JSON object of custom variable values")],
    {"email": "body", "first_name": "body", "last_name": "body", "company_name": "body",
     "title": "body", "phone": "body", "website": "body", "custom_variables": "body"})

cmd("leads_create_multiple", "Create multiple leads at once.", "POST", "/api/leads/multiple",
    [p("leads", d="JSON array of lead objects")],
    {"leads": "body"})

cmd("leads_create_or_update", "Create or update a lead by ID.", "POST", "/api/leads/create-or-update/{lead_id}",
    [p("lead_id", d="Lead ID"),
     p("email", req=False, d="Email address"),
     p("first_name", req=False, d="First name"),
     p("last_name", req=False, d="Last name"),
     p("company_name", req=False, d="Company name"),
     p("title", req=False, d="Job title"),
     p("phone", req=False, d="Phone number"),
     p("website", req=False, d="Website URL"),
     p("custom_variables", req=False, d="JSON object of custom variable values")],
    {"lead_id": "path", "email": "body", "first_name": "body", "last_name": "body",
     "company_name": "body", "title": "body", "phone": "body", "website": "body", "custom_variables": "body"})

cmd("leads_create_or_update_multiple", "Create or update multiple leads.", "POST", "/api/leads/create-or-update/multiple",
    [p("leads", d="JSON array of lead objects")],
    {"leads": "body"})

cmd("leads_update", "Update a lead by ID.", "PATCH", "/api/leads/{lead_id}",
    [p("lead_id", d="Lead ID"),
     p("first_name", req=False, d="First name"),
     p("last_name", req=False, d="Last name"),
     p("company_name", req=False, d="Company name"),
     p("title", req=False, d="Job title"),
     p("phone", req=False, d="Phone number"),
     p("website", req=False, d="Website URL")],
    {"lead_id": "path", "first_name": "body", "last_name": "body", "company_name": "body",
     "title": "body", "phone": "body", "website": "body"})

cmd("leads_replace", "Replace a lead entirely by ID.", "PUT", "/api/leads/{lead_id}",
    [p("lead_id", d="Lead ID"),
     p("email", d="Email address"),
     p("first_name", req=False, d="First name"),
     p("last_name", req=False, d="Last name"),
     p("company_name", req=False, d="Company name"),
     p("title", req=False, d="Job title"),
     p("phone", req=False, d="Phone number"),
     p("website", req=False, d="Website URL")],
    {"lead_id": "path", "email": "body", "first_name": "body", "last_name": "body",
     "company_name": "body", "title": "body", "phone": "body", "website": "body"})

cmd("leads_delete", "Delete a lead.", "DELETE", "/api/leads/{lead_id}",
    [p("lead_id", d="Lead ID")],
    {"lead_id": "path"})

cmd("leads_bulk_delete", "Delete multiple leads.", "DELETE", "/api/leads/bulk",
    [p("lead_ids", d="JSON array of lead IDs")],
    {"lead_ids": "body"})

cmd("leads_bulk_csv", "Import leads from CSV content.", "POST", "/api/leads/bulk/csv",
    [p("csv", d="CSV content string"),
     p("campaign_id", req=False, d="Campaign ID to attach imported leads to")],
    {"csv": "body", "campaign_id": "body"})

cmd("leads_update_status", "Update a lead's status.", "PATCH", "/api/leads/{lead_id}/update-status",
    [p("lead_id", d="Lead ID"),
     p("status", d="New status value")],
    {"lead_id": "path", "status": "body"})

cmd("leads_bulk_update_status", "Bulk update status for multiple leads.", "PATCH", "/api/leads/bulk-update-status",
    [p("lead_ids", d="JSON array of lead IDs"),
     p("status", d="New status value")],
    {"lead_ids": "body", "status": "body"})

cmd("leads_unsubscribe", "Unsubscribe a lead.", "PATCH", "/api/leads/{lead_id}/unsubscribe",
    [p("lead_id", d="Lead ID")],
    {"lead_id": "path"})

cmd("leads_blacklist", "Blacklist a lead.", "POST", "/api/leads/{lead_id}/blacklist",
    [p("lead_id", d="Lead ID")],
    {"lead_id": "path"})

cmd("leads_replies", "Get replies for a specific lead.", "GET", "/api/leads/{lead_id}/replies",
    [p("lead_id", d="Lead ID"),
     p("page", "int", False, "Page number")],
    {"lead_id": "path", "page": "query"})

cmd("leads_sent_emails", "Get sent emails for a specific lead.", "GET", "/api/leads/{lead_id}/sent-emails",
    [p("lead_id", d="Lead ID"),
     p("page", "int", False, "Page number")],
    {"lead_id": "path", "page": "query"})

cmd("leads_scheduled_emails", "Get scheduled emails for a specific lead.", "GET", "/api/leads/{lead_id}/scheduled-emails",
    [p("lead_id", d="Lead ID"),
     p("page", "int", False, "Page number")],
    {"lead_id": "path", "page": "query"})


# ─── REPLIES (master inbox) ──────────────────────────────────────────────────

cmd("replies_list", "List replies in the master inbox.", "GET", "/api/replies",
    [p("page", "int", False, "Page number"),
     p("campaign_id", req=False, d="Filter by campaign ID"),
     p("is_read", req=False, d="Filter by read status (true/false)")],
    {"page": "query", "campaign_id": "query", "is_read": "query"})

cmd("replies_get", "Get a reply by ID.", "GET", "/api/replies/{id}",
    [p("id", d="Reply ID")],
    {"id": "path"})

cmd("replies_new", "Send a new email (not a reply to an existing thread).", "POST", "/api/replies/new",
    [p("to_email", d="Recipient email address"),
     p("from_sender_email_id", d="Sender email account ID to send from"),
     p("subject", req=False, d="Email subject"),
     p("body_text", req=False, d="Plain text body"),
     p("body_html", req=False, d="HTML body")],
    {"to_email": "body", "from_sender_email_id": "body", "subject": "body",
     "body_text": "body", "body_html": "body"})

cmd("replies_reply", "Reply to an existing email thread.", "POST", "/api/replies/{reply_id}/reply",
    [p("reply_id", d="Reply ID to respond to"),
     p("message", d="Reply message body (HTML supported)"),
     p("sender_email_id", req=False, d="Sender email account ID (required unless reply_all is true)"),
     p("to_emails", req=False, d="JSON array of recipient email addresses (required unless reply_all is true)"),
     p("reply_all", "bool", False, "If true, reply to all recipients using the original sender")],
    {"reply_id": "path", "message": "body", "sender_email_id": "body",
     "to_emails": "body", "reply_all": "body"})

cmd("replies_forward", "Forward a reply to another email address.", "POST", "/api/replies/{reply_id}/forward",
    [p("reply_id", d="Reply ID to forward"),
     p("to_email", d="Email address to forward to"),
     p("body_text", req=False, d="Additional text to include")],
    {"reply_id": "path", "to_email": "body", "body_text": "body"})

cmd("replies_mark_interested", "Mark a reply as interested.", "PATCH", "/api/replies/{reply_id}/mark-as-interested",
    [p("reply_id", d="Reply ID")],
    {"reply_id": "path"})

cmd("replies_mark_not_interested", "Mark a reply as not interested.", "PATCH", "/api/replies/{reply_id}/mark-as-not-interested",
    [p("reply_id", d="Reply ID")],
    {"reply_id": "path"})

cmd("replies_mark_read_unread", "Mark a reply as read or unread.", "PATCH", "/api/replies/{reply_id}/mark-as-read-or-unread",
    [p("reply_id", d="Reply ID"),
     p("is_read", "bool", True, "True to mark as read, false for unread")],
    {"reply_id": "path", "is_read": "body"})

cmd("replies_mark_automated", "Mark a reply as automated or not automated.", "PATCH", "/api/replies/{reply_id}/mark-as-automated-or-not-automated",
    [p("reply_id", d="Reply ID"),
     p("is_automated", "bool", True, "True if automated, false if not")],
    {"reply_id": "path", "is_automated": "body"})

cmd("replies_unsubscribe", "Unsubscribe the contact from a reply.", "PATCH", "/api/replies/{reply_id}/unsubscribe",
    [p("reply_id", d="Reply ID")],
    {"reply_id": "path"})

cmd("replies_conversation_thread", "Get the full conversation thread for a reply.", "GET", "/api/replies/{reply_id}/conversation-thread",
    [p("reply_id", d="Reply ID")],
    {"reply_id": "path"})

cmd("replies_attach_scheduled_email", "Attach a scheduled email to a reply.", "POST", "/api/replies/{reply_id}/attach-scheduled-email-to-reply",
    [p("reply_id", d="Reply ID"),
     p("scheduled_email_id", d="Scheduled email ID")],
    {"reply_id": "path", "scheduled_email_id": "body"})

cmd("replies_push_to_followup", "Push a reply to a follow-up campaign.", "POST", "/api/replies/{reply_id}/followup-campaign/push",
    [p("reply_id", d="Reply ID"),
     p("campaign_id", d="Follow-up campaign ID")],
    {"reply_id": "path", "campaign_id": "body"})

cmd("replies_delete", "Delete a reply.", "DELETE", "/api/replies/{reply_id}",
    [p("reply_id", d="Reply ID")],
    {"reply_id": "path"})


# ─── REPLY TEMPLATES ─────────────────────────────────────────────────────────

cmd("reply_templates_list", "List reply templates.", "GET", "/api/reply-templates",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("reply_templates_get", "Get a reply template by ID.", "GET", "/api/reply-templates/{id}",
    [p("id", d="Reply template ID")],
    {"id": "path"})

cmd("reply_templates_create", "Create a reply template.", "POST", "/api/reply-templates",
    [p("name", d="Template name"),
     p("body_text", req=False, d="Plain text template body"),
     p("body_html", req=False, d="HTML template body")],
    {"name": "body", "body_text": "body", "body_html": "body"})

cmd("reply_templates_update", "Update a reply template.", "PUT", "/api/reply-templates/{id}",
    [p("id", d="Reply template ID"),
     p("name", req=False, d="Template name"),
     p("body_text", req=False, d="Plain text body"),
     p("body_html", req=False, d="HTML body")],
    {"id": "path", "name": "body", "body_text": "body", "body_html": "body"})

cmd("reply_templates_delete", "Delete a reply template.", "DELETE", "/api/reply-templates/{reply_template_id}",
    [p("reply_template_id", d="Reply template ID")],
    {"reply_template_id": "path"})


# ─── SCHEDULED EMAILS ────────────────────────────────────────────────────────

cmd("scheduled_emails_list", "List scheduled emails.", "GET", "/api/scheduled-emails",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("scheduled_emails_get", "Get a scheduled email by ID.", "GET", "/api/scheduled-emails/{id}",
    [p("id", d="Scheduled email ID")],
    {"id": "path"})


# ─── TAGS ─────────────────────────────────────────────────────────────────────

cmd("tags_list", "List custom tags.", "GET", "/api/tags",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("tags_get", "Get a tag by ID.", "GET", "/api/tags/{id}",
    [p("id", d="Tag ID")],
    {"id": "path"})

cmd("tags_create", "Create a custom tag.", "POST", "/api/tags",
    [p("name", d="Tag name")],
    {"name": "body"})

cmd("tags_delete", "Delete a tag.", "DELETE", "/api/tags/{tag_id}",
    [p("tag_id", d="Tag ID")],
    {"tag_id": "path"})

cmd("tags_attach_to_campaigns", "Attach tags to campaigns.", "POST", "/api/tags/attach-to-campaigns",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("campaign_ids", d="JSON array of campaign IDs")],
    {"tag_ids": "body", "campaign_ids": "body"})

cmd("tags_remove_from_campaigns", "Remove tags from campaigns.", "POST", "/api/tags/remove-from-campaigns",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("campaign_ids", d="JSON array of campaign IDs")],
    {"tag_ids": "body", "campaign_ids": "body"})

cmd("tags_attach_to_leads", "Attach tags to leads.", "POST", "/api/tags/attach-to-leads",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("lead_ids", d="JSON array of lead IDs")],
    {"tag_ids": "body", "lead_ids": "body"})

cmd("tags_remove_from_leads", "Remove tags from leads.", "POST", "/api/tags/remove-from-leads",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("lead_ids", d="JSON array of lead IDs")],
    {"tag_ids": "body", "lead_ids": "body"})

cmd("tags_attach_to_sender_emails", "Attach tags to sender email accounts.", "POST", "/api/tags/attach-to-sender-emails",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("sender_email_ids", d="JSON array of sender email IDs")],
    {"tag_ids": "body", "sender_email_ids": "body"})

cmd("tags_remove_from_sender_emails", "Remove tags from sender email accounts.", "POST", "/api/tags/remove-from-sender-emails",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("sender_email_ids", d="JSON array of sender email IDs")],
    {"tag_ids": "body", "sender_email_ids": "body"})


# ─── TRACKING DOMAINS ────────────────────────────────────────────────────────

cmd("tracking_domains_list", "List custom tracking domains.", "GET", "/api/custom-tracking-domain",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("tracking_domains_get", "Get a custom tracking domain by ID.", "GET", "/api/custom-tracking-domain/{id}",
    [p("id", d="Tracking domain ID")],
    {"id": "path"})

cmd("tracking_domains_create", "Add a custom tracking domain.", "POST", "/api/custom-tracking-domain",
    [p("domain", d="Domain name")],
    {"domain": "body"})

cmd("tracking_domains_delete", "Delete a custom tracking domain.", "DELETE", "/api/custom-tracking-domain/{custom_tracking_domain_id}",
    [p("custom_tracking_domain_id", d="Tracking domain ID")],
    {"custom_tracking_domain_id": "path"})


# ─── USERS ────────────────────────────────────────────────────────────────────

cmd("users_get", "Get current user profile.", "GET", "/api/users",
    [], {})

cmd("users_update_password", "Update the current user's password.", "PUT", "/api/users/password",
    [p("current_password", d="Current password"),
     p("password", d="New password"),
     p("password_confirmation", d="Confirm new password")],
    {"current_password": "body", "password": "body", "password_confirmation": "body"})

cmd("users_update_profile_picture", "Update the current user's profile picture.", "POST", "/api/users/profile-picture",
    [p("photo", d="Base64-encoded photo or file path")],
    {"photo": "body"})

cmd("users_headless_ui_token", "Generate a headless UI token for the current user.", "POST", "/api/users/headless-ui-token",
    [], {})


# ─── WARMUP ───────────────────────────────────────────────────────────────────

cmd("warmup_list", "List sender email accounts with warmup status.", "GET", "/api/warmup/sender-emails",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("warmup_get", "Get warmup details for a sender email account.", "GET", "/api/warmup/sender-emails/{senderEmailId}",
    [p("senderEmailId", d="Sender email account ID")],
    {"senderEmailId": "path"})

cmd("warmup_enable", "Enable warmup for sender email accounts.", "PATCH", "/api/warmup/sender-emails/enable",
    [p("sender_email_ids", d="JSON array of sender email IDs")],
    {"sender_email_ids": "body"})

cmd("warmup_disable", "Disable warmup for sender email accounts.", "PATCH", "/api/warmup/sender-emails/disable",
    [p("sender_email_ids", d="JSON array of sender email IDs")],
    {"sender_email_ids": "body"})

cmd("warmup_update_limits", "Update daily warmup limits for sender email accounts.", "PATCH", "/api/warmup/sender-emails/update-daily-warmup-limits",
    [p("sender_email_ids", d="JSON array of sender email IDs"),
     p("daily_warmup_limit", "int", True, "New daily warmup limit")],
    {"sender_email_ids": "body", "daily_warmup_limit": "body"})


# ─── WEBHOOK EVENTS ──────────────────────────────────────────────────────────

cmd("webhook_events_event_types", "List available webhook event types.", "GET", "/api/webhook-events/event-types",
    [], {})

cmd("webhook_events_sample_payload", "Get a sample payload for a webhook event type.", "GET", "/api/webhook-events/sample-payload",
    [p("event_type", req=False, d="Event type to get sample for")],
    {"event_type": "query"})

cmd("webhook_events_test", "Send a test webhook event.", "POST", "/api/webhook-events/test-event",
    [p("webhook_url_id", d="Webhook URL ID"),
     p("event_type", d="Event type to test")],
    {"webhook_url_id": "body", "event_type": "body"})


# ─── WEBHOOKS ─────────────────────────────────────────────────────────────────

cmd("webhooks_list", "List webhook URLs.", "GET", "/api/webhook-url",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("webhooks_get", "Get a webhook URL by ID.", "GET", "/api/webhook-url/{id}",
    [p("id", d="Webhook URL ID")],
    {"id": "path"})

cmd("webhooks_create", "Create a webhook URL.", "POST", "/api/webhook-url",
    [p("url", d="Webhook endpoint URL"),
     p("event_types", req=False, d="JSON array of event types to subscribe to")],
    {"url": "body", "event_types": "body"})

cmd("webhooks_update", "Update a webhook URL.", "PUT", "/api/webhook-url/{id}",
    [p("id", d="Webhook URL ID"),
     p("url", req=False, d="New webhook URL"),
     p("event_types", req=False, d="JSON array of event types")],
    {"id": "path", "url": "body", "event_types": "body"})

cmd("webhooks_delete", "Delete a webhook URL.", "DELETE", "/api/webhook-url/{webhook_url_id}",
    [p("webhook_url_id", d="Webhook URL ID")],
    {"webhook_url_id": "path"})


# ─── WORKSPACES (v1) ─────────────────────────────────────────────────────────

cmd("workspaces_list", "List workspaces.", "GET", "/api/workspaces",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("workspaces_get", "Get a workspace by ID.", "GET", "/api/workspaces/{team_id}",
    [p("team_id", d="Workspace/team ID")],
    {"team_id": "path"})

cmd("workspaces_create", "Create a new workspace.", "POST", "/api/workspaces",
    [p("name", d="Workspace name")],
    {"name": "body"})

cmd("workspaces_update", "Update a workspace.", "PUT", "/api/workspaces/{team_id}",
    [p("team_id", d="Workspace/team ID"),
     p("name", req=False, d="New workspace name")],
    {"team_id": "path", "name": "body"})

cmd("workspaces_switch", "Switch to a different workspace.", "POST", "/api/workspaces/switch-workspace",
    [p("team_id", d="Workspace/team ID to switch to")],
    {"team_id": "body"})

cmd("workspaces_invite_members", "Invite members to the workspace.", "POST", "/api/workspaces/invite-members",
    [p("emails", d="JSON array of email addresses to invite")],
    {"emails": "body"})

cmd("workspaces_accept_invite", "Accept a workspace invitation.", "POST", "/api/workspaces/accept/{team_invitation_id}",
    [p("team_invitation_id", d="Invitation ID")],
    {"team_invitation_id": "path"})

cmd("workspaces_update_member", "Update a workspace member's role.", "PUT", "/api/workspaces/members/{user_id}",
    [p("user_id", d="User ID"),
     p("role", req=False, d="New role")],
    {"user_id": "path", "role": "body"})

cmd("workspaces_remove_member", "Remove a member from the workspace.", "DELETE", "/api/workspaces/members/{user_id}",
    [p("user_id", d="User ID")],
    {"user_id": "path"})


# ─── WORKSPACES v1.1 ─────────────────────────────────────────────────────────

cmd("workspaces_v11_list", "List workspaces (v1.1 API).", "GET", "/api/workspaces/v1.1",
    [p("page", "int", False, "Page number")],
    {"page": "query"})

cmd("workspaces_v11_get", "Get a workspace by ID (v1.1 API).", "GET", "/api/workspaces/v1.1/{team_id}",
    [p("team_id", d="Workspace/team ID")],
    {"team_id": "path"})

cmd("workspaces_v11_create", "Create a new workspace (v1.1 API).", "POST", "/api/workspaces/v1.1",
    [p("name", d="Workspace name")],
    {"name": "body"})

cmd("workspaces_v11_update", "Update a workspace (v1.1 API).", "PUT", "/api/workspaces/v1.1/{team_id}",
    [p("team_id", d="Workspace/team ID"),
     p("name", req=False, d="New workspace name")],
    {"team_id": "path", "name": "body"})

cmd("workspaces_v11_delete", "Delete a workspace (v1.1 API).", "DELETE", "/api/workspaces/v1.1/{team_id}",
    [p("team_id", d="Workspace/team ID")],
    {"team_id": "path"})

cmd("workspaces_v11_switch", "Switch to a different workspace (v1.1 API).", "POST", "/api/workspaces/v1.1/switch-workspace",
    [p("team_id", d="Workspace/team ID")],
    {"team_id": "body"})

cmd("workspaces_v11_create_user", "Create a user in the workspace (v1.1 API).", "POST", "/api/workspaces/v1.1/users",
    [p("name", d="User name"),
     p("email", d="User email"),
     p("password", d="User password"),
     p("role", d="User role (e.g. admin, member)")],
    {"name": "body", "email": "body", "password": "body", "role": "body"})

cmd("workspaces_v11_invite_members", "Invite members to workspace (v1.1 API).", "POST", "/api/workspaces/v1.1/invite-members",
    [p("emails", d="JSON array of email addresses")],
    {"emails": "body"})

cmd("workspaces_v11_accept_invite", "Accept a workspace invitation (v1.1 API).", "POST", "/api/workspaces/v1.1/accept/{team_invitation_id}",
    [p("team_invitation_id", d="Invitation ID")],
    {"team_invitation_id": "path"})

cmd("workspaces_v11_remove_member", "Remove a member from the workspace (v1.1 API).", "DELETE", "/api/workspaces/v1.1/members/{user_id}",
    [p("user_id", d="User ID")],
    {"user_id": "path"})

cmd("workspaces_v11_create_api_token", "Create an API token for a workspace (v1.1 API).", "POST", "/api/workspaces/v1.1/{team_id}/api-tokens",
    [p("team_id", d="Workspace/team ID"),
     p("name", d="Token name")],
    {"team_id": "path", "name": "body"})

cmd("workspaces_v11_stats", "Get workspace statistics (v1.1 API).", "GET", "/api/workspaces/v1.1/stats",
    [p("start_date", req=False, d="Start date (YYYY-MM-DD)"),
     p("end_date", req=False, d="End date (YYYY-MM-DD)")],
    {"start_date": "query", "end_date": "query"})

cmd("workspaces_v11_line_area_chart_stats", "Get workspace line/area chart stats (v1.1 API).", "GET", "/api/workspaces/v1.1/line-area-chart-stats",
    [p("start_date", req=False, d="Start date (YYYY-MM-DD)"),
     p("end_date", req=False, d="End date (YYYY-MM-DD)")],
    {"start_date": "query", "end_date": "query"})

cmd("workspaces_v11_master_inbox_settings", "Get master inbox settings (v1.1 API).", "GET", "/api/workspaces/v1.1/master-inbox-settings",
    [], {})

cmd("workspaces_v11_update_master_inbox_settings", "Update master inbox settings (v1.1 API).", "PATCH", "/api/workspaces/v1.1/master-inbox-settings",
    [p("settings", d="JSON object of master inbox settings")],
    {"settings": "body"})


# ─── Plugin class ────────────────────────────────────────────────────────────

_TYPE_MAP = {"str": "str", "int": "int", "bool": "bool"}
_OPTIONAL_TYPE_MAP = {"str": "Optional[str]", "int": "Optional[int]", "bool": "Optional[bool]"}


class BisonPlugin(MCPPlugin):
    name = "bison"

    def __init__(self):
        self.tools = {}
        self._register_meta_tools()
        for cmd_def in COMMANDS:
            self._register_tool(cmd_def)

    def _register_tool(self, cmd_def: dict) -> None:
        name = cmd_def["name"]
        desc = cmd_def["description"]
        method = cmd_def["method"]
        path = cmd_def["path"]
        params = cmd_def.get("params", [])
        field_mappings = cmd_def.get("field_mappings", {})

        sig_parts: list[str] = []
        collect_lines: list[str] = []

        for param in params:
            pname = param["name"]
            ptype = param["type"]
            req = param.get("required", True)

            if req:
                type_str = _TYPE_MAP.get(ptype, "str")
                sig_parts.append(f"{pname}: {type_str}")
            else:
                opt_type = _OPTIONAL_TYPE_MAP.get(ptype, "Optional[str]")
                sig_parts.append(f"{pname}: {opt_type} = None")

            collect_lines.append(f'    if {pname} is not None: _p["{pname}"] = {pname}')

        sig_parts.append('account: str = ""')

        sig = ", ".join(sig_parts)
        collect = "\n".join(collect_lines) if collect_lines else "    pass"

        fn_code = f'''def {name}({sig}) -> dict:
    """{desc}"""
    _p = {{}}
{collect}
    return _bison_request("{method}", "{path}", _p, {repr(field_mappings)}, account=account)
'''

        ns: dict[str, Any] = {
            "Optional": Optional,
            "_bison_request": _bison_request,
        }
        exec(fn_code, ns)
        fn = ns[name]

        access = _access_for_command(cmd_def)
        self.tools[name] = ToolDef(access=access, handler=fn, description=desc)

    def _register_meta_tools(self):
        def get_account_credentials(account: str = "") -> dict:
            """Get the API key and base URL for a configured Bison account. Use this when an agent needs to call the Bison API directly in code."""
            resolved = _get_bison_account_credentials(account)
            if "error" in resolved:
                return resolved
            return {
                "account": resolved["account"],
                "api_key": resolved["api_key"],
                "base_url": resolved["base_url"],
                "hint": "Use these credentials for direct Bison API calls in code. Keep the API key secret.",
            }

        def list_accounts() -> dict:
            """List all configured Bison accounts for the current key. Use this to find available account names before calling other Bison tools or get_account_credentials."""
            accounts = _list_bison_accounts()
            return {"accounts": accounts, "hint": "Pass the account name as the `account` parameter to any Bison tool."}

        self.tools["get_account_credentials"] = ToolDef(
            access="admin",
            handler=get_account_credentials,
            description=get_account_credentials.__doc__,
        )
        self.tools["list_accounts"] = ToolDef(access="read", handler=list_accounts, description=list_accounts.__doc__)

    def health_check(self) -> dict[str, Any]:
        """Health check cannot validate per-key credentials, so return ok."""
        return {"status": "ok"}
