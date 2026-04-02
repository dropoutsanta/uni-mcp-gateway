"""Instantly API v2 plugin for the MCP Gateway.

Full access to the Instantly.ai cold email platform: accounts, campaigns,
leads, email/unibox, analytics, tags, webhooks, and more.
Multi-account support via {account}.api_key credential pattern.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE_URL = "https://api.instantly.ai"

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


_TYPE_MAP = {"str": "str", "int": "int", "bool": "bool", "float": "float"}
_OPTIONAL_TYPE_MAP = {
    "str": "Optional[str]",
    "int": "Optional[int]",
    "bool": "Optional[bool]",
    "float": "Optional[float]",
}


# ── Multi-account credential resolution ──────────────────────────────────────

def _list_instantly_accounts() -> list[str]:
    try:
        creds = get_credentials("instantly")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _get_instantly_account_credentials(account: str = "") -> dict:
    try:
        creds = get_credentials("instantly")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_instantly_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Instantly accounts configured. You MUST specify the `account` parameter.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Instantly credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No Instantly credentials found for account '{selected}'.",
            "available_accounts": _list_instantly_accounts(),
        }

    return {"account": selected, "api_key": api_key}


# ── HTTP request helper ──────────────────────────────────────────────────────

def _instantly_request(method: str, path: str, params: dict, field_mappings: dict, account: str = "") -> dict:
    resolved = _get_instantly_account_credentials(account)
    if "error" in resolved:
        return resolved

    api_key = resolved["api_key"]
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

    url = f"{_BASE_URL}{url_path}"
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


# ── Access level determination ────────────────────────────────────────────────

READ_LIKE_POSTS = {
    "leads_list",
    "accounts_warmup_analytics",
    "accounts_test_vitals",
    "lead_labels_test_ai",
}

ADMIN_TOOLS = {
    "workspace_patch",
    "workspace_change_owner",
    "workspace_domain_set",
    "workspace_domain_delete",
    "api_keys_list",
    "api_keys_create",
    "api_keys_delete",
    "accounts_move",
}


def _access_for_command(cmd_def: dict) -> str:
    name = cmd_def["name"]
    method = cmd_def["method"]
    if name in ADMIN_TOOLS:
        return "admin"
    if method == "GET" or name in READ_LIKE_POSTS:
        return "read"
    return "write"


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

# ── Accounts ─────────────────────────────────────────────────────────────────

cmd("accounts_list", "List sending accounts.", "GET", "/api/v2/accounts",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search by email"),
     p("status", "int", False, "Filter by status (1=active, 2=paused, -1=error)"),
     p("provider_code", "int", False, "Provider filter (2=Google, 3=Microsoft)"),
     p("tag_ids", "str", False, "Comma-separated tag IDs")],
    {"limit": "query", "starting_after": "query", "search": "query",
     "status": "query", "provider_code": "query", "tag_ids": "query"})

cmd("accounts_get", "Get a sending account by email.", "GET", "/api/v2/accounts/{email}",
    [p("email", d="Account email address")],
    {"email": "path"})

cmd("accounts_create", "Create a sending account.", "POST", "/api/v2/accounts",
    [p("email", d="Email address"),
     p("first_name", "str", False, "First name"),
     p("last_name", "str", False, "Last name"),
     p("provider_code", "int", False, "Provider (1=IMAP, 2=Google, 3=Microsoft, 4=AWS)"),
     p("imap_host", "str", False, "IMAP host"),
     p("imap_port", "int", False, "IMAP port"),
     p("imap_username", "str", False, "IMAP username"),
     p("imap_password", "str", False, "IMAP password"),
     p("smtp_host", "str", False, "SMTP host"),
     p("smtp_port", "int", False, "SMTP port"),
     p("smtp_username", "str", False, "SMTP username"),
     p("smtp_password", "str", False, "SMTP password"),
     p("daily_limit", "int", False, "Daily sending limit"),
     p("warmup_enabled", "bool", False, "Enable warmup immediately")],
    {"email": "body", "first_name": "body", "last_name": "body", "provider_code": "body",
     "imap_host": "body", "imap_port": "body", "imap_username": "body", "imap_password": "body",
     "smtp_host": "body", "smtp_port": "body", "smtp_username": "body", "smtp_password": "body",
     "daily_limit": "body", "warmup_enabled": "body"})

cmd("accounts_patch", "Update a sending account.", "PATCH", "/api/v2/accounts/{email}",
    [p("email", d="Account email"),
     p("first_name", "str", False, "First name"),
     p("last_name", "str", False, "Last name"),
     p("daily_limit", "int", False, "Daily sending limit"),
     p("tracking_domain_name", "str", False, "Custom tracking domain")],
    {"email": "path", "first_name": "body", "last_name": "body",
     "daily_limit": "body", "tracking_domain_name": "body"})

cmd("accounts_delete", "Delete a sending account.", "DELETE", "/api/v2/accounts/{email}",
    [p("email", d="Account email")],
    {"email": "path"})

cmd("accounts_pause", "Pause a sending account.", "POST", "/api/v2/accounts/{email}/pause",
    [p("email", d="Account email")],
    {"email": "path"})

cmd("accounts_resume", "Resume a paused sending account.", "POST", "/api/v2/accounts/{email}/resume",
    [p("email", d="Account email")],
    {"email": "path"})

cmd("accounts_mark_fixed", "Mark a sending account as fixed after an error.", "POST", "/api/v2/accounts/{email}/mark-fixed",
    [p("email", d="Account email")],
    {"email": "path"})

cmd("accounts_test_vitals", "Test vitals for one or more accounts.", "POST", "/api/v2/accounts/test/vitals",
    [p("accounts", d="JSON array of account email strings to test")],
    {"accounts": "body"})

cmd("accounts_warmup_enable", "Enable warmup for accounts.", "POST", "/api/v2/accounts/warmup/enable",
    [p("emails", d="JSON array of email addresses")],
    {"emails": "body"})

cmd("accounts_warmup_disable", "Disable warmup for accounts.", "POST", "/api/v2/accounts/warmup/disable",
    [p("emails", d="JSON array of email addresses")],
    {"emails": "body"})

cmd("accounts_analytics_daily", "Get daily account analytics.", "GET", "/api/v2/accounts/analytics/daily",
    [p("start_date", "str", False, "Start date (YYYY-MM-DD)"),
     p("end_date", "str", False, "End date (YYYY-MM-DD)"),
     p("emails", "str", False, "Comma-separated emails to filter")],
    {"start_date": "query", "end_date": "query", "emails": "query"})

cmd("accounts_warmup_analytics", "Get warmup analytics for accounts.", "POST", "/api/v2/accounts/warmup-analytics",
    [p("emails", d="JSON array of email addresses")],
    {"emails": "body"})

cmd("accounts_ctd_status", "Get custom tracking domain status.", "GET", "/api/v2/accounts/ctd/status",
    [p("host", d="Tracking domain hostname")],
    {"host": "query"})

cmd("accounts_move", "Move accounts between workspaces (admin workspace key required).", "POST", "/api/v2/accounts/move",
    [p("emails", d="JSON array of email addresses"),
     p("source_workspace_id", d="Source workspace ID"),
     p("destination_workspace_id", d="Destination workspace ID")],
    {"emails": "body", "source_workspace_id": "body", "destination_workspace_id": "body"})

cmd("accounts_campaign_mapping", "Get campaigns associated with an email account.", "GET", "/api/v2/account-campaign-mappings/{email}",
    [p("email", d="Account email"),
     p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor")],
    {"email": "path", "limit": "query", "starting_after": "query"})


# ── Campaigns ────────────────────────────────────────────────────────────────

cmd("campaigns_list", "List campaigns.", "GET", "/api/v2/campaigns",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search by name"),
     p("tag_ids", "str", False, "Comma-separated tag IDs"),
     p("status", "int", False, "Status filter")],
    {"limit": "query", "starting_after": "query", "search": "query",
     "tag_ids": "query", "status": "query"})

cmd("campaigns_get", "Get a campaign by ID.", "GET", "/api/v2/campaigns/{id}",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_create", "Create a campaign.", "POST", "/api/v2/campaigns",
    [p("name", d="Campaign name"),
     p("sequences", "str", False, "JSON array of sequence step objects"),
     p("schedule", "str", False, "JSON schedule configuration object"),
     p("campaign_accounts", "str", False, "JSON array of account email strings"),
     p("daily_limit", "int", False, "Daily sending limit"),
     p("stop_on_reply", "bool", False, "Stop sequence on reply"),
     p("stop_on_auto_reply", "bool", False, "Stop sequence on auto-reply"),
     p("text_only", "bool", False, "Send text-only emails"),
     p("link_tracking", "bool", False, "Enable link tracking"),
     p("open_tracking", "bool", False, "Enable open tracking")],
    {"name": "body", "sequences": "body", "schedule": "body",
     "campaign_accounts": "body", "daily_limit": "body",
     "stop_on_reply": "body", "stop_on_auto_reply": "body",
     "text_only": "body", "link_tracking": "body", "open_tracking": "body"})

cmd("campaigns_patch", "Update a campaign.", "PATCH", "/api/v2/campaigns/{id}",
    [p("id", d="Campaign ID"),
     p("name", "str", False, "Campaign name"),
     p("sequences", "str", False, "JSON array of sequence steps"),
     p("schedule", "str", False, "JSON schedule configuration"),
     p("daily_limit", "int", False, "Daily sending limit"),
     p("stop_on_reply", "bool", False),
     p("stop_on_auto_reply", "bool", False),
     p("link_tracking", "bool", False),
     p("open_tracking", "bool", False)],
    {"id": "path", "name": "body", "sequences": "body", "schedule": "body",
     "daily_limit": "body", "stop_on_reply": "body", "stop_on_auto_reply": "body",
     "link_tracking": "body", "open_tracking": "body"})

cmd("campaigns_delete", "Delete a campaign.", "DELETE", "/api/v2/campaigns/{id}",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_activate", "Activate (start/resume) a campaign.", "POST", "/api/v2/campaigns/{id}/activate",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_pause", "Pause (stop) a campaign.", "POST", "/api/v2/campaigns/{id}/pause",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_duplicate", "Duplicate a campaign.", "POST", "/api/v2/campaigns/{id}/duplicate",
    [p("id", d="Campaign ID"),
     p("name", "str", False, "Name for the duplicated campaign")],
    {"id": "path", "name": "body"})

cmd("campaigns_share", "Share a campaign (generate share link).", "POST", "/api/v2/campaigns/{id}/share",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_export", "Export a campaign to JSON format.", "POST", "/api/v2/campaigns/{id}/export",
    [p("id", d="Campaign ID")],
    {"id": "path"})

cmd("campaigns_from_export", "Create a campaign from a shared/exported campaign.", "POST", "/api/v2/campaigns/{id}/from-export",
    [p("id", d="Shared campaign ID")],
    {"id": "path"})

cmd("campaigns_add_variables", "Add variables to a campaign.", "POST", "/api/v2/campaigns/{id}/variables",
    [p("id", d="Campaign ID"),
     p("variables", d="JSON array of variable objects")],
    {"id": "path", "variables": "body"})

cmd("campaigns_analytics", "Get analytics for one or more campaigns.", "GET", "/api/v2/campaigns/analytics",
    [p("id", "str", False, "Single campaign ID"),
     p("ids", "str", False, "Comma-separated campaign IDs"),
     p("start_date", "str", False, "Start date"),
     p("end_date", "str", False, "End date"),
     p("exclude_total_leads_count", "bool", False, "Exclude total leads count")],
    {"id": "query", "ids": "query", "start_date": "query", "end_date": "query",
     "exclude_total_leads_count": "query"})

cmd("campaigns_analytics_daily", "Get daily campaign analytics.", "GET", "/api/v2/campaigns/analytics/daily",
    [p("campaign_id", d="Campaign ID"),
     p("start_date", "str", False, "Start date"),
     p("end_date", "str", False, "End date"),
     p("campaign_status", "int", False, "Filter by campaign status")],
    {"campaign_id": "query", "start_date": "query", "end_date": "query", "campaign_status": "query"})

cmd("campaigns_analytics_overview", "Get campaign analytics overview.", "GET", "/api/v2/campaigns/analytics/overview",
    [p("id", "str", False, "Single campaign ID"),
     p("ids", "str", False, "Comma-separated campaign IDs"),
     p("campaign_status", "int", False, "Filter by campaign status"),
     p("expand_crm_events", "bool", False, "Include CRM events breakdown")],
    {"id": "query", "ids": "query", "campaign_status": "query", "expand_crm_events": "query"})

cmd("campaigns_analytics_steps", "Get campaign steps analytics.", "GET", "/api/v2/campaigns/analytics/steps",
    [p("campaign_id", d="Campaign ID"),
     p("start_date", "str", False, "Start date"),
     p("end_date", "str", False, "End date"),
     p("include_opportunities_count", "bool", False, "Include opportunities count")],
    {"campaign_id": "query", "start_date": "query", "end_date": "query",
     "include_opportunities_count": "query"})

cmd("campaigns_count_launched", "Get count of launched campaigns.", "GET", "/api/v2/campaigns/count-launched",
    [], {})

cmd("campaigns_search_by_contact", "Search campaigns by lead email.", "GET", "/api/v2/campaigns/search-by-contact",
    [p("search", d="Lead email to search for"),
     p("sort_column", "str", False, "Column to sort by"),
     p("sort_order", "str", False, "Sort order (asc/desc)")],
    {"search": "query", "sort_column": "query", "sort_order": "query"})

cmd("campaigns_sending_status", "Get campaign sending status.", "GET", "/api/v2/campaigns/{id}/sending-status",
    [p("id", d="Campaign ID"),
     p("with_ai_summary", "bool", False, "Include AI summary")],
    {"id": "path", "with_ai_summary": "query"})


# ── Campaign Subsequences ────────────────────────────────────────────────────

cmd("subsequences_list", "List campaign subsequences.", "GET", "/api/v2/subsequences",
    [p("parent_campaign", d="Parent campaign ID"),
     p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search by name")],
    {"parent_campaign": "query", "limit": "query", "starting_after": "query", "search": "query"})

cmd("subsequences_get", "Get a subsequence by ID.", "GET", "/api/v2/subsequences/{id}",
    [p("id", d="Subsequence ID")],
    {"id": "path"})

cmd("subsequences_create", "Create a campaign subsequence.", "POST", "/api/v2/subsequences",
    [p("parent_campaign", d="Parent campaign ID"),
     p("name", d="Subsequence name"),
     p("conditions", "str", False, "JSON conditions object"),
     p("subsequence_schedule", "str", False, "JSON schedule configuration"),
     p("sequences", "str", False, "JSON array of sequence steps")],
    {"parent_campaign": "body", "name": "body", "conditions": "body",
     "subsequence_schedule": "body", "sequences": "body"})

cmd("subsequences_patch", "Update a subsequence.", "PATCH", "/api/v2/subsequences/{id}",
    [p("id", d="Subsequence ID"),
     p("name", "str", False, "Name")],
    {"id": "path", "name": "body"})

cmd("subsequences_delete", "Delete a subsequence.", "DELETE", "/api/v2/subsequences/{id}",
    [p("id", d="Subsequence ID")],
    {"id": "path"})

cmd("subsequences_duplicate", "Duplicate a subsequence.", "POST", "/api/v2/subsequences/{id}/duplicate",
    [p("id", d="Subsequence ID"),
     p("parent_campaign", d="Parent campaign for the duplicate"),
     p("name", "str", False, "Name for the duplicate")],
    {"id": "path", "parent_campaign": "body", "name": "body"})

cmd("subsequences_pause", "Pause a subsequence.", "POST", "/api/v2/subsequences/{id}/pause",
    [p("id", d="Subsequence ID")],
    {"id": "path"})

cmd("subsequences_resume", "Resume a paused subsequence.", "POST", "/api/v2/subsequences/{id}/resume",
    [p("id", d="Subsequence ID")],
    {"id": "path"})

cmd("subsequences_sending_status", "Get subsequence sending status.", "GET", "/api/v2/subsequences/{id}/sending-status",
    [p("id", d="Subsequence ID"),
     p("with_ai_summary", "bool", False, "Include AI summary")],
    {"id": "path", "with_ai_summary": "query"})


# ── Leads ────────────────────────────────────────────────────────────────────

cmd("leads_list", "List leads (POST-based list with filters).", "POST", "/api/v2/leads/list",
    [p("campaign", "str", False, "Campaign ID to filter by"),
     p("list_id", "str", False, "Lead list ID to filter by"),
     p("search", "str", False, "Search term"),
     p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("interest_value", "int", False, "Interest status filter"),
     p("is_website_visitor", "bool", False, "Filter website visitors")],
    {"campaign": "body", "list_id": "body", "search": "body",
     "limit": "body", "starting_after": "body", "interest_value": "body",
     "is_website_visitor": "body"})

cmd("leads_get", "Get a lead by ID.", "GET", "/api/v2/leads/{id}",
    [p("id", d="Lead ID")],
    {"id": "path"})

cmd("leads_create", "Create a lead.", "POST", "/api/v2/leads",
    [p("email", d="Lead email address"),
     p("campaign", "str", False, "Campaign ID to add lead to"),
     p("list_id", "str", False, "Lead list ID to add lead to"),
     p("first_name", "str", False, "First name"),
     p("last_name", "str", False, "Last name"),
     p("company_name", "str", False, "Company name"),
     p("personalization", "str", False, "Personalization text"),
     p("phone", "str", False, "Phone number"),
     p("website", "str", False, "Website URL"),
     p("custom_variables", "str", False, "JSON object of custom variables")],
    {"email": "body", "campaign": "body", "list_id": "body",
     "first_name": "body", "last_name": "body", "company_name": "body",
     "personalization": "body", "phone": "body", "website": "body",
     "custom_variables": "body"})

cmd("leads_patch", "Update a lead.", "PATCH", "/api/v2/leads/{id}",
    [p("id", d="Lead ID"),
     p("first_name", "str", False),
     p("last_name", "str", False),
     p("company_name", "str", False),
     p("personalization", "str", False),
     p("phone", "str", False),
     p("website", "str", False),
     p("custom_variables", "str", False, "JSON object of custom variables")],
    {"id": "path", "first_name": "body", "last_name": "body",
     "company_name": "body", "personalization": "body",
     "phone": "body", "website": "body", "custom_variables": "body"})

cmd("leads_delete", "Delete a lead.", "DELETE", "/api/v2/leads/{id}",
    [p("id", d="Lead ID")],
    {"id": "path"})

cmd("leads_bulk_add", "Add leads in bulk to a campaign or list.", "POST", "/api/v2/leads/add",
    [p("leads", d="JSON array of lead objects (email, first_name, etc.)"),
     p("campaign_id", "str", False, "Campaign ID"),
     p("list_id", "str", False, "Lead list ID"),
     p("skip_if_in_workspace", "bool", False, "Skip if lead exists in workspace"),
     p("skip_if_in_campaign", "bool", False, "Skip if lead exists in campaign")],
    {"leads": "body", "campaign_id": "body", "list_id": "body",
     "skip_if_in_workspace": "body", "skip_if_in_campaign": "body"})

cmd("leads_bulk_delete", "Delete leads in bulk.", "DELETE", "/api/v2/leads",
    [p("campaign_id", "str", False, "Campaign ID"),
     p("list_id", "str", False, "Lead list ID"),
     p("ids", "str", False, "JSON array of lead IDs to delete"),
     p("delete_all_from_company", "bool", False, "Delete all from company"),
     p("limit", "int", False, "Max leads to delete")],
    {"campaign_id": "body", "list_id": "body", "ids": "body",
     "delete_all_from_company": "body", "limit": "body"})

cmd("leads_move", "Move leads to a different campaign or list (background job).", "POST", "/api/v2/leads/move",
    [p("to_campaign_id", "str", False, "Destination campaign ID"),
     p("to_list_id", "str", False, "Destination list ID"),
     p("filters", "str", False, "JSON filter object")],
    {"to_campaign_id": "body", "to_list_id": "body", "filters": "body"})

cmd("leads_merge", "Merge two leads.", "POST", "/api/v2/leads/merge",
    [p("lead_id", d="Source lead ID"),
     p("destination_lead_id", d="Destination lead ID")],
    {"lead_id": "body", "destination_lead_id": "body"})

cmd("leads_update_interest", "Update interest status of a lead.", "POST", "/api/v2/leads/update-interest-status",
    [p("lead_email", d="Lead email address"),
     p("interest_value", "int", True, "Interest value"),
     p("campaign_id", "str", False, "Campaign ID context")],
    {"lead_email": "body", "interest_value": "body", "campaign_id": "body"})

cmd("leads_bulk_assign", "Bulk assign leads to organization users.", "POST", "/api/v2/leads/bulk-assign",
    [p("organization_user_ids", d="JSON array of user IDs"),
     p("filters", "str", False, "JSON filter object")],
    {"organization_user_ids": "body", "filters": "body"})

cmd("leads_move_to_subsequence", "Move a lead to a subsequence.", "POST", "/api/v2/leads/subsequence/move",
    [p("subsequence_id", d="Subsequence ID"),
     p("id", d="Lead ID")],
    {"subsequence_id": "body", "id": "body"})

cmd("leads_remove_from_subsequence", "Remove a lead from a subsequence.", "POST", "/api/v2/leads/subsequence/remove",
    [p("id", d="Lead ID")],
    {"id": "body"})


# ── Lead Lists ───────────────────────────────────────────────────────────────

cmd("lead_lists_list", "List lead lists.", "GET", "/api/v2/lead-lists",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search by name"),
     p("has_enrichment_task", "bool", False, "Filter by enrichment task")],
    {"limit": "query", "starting_after": "query", "search": "query",
     "has_enrichment_task": "query"})

cmd("lead_lists_get", "Get a lead list by ID.", "GET", "/api/v2/lead-lists/{id}",
    [p("id", d="Lead list ID")],
    {"id": "path"})

cmd("lead_lists_create", "Create a lead list.", "POST", "/api/v2/lead-lists",
    [p("name", d="List name"),
     p("has_enrichment_task", "bool", False, "Enable enrichment"),
     p("owned_by", "str", False, "Owner user ID")],
    {"name": "body", "has_enrichment_task": "body", "owned_by": "body"})

cmd("lead_lists_patch", "Update a lead list.", "PATCH", "/api/v2/lead-lists/{id}",
    [p("id", d="Lead list ID"),
     p("name", "str", False, "List name"),
     p("has_enrichment_task", "bool", False, "Enable enrichment"),
     p("owned_by", "str", False, "Owner user ID")],
    {"id": "path", "name": "body", "has_enrichment_task": "body", "owned_by": "body"})

cmd("lead_lists_delete", "Delete a lead list.", "DELETE", "/api/v2/lead-lists/{id}",
    [p("id", d="Lead list ID")],
    {"id": "path"})

cmd("lead_lists_verification_stats", "Get verification statistics for a lead list.", "GET", "/api/v2/lead-lists/{id}/verification-stats",
    [p("id", d="Lead list ID")],
    {"id": "path"})


# ── Lead Labels ──────────────────────────────────────────────────────────────

cmd("lead_labels_list", "List lead labels.", "GET", "/api/v2/lead-labels",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search term"),
     p("interest_status", "int", False, "Filter by interest status")],
    {"limit": "query", "starting_after": "query", "search": "query",
     "interest_status": "query"})

cmd("lead_labels_get", "Get a lead label by ID.", "GET", "/api/v2/lead-labels/{id}",
    [p("id", d="Lead label ID")],
    {"id": "path"})

cmd("lead_labels_create", "Create a lead label.", "POST", "/api/v2/lead-labels",
    [p("label", d="Label text"),
     p("interest_status_label", "str", False, "Interest status label"),
     p("description", "str", False, "Description"),
     p("use_with_ai", "bool", False, "Enable AI classification with this label")],
    {"label": "body", "interest_status_label": "body",
     "description": "body", "use_with_ai": "body"})

cmd("lead_labels_patch", "Update a lead label.", "PATCH", "/api/v2/lead-labels/{id}",
    [p("id", d="Lead label ID"),
     p("label", "str", False, "Label text"),
     p("interest_status_label", "str", False),
     p("description", "str", False),
     p("use_with_ai", "bool", False)],
    {"id": "path", "label": "body", "interest_status_label": "body",
     "description": "body", "use_with_ai": "body"})

cmd("lead_labels_delete", "Delete a lead label.", "DELETE", "/api/v2/lead-labels/{id}",
    [p("id", d="Lead label ID"),
     p("reassigned_status", "str", False, "Reassign leads to this status")],
    {"id": "path", "reassigned_status": "body"})

cmd("lead_labels_test_ai", "Test AI reply label prediction.", "POST", "/api/v2/lead-labels/ai-reply-label",
    [p("reply_text", d="Email reply text to classify")],
    {"reply_text": "body"})


# ── Email / Unibox ───────────────────────────────────────────────────────────

cmd("emails_list", "List emails (rate limited: 20 req/min).", "GET", "/api/v2/emails",
    [p("campaign_id", "str", False, "Campaign ID filter"),
     p("list_id", "str", False, "Lead list ID filter"),
     p("eaccount", "str", False, "Sending account email filter"),
     p("is_unread", "bool", False, "Filter unread only"),
     p("lead_email", "str", False, "Filter by lead email"),
     p("email_type", "str", False, "Type filter (sent, received, etc.)"),
     p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor")],
    {"campaign_id": "query", "list_id": "query", "eaccount": "query",
     "is_unread": "query", "lead_email": "query", "email_type": "query",
     "limit": "query", "starting_after": "query"})

cmd("emails_get", "Get an email by ID.", "GET", "/api/v2/emails/{id}",
    [p("id", d="Email ID")],
    {"id": "path"})

cmd("emails_patch", "Update an email (e.g. mark as read, set reminder).", "PATCH", "/api/v2/emails/{id}",
    [p("id", d="Email ID"),
     p("is_unread", "bool", False, "Mark as unread/read"),
     p("reminder_ts", "str", False, "Reminder timestamp")],
    {"id": "path", "is_unread": "body", "reminder_ts": "body"})

cmd("emails_delete", "Delete an email.", "DELETE", "/api/v2/emails/{id}",
    [p("id", d="Email ID")],
    {"id": "path"})

cmd("emails_reply", "Reply to an email.", "POST", "/api/v2/emails/reply",
    [p("eaccount", d="Sending account email"),
     p("reply_to_uuid", d="UUID of the email to reply to"),
     p("body", d="Reply body text/HTML"),
     p("subject", "str", False, "Subject override")],
    {"eaccount": "body", "reply_to_uuid": "body", "body": "body", "subject": "body"})

cmd("emails_forward", "Forward an email.", "POST", "/api/v2/emails/forward",
    [p("eaccount", d="Sending account email"),
     p("reply_to_uuid", d="UUID of the email to forward"),
     p("to_address_email_list", d="JSON array of recipient email strings"),
     p("body", "str", False, "Additional body text"),
     p("subject", "str", False, "Subject override")],
    {"eaccount": "body", "reply_to_uuid": "body", "to_address_email_list": "body",
     "body": "body", "subject": "body"})

cmd("emails_send_test", "Send a test email (rate limited: 10 req/min).", "POST", "/api/v2/emails/test",
    [p("eaccount", d="Sending account email"),
     p("to_address_email_list", d="JSON array of recipient emails"),
     p("subject", d="Email subject"),
     p("body", d="Email body text/HTML")],
    {"eaccount": "body", "to_address_email_list": "body", "subject": "body", "body": "body"})

cmd("emails_mark_thread_read", "Mark all emails in a thread as read.", "POST", "/api/v2/emails/threads/{thread_id}/mark-as-read",
    [p("thread_id", d="Thread ID")],
    {"thread_id": "path"})

cmd("emails_unread_count", "Count unread emails.", "GET", "/api/v2/emails/unread/count",
    [], {})


# ── Custom Tags ──────────────────────────────────────────────────────────────

cmd("tags_list", "List custom tags.", "GET", "/api/v2/custom-tags",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("search", "str", False, "Search term"),
     p("resource_ids", "str", False, "Comma-separated resource IDs"),
     p("tag_ids", "str", False, "Comma-separated tag IDs")],
    {"limit": "query", "starting_after": "query", "search": "query",
     "resource_ids": "query", "tag_ids": "query"})

cmd("tags_get", "Get a custom tag by ID.", "GET", "/api/v2/custom-tags/{id}",
    [p("id", d="Tag ID")],
    {"id": "path"})

cmd("tags_create", "Create a custom tag.", "POST", "/api/v2/custom-tags",
    [p("label", d="Tag label"),
     p("description", "str", False, "Tag description")],
    {"label": "body", "description": "body"})

cmd("tags_patch", "Update a custom tag.", "PATCH", "/api/v2/custom-tags/{id}",
    [p("id", d="Tag ID"),
     p("label", "str", False, "Tag label"),
     p("description", "str", False, "Tag description")],
    {"id": "path", "label": "body", "description": "body"})

cmd("tags_delete", "Delete a custom tag.", "DELETE", "/api/v2/custom-tags/{id}",
    [p("id", d="Tag ID")],
    {"id": "path"})

cmd("tags_toggle_resource", "Assign or unassign tags to resources (accounts/campaigns).", "POST", "/api/v2/custom-tags/toggle-resource",
    [p("tag_ids", d="JSON array of tag IDs"),
     p("resource_type", d="Resource type (e.g. 'account', 'campaign')"),
     p("resource_ids", d="JSON array of resource IDs"),
     p("assign", "bool", True, "True to assign, false to unassign")],
    {"tag_ids": "body", "resource_type": "body", "resource_ids": "body", "assign": "body"})

cmd("tag_mappings_list", "List custom tag mappings (which tags are assigned to which resources).", "GET", "/api/v2/custom-tag-mappings",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("resource_ids", "str", False, "Comma-separated resource IDs")],
    {"limit": "query", "starting_after": "query", "resource_ids": "query"})


# ── Block List ───────────────────────────────────────────────────────────────

cmd("blocklist_list", "List block list entries.", "GET", "/api/v2/block-lists-entries",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("domains_only", "bool", False, "Show only domain entries"),
     p("search", "str", False, "Search term")],
    {"limit": "query", "starting_after": "query", "domains_only": "query", "search": "query"})

cmd("blocklist_get", "Get a block list entry by ID.", "GET", "/api/v2/block-lists-entries/{id}",
    [p("id", d="Block list entry ID")],
    {"id": "path"})

cmd("blocklist_create", "Create a block list entry.", "POST", "/api/v2/block-lists-entries",
    [p("bl_value", d="Email or domain to block")],
    {"bl_value": "body"})

cmd("blocklist_patch", "Update a block list entry.", "PATCH", "/api/v2/block-lists-entries/{id}",
    [p("id", d="Block list entry ID"),
     p("bl_value", d="Updated email or domain")],
    {"id": "path", "bl_value": "body"})

cmd("blocklist_delete", "Delete a block list entry.", "DELETE", "/api/v2/block-lists-entries/{id}",
    [p("id", d="Block list entry ID")],
    {"id": "path"})

cmd("blocklist_bulk_create", "Bulk create block list entries.", "POST", "/api/v2/block-lists-entries/bulk-create",
    [p("bl_values", d="JSON array of emails/domains to block")],
    {"bl_values": "body"})

cmd("blocklist_bulk_delete", "Bulk delete block list entries.", "POST", "/api/v2/block-lists-entries/bulk-delete",
    [p("ids", d="JSON array of entry IDs to delete")],
    {"ids": "body"})

cmd("blocklist_delete_all", "Delete all block list entries.", "DELETE", "/api/v2/block-lists-entries",
    [p("domains_only", "bool", False, "Only delete domain entries"),
     p("search", "str", False, "Filter by search term before deleting")],
    {"domains_only": "query", "search": "query"})

cmd("blocklist_download", "Download all block list entries as CSV.", "GET", "/api/v2/block-lists-entries/download",
    [p("domains_only", "bool", False, "Only include domain entries"),
     p("search", "str", False, "Filter by search term")],
    {"domains_only": "query", "search": "query"})


# ── OAuth ────────────────────────────────────────────────────────────────────

cmd("oauth_google_init", "Initialize Google OAuth flow for connecting a Google account.", "POST", "/api/v2/oauth/google/init",
    [], {})

cmd("oauth_microsoft_init", "Initialize Microsoft OAuth flow for connecting a Microsoft account.", "POST", "/api/v2/oauth/microsoft/init",
    [], {})

cmd("oauth_session_status", "Check the status of an OAuth session.", "GET", "/api/v2/oauth/session/status/{sessionId}",
    [p("sessionId", d="OAuth session ID")],
    {"sessionId": "path"})


# ── Webhooks ─────────────────────────────────────────────────────────────────

cmd("webhooks_list", "List webhooks.", "GET", "/api/v2/webhooks",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("campaign", "str", False, "Filter by campaign ID"),
     p("event_type", "str", False, "Filter by event type")],
    {"limit": "query", "starting_after": "query", "campaign": "query", "event_type": "query"})

cmd("webhooks_get", "Get a webhook by ID.", "GET", "/api/v2/webhooks/{id}",
    [p("id", d="Webhook ID")],
    {"id": "path"})

cmd("webhooks_create", "Create a webhook.", "POST", "/api/v2/webhooks",
    [p("name", d="Webhook name"),
     p("target_hook_url", d="Webhook target URL"),
     p("event_type", d="Event type to subscribe to"),
     p("campaign", "str", False, "Campaign ID to scope to"),
     p("custom_interest_value", "str", False, "Custom interest value filter"),
     p("headers", "str", False, "JSON object of custom headers")],
    {"name": "body", "target_hook_url": "body", "event_type": "body",
     "campaign": "body", "custom_interest_value": "body", "headers": "body"})

cmd("webhooks_patch", "Update a webhook.", "PATCH", "/api/v2/webhooks/{id}",
    [p("id", d="Webhook ID"),
     p("name", "str", False, "Webhook name"),
     p("target_hook_url", "str", False, "Target URL"),
     p("event_type", "str", False, "Event type"),
     p("campaign", "str", False, "Campaign ID")],
    {"id": "path", "name": "body", "target_hook_url": "body",
     "event_type": "body", "campaign": "body"})

cmd("webhooks_delete", "Delete a webhook.", "DELETE", "/api/v2/webhooks/{id}",
    [p("id", d="Webhook ID")],
    {"id": "path"})

cmd("webhooks_resume", "Resume a paused webhook.", "POST", "/api/v2/webhooks/{id}/resume",
    [p("id", d="Webhook ID")],
    {"id": "path"})

cmd("webhooks_test", "Send a test event to a webhook.", "POST", "/api/v2/webhooks/{id}/test",
    [p("id", d="Webhook ID")],
    {"id": "path"})

cmd("webhooks_event_types", "List available webhook event types.", "GET", "/api/v2/webhooks/event-types",
    [], {})

cmd("webhook_events_list", "List webhook events.", "GET", "/api/v2/webhook-events",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("success", "bool", False, "Filter by success/failure"),
     p("from_date", "str", False, "Start date filter"),
     p("to_date", "str", False, "End date filter"),
     p("search", "str", False, "Search term")],
    {"limit": "query", "starting_after": "query", "success": "query",
     "from_date": "query", "to_date": "query", "search": "query"})

cmd("webhook_events_get", "Get a webhook event by ID.", "GET", "/api/v2/webhook-events/{id}",
    [p("id", d="Webhook event ID")],
    {"id": "path"})

cmd("webhook_events_summary", "Get aggregate summary of webhook events.", "GET", "/api/v2/webhook-events/summary",
    [p("from_date", "str", False, "Start date"),
     p("to_date", "str", False, "End date")],
    {"from_date": "query", "to_date": "query"})

cmd("webhook_events_summary_by_date", "Get webhook events summary grouped by date.", "GET", "/api/v2/webhook-events/summary-by-date",
    [p("from_date", "str", False, "Start date"),
     p("to_date", "str", False, "End date")],
    {"from_date": "query", "to_date": "query"})


# ── Background Jobs ──────────────────────────────────────────────────────────

cmd("jobs_list", "List background jobs.", "GET", "/api/v2/background-jobs",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor"),
     p("ids", "str", False, "Comma-separated job IDs"),
     p("type", "str", False, "Job type filter"),
     p("entity_type", "str", False, "Entity type filter"),
     p("entity_id", "str", False, "Entity ID filter"),
     p("status", "str", False, "Status filter")],
    {"limit": "query", "starting_after": "query", "ids": "query",
     "type": "query", "entity_type": "query", "entity_id": "query", "status": "query"})

cmd("jobs_get", "Get a background job by ID.", "GET", "/api/v2/background-jobs/{id}",
    [p("id", d="Background job ID"),
     p("data_fields", "str", False, "Comma-separated data fields to include")],
    {"id": "path", "data_fields": "query"})


# ── Workspace ────────────────────────────────────────────────────────────────

cmd("workspace_get", "Get current workspace info.", "GET", "/api/v2/workspaces/current",
    [], {})

cmd("workspace_patch", "Update current workspace settings.", "PATCH", "/api/v2/workspaces/current",
    [p("name", "str", False, "Workspace name"),
     p("org_logo_url", "str", False, "Organization logo URL")],
    {"name": "body", "org_logo_url": "body"})

cmd("workspace_change_owner", "Transfer workspace ownership.", "POST", "/api/v2/workspaces/current/change-owner",
    [p("email", d="New owner email address")],
    {"email": "body"})

cmd("workspace_domain_get", "Get organization agency domain info.", "GET", "/api/v2/workspaces/current/whitelabel-domain",
    [], {})

cmd("workspace_domain_set", "Set the agency domain for the workspace.", "POST", "/api/v2/workspaces/current/whitelabel-domain",
    [p("domain", d="Agency domain name")],
    {"domain": "body"})

cmd("workspace_domain_delete", "Delete the organization agency domain.", "DELETE", "/api/v2/workspaces/current/whitelabel-domain",
    [], {})


# ── API Keys ─────────────────────────────────────────────────────────────────

cmd("api_keys_list", "List API keys.", "GET", "/api/v2/api-keys",
    [p("limit", "int", False, "Max results"),
     p("starting_after", "str", False, "Pagination cursor")],
    {"limit": "query", "starting_after": "query"})

cmd("api_keys_create", "Create a new API key.", "POST", "/api/v2/api-keys",
    [p("name", d="Key name"),
     p("scopes", d="JSON array of scope strings (e.g. [\"all:read\", \"campaigns:create\"])")],
    {"name": "body", "scopes": "body"})

cmd("api_keys_delete", "Delete an API key.", "DELETE", "/api/v2/api-keys/{id}",
    [p("id", d="API key ID")],
    {"id": "path"})


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN CLASS
# ══════════════════════════════════════════════════════════════════════════════

class InstantlyPlugin(MCPPlugin):
    name = "instantly"

    def __init__(self):
        self.tools: dict[str, ToolDef] = {}
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
    return _instantly_request("{method}", "{path}", _p, {repr(field_mappings)}, account=account)
'''

        ns: dict[str, Any] = {
            "Optional": Optional,
            "_instantly_request": _instantly_request,
        }
        exec(fn_code, ns)
        fn = ns[name]

        access = _access_for_command(cmd_def)
        self.tools[name] = ToolDef(access=access, handler=fn, description=desc)

    def _register_meta_tools(self):
        def get_account_credentials(account: str = "") -> dict:
            """Get the API key for a configured Instantly account. Use this when an agent needs to call the Instantly API directly in code."""
            resolved = _get_instantly_account_credentials(account)
            if "error" in resolved:
                return resolved
            return {
                "account": resolved["account"],
                "api_key": resolved["api_key"],
                "base_url": _BASE_URL,
                "hint": "Use these credentials for direct Instantly API calls. Keep the API key secret.",
            }

        def list_accounts() -> dict:
            """List all configured Instantly accounts for the current key."""
            accounts = _list_instantly_accounts()
            return {"accounts": accounts, "hint": "Pass the account name as the `account` parameter to any Instantly tool."}

        self.tools["get_account_credentials"] = ToolDef(
            access="admin",
            handler=get_account_credentials,
            description=get_account_credentials.__doc__,
        )
        self.tools["list_accounts"] = ToolDef(
            access="read",
            handler=list_accounts,
            description=list_accounts.__doc__,
        )

    def health_check(self) -> dict[str, Any]:
        try:
            resolved = _get_instantly_account_credentials()
            if "error" in resolved:
                return {"status": "no_credentials", "detail": resolved["error"]}
            resp = httpx.get(
                f"{_BASE_URL}/api/v2/workspaces/current",
                headers={"Authorization": f"Bearer {resolved['api_key']}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
