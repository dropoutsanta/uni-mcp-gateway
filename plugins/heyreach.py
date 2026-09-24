"""HeyReach LinkedIn automation plugin for the MCP Gateway.

Covers campaigns, leads, lists, LinkedIn accounts, conversations,
stats, webhooks, tags, and network. Multi-account via {account}.api_key.

Auth: X-API-KEY header (not Bearer).
Base URL: https://api.heyreach.io/api/public
Rate limit: 300 req/min.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE = "https://api.heyreach.io/api/public"
_TIMEOUT = 30


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("heyreach")
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
        creds = get_credentials("heyreach")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple HeyReach accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No HeyReach credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No HeyReach credentials for account '{selected}'.",
            "available_accounts": _list_accounts(),
        }
    return {"account": selected, "api_key": api_key}


def _req(method: str, path: str, api_key: str, *,
         params: dict | None = None,
         body: dict | None = None,
         timeout: float = _TIMEOUT) -> dict:
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    url = f"{_BASE}{path}"
    try:
        resp = httpx.request(method, url, headers=headers,
                             params=params, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 204:
        return {"success": True}
    if resp.status_code == 429:
        return {"error": "Rate limited (300 req/min). Try again shortly."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}
    try:
        return resp.json()
    except Exception:
        return {"data": resp.text}


class HeyReachPlugin(MCPPlugin):
    name = "heyreach"

    def __init__(self):
        self.tools: dict[str, ToolDef] = {
            # -- META --
            "how_to_use_me": ToolDef(
                access="read", handler=self.how_to_use_me,
                description=(
                    "START HERE. Returns a guide explaining every HeyReach tool, "
                    "when to use each one, and example workflows."
                ),
            ),
            "check_api_key": ToolDef(
                access="read", handler=self.check_api_key,
                description="Validate the API key. Returns account info if valid.",
            ),
            "list_accounts": ToolDef(
                access="read", handler=self.list_accounts_handler,
                description="List configured HeyReach accounts for the current key.",
            ),

            # -- CAMPAIGNS --
            "campaigns_list": ToolDef(
                access="read", handler=self.campaigns_list,
                description=(
                    "List all campaigns (paginated).\n"
                    "Params: limit (1-100, default 20), offset (default 0), "
                    "keyword (filter by name), statuses (comma-sep: DRAFT,PAUSED,"
                    "ACTIVE,FINISHED,ARCHIVED), account_ids (comma-sep LinkedIn "
                    "account IDs). Optional: account."
                ),
            ),

            # -- LISTS --
            "lists_list": ToolDef(
                access="read", handler=self.lists_list,
                description=(
                    "List all lead/company lists (paginated).\n"
                    "Params: limit (default 20), offset (default 0). Optional: account."
                ),
            ),
            "lists_create": ToolDef(
                access="write", handler=self.lists_create,
                description=(
                    "Create an empty list.\n"
                    "Params: name (required), type (USER_LIST or COMPANY_LIST, "
                    "default USER_LIST). Optional: account."
                ),
            ),

            # -- LEADS --
            "leads_list": ToolDef(
                access="read", handler=self.leads_list,
                description=(
                    "List leads in a list (paginated).\n"
                    "Params: list_id (required), limit (1-1000, default 100), "
                    "offset (default 0), keyword (filter), created_from (ISO date), "
                    "created_to (ISO date), linkedin_id, profile_url. Optional: account."
                ),
            ),
            "leads_get": ToolDef(
                access="read", handler=self.leads_get,
                description=(
                    "Get a single lead by LinkedIn profile URL.\n"
                    "Params: profile_url (required). Optional: account."
                ),
            ),
            "leads_add": ToolDef(
                access="write", handler=self.leads_add,
                description=(
                    "Add leads to a list (max 100 per call).\n"
                    "Params: list_id (required), leads_json (required — JSON array "
                    "of lead objects. Each lead needs at least a 'profileUrl' field. "
                    "Can also include firstName, lastName, companyName, etc.).\n"
                    "Optional: account."
                ),
            ),
            "leads_lists_for": ToolDef(
                access="read", handler=self.leads_lists_for,
                description=(
                    "Get all lists a lead belongs to.\n"
                    "Params: profile_url (required), limit (default 20), "
                    "offset (default 0). Optional: account."
                ),
            ),

            # -- COMPANIES --
            "companies_list": ToolDef(
                access="read", handler=self.companies_list,
                description=(
                    "Get companies from a company list.\n"
                    "Params: list_id (required), limit (default 100), "
                    "offset (default 0), keyword (filter). Optional: account."
                ),
            ),

            # -- LINKEDIN ACCOUNTS --
            "linkedin_accounts_list": ToolDef(
                access="read", handler=self.linkedin_accounts_list,
                description=(
                    "List all connected LinkedIn accounts (paginated).\n"
                    "Params: limit (1-100, default 20), offset (default 0), "
                    "keyword (filter). Optional: account."
                ),
            ),

            # -- STATS --
            "stats_overall": ToolDef(
                access="read", handler=self.stats_overall,
                description=(
                    "Get overall LinkedIn outreach statistics.\n"
                    "Params: date_from (YYYY-MM-DD), date_to (YYYY-MM-DD), "
                    "account_ids (comma-sep LinkedIn account IDs), "
                    "campaign_ids (comma-sep campaign IDs). All optional. "
                    "Optional: account."
                ),
            ),

            # -- CONVERSATIONS --
            "conversations_list": ToolDef(
                access="read", handler=self.conversations_list,
                description=(
                    "List LinkedIn conversations with filters (paginated).\n"
                    "Params: limit (1-100, default 20), offset (default 0), "
                    "filters_json (JSON object with filter criteria). "
                    "Optional: account."
                ),
            ),

            # -- WEBHOOKS --
            "webhooks_list": ToolDef(
                access="read", handler=self.webhooks_list,
                description=(
                    "List all webhooks (paginated).\n"
                    "Params: limit (default 20), offset (default 0). Optional: account."
                ),
            ),
            "webhooks_create": ToolDef(
                access="write", handler=self.webhooks_create,
                description=(
                    "Create a webhook.\n"
                    "Params: webhook_name (required, max 25 chars), "
                    "webhook_url (required), event_type (required — one of: "
                    "CONNECTION_REQUEST_SENT, CONNECTION_REQUEST_ACCEPTED, "
                    "MESSAGE_SENT), campaign_ids (comma-sep, optional — empty "
                    "means all campaigns). Optional: account."
                ),
            ),
            "webhooks_get": ToolDef(
                access="read", handler=self.webhooks_get,
                description="Get a webhook by ID. Params: webhook_id (required). Optional: account.",
            ),
            "webhooks_update": ToolDef(
                access="write", handler=self.webhooks_update,
                description=(
                    "Update a webhook.\n"
                    "Params: webhook_id (required). Optional: webhook_name, "
                    "webhook_url, event_type, campaign_ids (comma-sep), "
                    "is_active (bool). Optional: account."
                ),
            ),
            "webhooks_delete": ToolDef(
                access="write", handler=self.webhooks_delete,
                description="Delete a webhook. Params: webhook_id (required). Optional: account.",
            ),

            # -- TAGS --
            "tags_create": ToolDef(
                access="write", handler=self.tags_create,
                description=(
                    "Create tags for the workspace.\n"
                    "Params: tags_json (required — JSON array of objects with "
                    "'displayName' and 'color' fields). Optional: account."
                ),
            ),

            # -- NETWORK --
            "network_list": ToolDef(
                access="read", handler=self.network_list,
                description=(
                    "Get LinkedIn network/connections for a sender account.\n"
                    "Params: sender_id (required), page_number (default 0), "
                    "page_size (default 50). Optional: account."
                ),
            ),
        }

    # =================================================================
    # META
    # =================================================================

    def how_to_use_me(self, **kwargs) -> Any:
        return {
            "overview": (
                "HeyReach is a LinkedIn outreach automation platform. It manages "
                "multiple LinkedIn accounts, runs connection request and messaging "
                "campaigns, tracks conversations, and provides outreach analytics. "
                "Use this plugin to manage campaigns, leads, lists, and monitor "
                "LinkedIn outreach performance."
            ),
            "quick_guide": {
                "List all campaigns": "campaigns_list(limit=50)",
                "Find a specific campaign": "campaigns_list(keyword='onboarding')",
                "See connected LinkedIn accounts": "linkedin_accounts_list()",
                "List all lead lists": "lists_list()",
                "Get leads from a list": "leads_list(list_id=12345, limit=100)",
                "Look up a specific lead": "leads_get(profile_url='https://linkedin.com/in/janedoe')",
                "Add leads to a list": (
                    "leads_add(list_id=12345, leads_json='[{\"profileUrl\": "
                    "\"https://linkedin.com/in/janedoe\", \"firstName\": \"Jane\", "
                    "\"lastName\": \"Doe\"}]')"
                ),
                "Create a new lead list": "lists_create(name='Q2 Prospects')",
                "Check outreach stats": "stats_overall(date_from='2026-03-01', date_to='2026-03-31')",
                "View conversations": "conversations_list(limit=50)",
            },
            "workflows": {
                "Import leads from AI Ark into HeyReach": [
                    "1. Use ai_ark_search_people to find leads by domain/title/seniority",
                    "2. Create a HeyReach list: lists_create(name='AI Ark - Q2')",
                    "3. Format leads as [{profileUrl, firstName, lastName, companyName}]",
                    "4. Add them: leads_add(list_id=NEW_ID, leads_json='[...]')",
                    "Note: AI Ark returns LinkedIn URLs in the profile data — use those as profileUrl",
                ],
                "Monitor campaign performance": [
                    "1. stats_overall(date_from='2026-03-01') — get connection/message/reply rates",
                    "2. campaigns_list() — see campaign statuses",
                    "3. conversations_list() — check recent replies",
                ],
            },
            "tips": [
                "leads_add accepts max 100 leads per call. Batch larger imports.",
                "Rate limit is 300 req/min across all endpoints.",
                "Pagination uses offset/limit (not cursor). Default limit varies by endpoint.",
                "leads_list supports up to 1000 per page; other endpoints cap at 100.",
                "LinkedIn profile URLs are the primary key for leads.",
                "Use check_api_key to verify credentials before bulk operations.",
            ],
            "multi_account": (
                "If multiple HeyReach accounts are configured, pass account='name' "
                "to each call. Use list_accounts to see available accounts."
            ),
        }

    def check_api_key(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", "/auth/CheckApiKey", r["api_key"])

    def list_accounts_handler(self, **kwargs) -> dict:
        return {"accounts": _list_accounts()}

    # =================================================================
    # CAMPAIGNS
    # =================================================================

    def campaigns_list(self, limit: int = 20, offset: int = 0,
                       keyword: str = "", statuses: str = "",
                       account_ids: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {
            "limit": min(int(limit), 100),
            "offset": int(offset),
        }
        if keyword:
            body["keyword"] = keyword
        if statuses:
            body["statuses"] = [s.strip() for s in statuses.split(",") if s.strip()]
        if account_ids:
            body["accountIds"] = [int(i.strip()) for i in account_ids.split(",") if i.strip()]
        return _req("POST", "/campaign/GetAll", r["api_key"], body=body)

    # =================================================================
    # LISTS
    # =================================================================

    def lists_list(self, limit: int = 20, offset: int = 0,
                   account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("POST", "/list/GetAll", r["api_key"],
                     body={"limit": int(limit), "offset": int(offset)})

    def lists_create(self, name: str = "", type: str = "USER_LIST",
                     account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not name:
            return {"error": "name is required"}
        return _req("POST", "/list/CreateEmptyList", r["api_key"],
                     body={"name": name, "type": type})

    # =================================================================
    # LEADS
    # =================================================================

    def leads_list(self, list_id: int = 0, limit: int = 100, offset: int = 0,
                   keyword: str = "", created_from: str = "", created_to: str = "",
                   linkedin_id: str = "", profile_url: str = "",
                   account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not list_id:
            return {"error": "list_id is required"}
        body: dict[str, Any] = {
            "listId": int(list_id),
            "limit": min(int(limit), 1000),
            "offset": int(offset),
        }
        if keyword:
            body["keyword"] = keyword
        if created_from:
            body["createdFrom"] = created_from
        if created_to:
            body["createdTo"] = created_to
        if linkedin_id:
            body["leadLinkedInId"] = linkedin_id
        if profile_url:
            body["leadProfileUrl"] = profile_url
        return _req("POST", "/lead/GetAll", r["api_key"], body=body)

    def leads_get(self, profile_url: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        return _req("GET", "/lead/Get", r["api_key"],
                     params={"profileUrl": profile_url})

    def leads_add(self, list_id: int = 0, leads_json: str = "",
                  account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not list_id:
            return {"error": "list_id is required"}
        if not leads_json:
            return {"error": "leads_json is required (JSON array of lead objects)"}
        import json
        try:
            leads = json.loads(leads_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid leads_json: {e}"}
        if not isinstance(leads, list):
            return {"error": "leads_json must be a JSON array"}
        if len(leads) > 100:
            return {"error": f"Max 100 leads per call, got {len(leads)}. Batch your requests."}
        return _req("POST", "/lead/AddToListV2", r["api_key"],
                     body={"listId": int(list_id), "leads": leads})

    def leads_lists_for(self, profile_url: str = "", limit: int = 20,
                        offset: int = 0, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        return _req("GET", "/lead/GetListsForLead", r["api_key"],
                     params={"profileUrl": profile_url,
                             "limit": int(limit), "offset": int(offset)})

    # =================================================================
    # COMPANIES
    # =================================================================

    def companies_list(self, list_id: int = 0, limit: int = 100,
                       offset: int = 0, keyword: str = "",
                       account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not list_id:
            return {"error": "list_id is required"}
        body: dict[str, Any] = {
            "listId": int(list_id),
            "limit": min(int(limit), 1000),
            "offset": int(offset),
        }
        if keyword:
            body["keyword"] = keyword
        return _req("POST", "/list/GetCompaniesFromList", r["api_key"], body=body)

    # =================================================================
    # LINKEDIN ACCOUNTS
    # =================================================================

    def linkedin_accounts_list(self, limit: int = 20, offset: int = 0,
                               keyword: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {
            "limit": min(int(limit), 100),
            "offset": int(offset),
        }
        if keyword:
            body["keyword"] = keyword
        return _req("POST", "/li_account/GetAll", r["api_key"], body=body)

    # =================================================================
    # STATS
    # =================================================================

    def stats_overall(self, date_from: str = "", date_to: str = "",
                      account_ids: str = "", campaign_ids: str = "",
                      account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {}
        if date_from:
            body["dateFrom"] = date_from
        if date_to:
            body["dateTo"] = date_to
        if account_ids:
            body["accountIds"] = [int(i.strip()) for i in account_ids.split(",") if i.strip()]
        else:
            body["accountIds"] = []
        if campaign_ids:
            body["campaignIds"] = [int(i.strip()) for i in campaign_ids.split(",") if i.strip()]
        else:
            body["campaignIds"] = []
        return _req("POST", "/stats/GetOverallStats", r["api_key"], body=body)

    # =================================================================
    # CONVERSATIONS
    # =================================================================

    def conversations_list(self, limit: int = 20, offset: int = 0,
                           filters_json: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {
            "limit": min(int(limit), 100),
            "offset": int(offset),
        }
        if filters_json:
            import json
            try:
                body["filters"] = json.loads(filters_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid filters_json: {e}"}
        else:
            body["filters"] = {}
        return _req("POST", "/conversation/GetAllV2", r["api_key"], body=body)

    # =================================================================
    # WEBHOOKS
    # =================================================================

    def webhooks_list(self, limit: int = 20, offset: int = 0,
                      account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("POST", "/webhook/GetAll", r["api_key"],
                     body={"limit": int(limit), "offset": int(offset)})

    def webhooks_create(self, webhook_name: str = "", webhook_url: str = "",
                        event_type: str = "", campaign_ids: str = "",
                        account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not webhook_name:
            return {"error": "webhook_name is required"}
        if not webhook_url:
            return {"error": "webhook_url is required"}
        if not event_type:
            return {"error": "event_type is required (CONNECTION_REQUEST_SENT, CONNECTION_REQUEST_ACCEPTED, or MESSAGE_SENT)"}
        body: dict[str, Any] = {
            "webhookName": webhook_name[:25],
            "webhookUrl": webhook_url,
            "eventType": event_type,
        }
        if campaign_ids:
            body["campaignIds"] = [int(i.strip()) for i in campaign_ids.split(",") if i.strip()]
        else:
            body["campaignIds"] = []
        return _req("POST", "/webhook/Create", r["api_key"], body=body)

    def webhooks_get(self, webhook_id: int = 0, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not webhook_id:
            return {"error": "webhook_id is required"}
        return _req("GET", "/webhook/Get", r["api_key"],
                     params={"webhookId": int(webhook_id)})

    def webhooks_update(self, webhook_id: str = "", webhook_name: str = "",
                        webhook_url: str = "", event_type: str = "",
                        campaign_ids: str = "", is_active: Optional[bool] = None,
                        account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not webhook_id:
            return {"error": "webhook_id is required"}
        body: dict[str, Any] = {"webhookId": webhook_id}
        if webhook_name:
            body["webhookName"] = webhook_name[:25]
        if webhook_url:
            body["webhookUrl"] = webhook_url
        if event_type:
            body["eventType"] = event_type
        if campaign_ids:
            body["campaignIds"] = [int(i.strip()) for i in campaign_ids.split(",") if i.strip()]
        if is_active is not None:
            body["isActive"] = is_active
        return _req("PUT", "/webhook/Update", r["api_key"], body=body)

    def webhooks_delete(self, webhook_id: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not webhook_id:
            return {"error": "webhook_id is required"}
        return _req("DELETE", "/webhook/Delete", r["api_key"],
                     params={"webhookId": webhook_id})

    # =================================================================
    # TAGS
    # =================================================================

    def tags_create(self, tags_json: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not tags_json:
            return {"error": "tags_json is required (JSON array of {displayName, color})"}
        import json
        try:
            tags = json.loads(tags_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid tags_json: {e}"}
        return _req("POST", "/tags/Create", r["api_key"], body={"tags": tags})

    # =================================================================
    # NETWORK
    # =================================================================

    def network_list(self, sender_id: int = 0, page_number: int = 0,
                     page_size: int = 50, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not sender_id:
            return {"error": "sender_id is required (use linkedin_accounts_list to find IDs)"}
        return _req("POST", "/network/GetMyNetworkForSender", r["api_key"],
                     body={
                         "senderId": int(sender_id),
                         "pageNumber": int(page_number),
                         "pageSize": int(page_size),
                     })

    # =================================================================
    # HEALTH CHECK
    # =================================================================

    def health_check(self) -> dict[str, Any]:
        try:
            r = _resolve()
            if "error" in r:
                return {"status": "no_credentials", "detail": r["error"]}
            resp = httpx.get(
                f"{_BASE}/auth/CheckApiKey",
                headers={"X-API-KEY": r["api_key"]},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
