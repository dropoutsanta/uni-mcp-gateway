"""BrandNav screener plugin for the MCP Gateway.

Search Shopify / DTC brands on https://app.brandnav.io/screener using the
internal REST API. There is no public API key — auth is the user's session
cookie copied straight out of the browser.

Required credentials per account:
  cookie     – full Cookie header value copied from a logged-in browser
               (open DevTools → Network → any POST to /api/* → "Request
               Headers" → "cookie" → right-click "Copy value")
  user_agent – (optional) matching User-Agent for the browser the cookie was
               captured from. Defaults to a recent Chrome on macOS string.

Multi-account: prefix each key with `<account>.` (e.g. `sales.cookie`,
`sales.user_agent`). Omit the prefix for the default account.

Cloudflare sits in front of brandnav.io, so we mirror the browser's
sec-fetch / origin / referer headers exactly. If the cookie expires the
endpoints typically respond 401 / 403 or with `success: false` and a
"please log in" style message — re-capture the cookie from the browser to
recover.

Endpoint map (all POST):
  /api/screener/filter/filters-list      list every available filter + type
  /api/screener/filter/filter-details    options/range for one filter
  /api/screener/search/count/            count matches (free, no quota cost)
  /api/screener/search/search            actual search, returns rows
  /api/screener/user/quota               remaining search quota for today
  /api/screener/export/export-columns    list of columns you can project / export
  /api/screener/export/export-request    queue a CSV export
  /api/screener/export/status            poll an export request
  /api/screener/export/exports-list      past exports
  /api/about/data/user                   session check (returns user object)
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials


_BASE = "https://app.brandnav.io"
_TIMEOUT = 60.0
_SEARCH_TIMEOUT = 300.0
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("brandnav")
    except RuntimeError:
        return []
    accounts: set[str] = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "cookie" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str = "") -> dict[str, Any]:
    try:
        creds = get_credentials("brandnav")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        avail = _list_accounts()
        if len(avail) == 1:
            selected = avail[0]
        elif len(avail) > 1:
            return {
                "error": "Multiple BrandNav accounts configured. Specify `account`.",
                "available_accounts": avail,
            }
        else:
            return {"error": "No BrandNav credentials configured for this key."}

    prefix = "" if selected == "default" else f"{selected}."
    cookie = creds.get(f"{prefix}cookie", "")
    ua = creds.get(f"{prefix}user_agent", "") or _DEFAULT_UA
    if not cookie:
        return {
            "error": f"No `cookie` credential for BrandNav account '{selected}'.",
            "available_accounts": _list_accounts(),
        }
    return {"account": selected, "cookie": cookie, "user_agent": ua}


def _headers(cookie: str, user_agent: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Cookie": cookie,
        "Origin": _BASE,
        "Referer": f"{_BASE}/screener",
        "User-Agent": user_agent,
        "Sec-Ch-Ua": '"Chromium";v="147", "Not.A/Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _post(
    path: str,
    cookie: str,
    user_agent: str,
    body: Optional[dict] = None,
    timeout: float = _TIMEOUT,
) -> dict[str, Any]:
    url = f"{_BASE}{path}"
    try:
        resp = httpx.post(
            url,
            headers=_headers(cookie, user_agent),
            json=body if body is not None else {},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return {"error": f"Timed out after {timeout}s calling {path}."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 401 or resp.status_code == 403:
        return {
            "error": (
                f"HTTP {resp.status_code} — your BrandNav cookie is likely "
                "expired or invalid. Re-copy the Cookie header from a "
                "logged-in browser and update the credential."
            ),
            "details_text": resp.text[:500],
        }
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "?")
        return {"error": f"Rate limited by Cloudflare. Retry after {retry}s."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text[:500]}

    try:
        return resp.json()
    except Exception:
        return {"data": resp.text[:1000]}


def _parse_json_param(name: str, value: str) -> Any:
    """Parse a JSON-string-encoded parameter or return an error dict."""
    if not value:
        return {"_error": f"`{name}` is required (JSON string)."}
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        return {"_error": f"`{name}` is not valid JSON: {exc}"}


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class BrandNavPlugin(MCPPlugin):
    name = "brandnav"

    def __init__(self) -> None:
        self.tools: dict[str, ToolDef] = {
            # Meta
            "how_to_use_me": ToolDef(
                access="read",
                handler=self.how_to_use_me,
                description=(
                    "START HERE. Returns a guide explaining the BrandNav "
                    "screener, the filter object shape, the typical "
                    "discover→count→search workflow, and how the cookie "
                    "auth works."
                ),
            ),
            "check_session": ToolDef(
                access="read",
                handler=self.check_session,
                description=(
                    "Verify the configured cookie is still valid by "
                    "fetching the BrandNav user object (POST "
                    "/api/about/data/user). Returns the email + "
                    "onboarding state on success, or an auth error if "
                    "the cookie has expired. Optional: account."
                ),
            ),
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts_handler,
                description="List BrandNav accounts configured for this gateway key.",
            ),
            # Discovery
            "filters_list": ToolDef(
                access="read",
                handler=self.filters_list,
                description=(
                    "Get every filter you can use in a search. Returns "
                    "[{id, title, type, groupName, groupID}, …]. Filter "
                    "types: 0 = code list (country, language), 1 = "
                    "string list (industry, platform, technologies), "
                    "2 = numeric range (followers), 4 = numeric range "
                    "(financial), 5 = AI keyword search. Use "
                    "filter_details(id=…) to see the legal values / "
                    "ranges for each filter. Optional: account."
                ),
            ),
            "filter_details": ToolDef(
                access="read",
                handler=self.filter_details,
                description=(
                    "Get the legal options or numeric range for a single "
                    "filter. Returns either {list:[…]} for list filters "
                    "(items may be strings or {name,code,group} objects) "
                    "or {minMinValue,maxMaxValue,minValue,maxValue} for "
                    "numeric filters. Params: id (required, e.g. "
                    "'country', 'niche', 'revenue', 'platform'). "
                    "Optional: account."
                ),
            ),
            "export_columns": ToolDef(
                access="read",
                handler=self.export_columns,
                description=(
                    "List every column you can request via `projection` "
                    "in search() or via export_request(). Returns "
                    "[{id, title}, …]. Optional: account."
                ),
            ),
            "quota": ToolDef(
                access="read",
                handler=self.quota,
                description=(
                    "Remaining BrandNav screener quota for the cookie's "
                    "account. Returns {quota, searchQuota, planDetails}. "
                    "Each search() call uses 1 from `searchQuota`. "
                    "count() is FREE. Optional: account."
                ),
            ),
            # Searching
            "count": ToolDef(
                access="read",
                handler=self.count,
                description=(
                    "FREE — count brands matching a filter set without "
                    "burning search quota. Always call this BEFORE "
                    "search() to size the result set.\n"
                    "Params: filters_json (required — JSON array of "
                    "filter objects, see how_to_use_me for the shape). "
                    "Optional: account.\n"
                    "Returns {success, count}."
                ),
            ),
            "search": ToolDef(
                access="read",
                handler=self.search,
                description=(
                    "Run a screener search. Costs 1 from `searchQuota`. "
                    "Params: filters_json (required — JSON array of "
                    "filter objects), projection_json (optional — JSON "
                    "array of column ids from export_columns; defaults "
                    "to a useful subset). Optional: account.\n"
                    "Returns {success, searchResults: [...]}. The result "
                    "set is capped server-side per plan; use "
                    "export_request for full extracts.\n"
                    "FILTER SHAPE — every filter object must include "
                    "{id, type, ...payload}. Payload by type:\n"
                    "  type 0 (country/language): {list: [{name,code,group}], normalSelection: false}\n"
                    "  type 1 (industry/platform/tech/etc): {list: ['Skincare & Cosmetics', ...], normalSelection: false}\n"
                    "  type 2/4 (numeric range — followers, revenue, prices, tech spend): {minValue: N, maxValue: N, normalSelection: false}\n"
                    "  type 5 (AI keyword search): {list: ['keyword1','keyword2', ...]}  (max 50 keywords, each ≤255 chars)\n"
                    "Use filters_list + filter_details to discover legal ids and values."
                ),
            ),
            # Exports (full extracts beyond on-screen limits)
            "export_request": ToolDef(
                access="write",
                handler=self.export_request,
                description=(
                    "Queue a CSV export of search results. Returns a "
                    "request_id; poll export_status(request_ids_json) "
                    "until completed, then download from "
                    "https://datav2.brandnav.io/exports/file-<id>.csv.\n"
                    "Params: filters_json (required), limit (default "
                    "1000), name (export name, default 'mcp-export'), "
                    "id_list_json (optional — limit to specific row "
                    "ids), exclude_exports_json (optional — list of "
                    "previous export ids to dedupe against), "
                    "normal_selection (bool, default true). "
                    "Optional: account."
                ),
            ),
            "export_status": ToolDef(
                access="read",
                handler=self.export_status,
                description=(
                    "Poll one or more export jobs. Params: "
                    "request_ids_json (required — JSON array of export "
                    "request_ids returned by export_request). Returns "
                    "{success, completed_data: {<id>: <bool>}}. When "
                    "true, fetch from "
                    "https://datav2.brandnav.io/exports/file-<id>.csv. "
                    "Optional: account."
                ),
            ),
            "exports_list": ToolDef(
                access="read",
                handler=self.exports_list,
                description="List past exports for the account. Optional: account.",
            ),
        }

    # =================================================================
    # Meta
    # =================================================================

    def how_to_use_me(self, **_: Any) -> dict[str, Any]:
        return {
            "overview": (
                "BrandNav is a Shopify/DTC brand discovery tool. The "
                "gateway plugin uses your browser session cookie to "
                "drive the same screener you see in the web UI."
            ),
            "auth": {
                "model": "session cookie (no public API key exists)",
                "set_credentials_example": (
                    'gateway_set_credentials(plugin="brandnav", '
                    'credentials_json=\'{"cookie": "ph_phc_...; '
                    'next-auth.session-token=...; ..."}\')'
                ),
                "rotate_when": (
                    "Any tool returns 'cookie is likely expired'. Open "
                    "https://app.brandnav.io in the browser used to "
                    "capture the cookie, log back in, and re-copy the "
                    "full Cookie header from any /api/* request."
                ),
            },
            "filter_object_shape": {
                "wrapper": "{id, type, ...payload}",
                "type_0_code_list": {
                    "examples": ["country", "language"],
                    "payload": (
                        '{"list":[{"name":"United States","code":"US","group":"North America"}, '
                        '{"name":"Canada","code":"CA","group":"North America"}], '
                        '"normalSelection":false}'
                    ),
                },
                "type_1_string_list": {
                    "examples": [
                        "niche", "platform", "technology", "shipping_partners",
                        "features", "city", "status",
                    ],
                    "payload": '{"list":["Skincare & Cosmetics","Fitness"], "normalSelection":false}',
                },
                "type_2_or_4_numeric_range": {
                    "examples_2": ["combined_followers", "instagram_followers"],
                    "examples_4": [
                        "revenue", "avg_prod_price", "min_prod_price",
                        "max_prod_price", "monthly_tech_spend",
                    ],
                    "payload": '{"minValue":10000, "maxValue":100000, "normalSelection":false}',
                },
                "type_5_keywords": {
                    "examples": ["keywords"],
                    "payload": '{"list":["organic","vegan","gluten-free"]}',
                    "limits": "≤50 keywords, each ≤255 chars",
                },
            },
            "typical_workflow": [
                "1. check_session() — confirm the cookie still works.",
                "2. filters_list() — see every filter id + its type.",
                "3. filter_details(id='niche') — fetch legal industry values.",
                "4. count(filters_json='[{...}]') — FREE, sizes the result set.",
                "5. search(filters_json='[{...}]') — burns 1 search quota, returns rows.",
                "6. (optional) export_request + export_status to pull a full CSV.",
            ],
            "example_search": {
                "task": "US Shopify skincare brands doing $50k–$500k/mo",
                "filters_json": json.dumps([
                    {
                        "id": "country",
                        "type": 0,
                        "list": [{"name": "United States", "code": "US", "group": "North America"}],
                        "normalSelection": False,
                    },
                    {
                        "id": "platform",
                        "type": 1,
                        "list": ["Shopify"],
                        "normalSelection": False,
                    },
                    {
                        "id": "niche",
                        "type": 1,
                        "list": ["Skincare & Cosmetics"],
                        "normalSelection": False,
                    },
                    {
                        "id": "revenue",
                        "type": 4,
                        "minValue": 50000,
                        "maxValue": 500000,
                        "normalSelection": False,
                    },
                ]),
            },
            "tips": [
                "ALWAYS count() before search() — count is free and avoids burning quota on bad filters.",
                "BrandNav search() returns a capped page (typically a few hundred rows). For full extracts use export_request → poll export_status → download CSV.",
                "filter_details() values are case-sensitive — copy them verbatim into your `list` payloads.",
                "Free plan is ~20 searches and 100 export rows. Bigger jobs need a paid plan on the underlying account.",
                "Cloudflare fronts the API — heavy parallelism may trip 429s. Keep concurrency low.",
            ],
            "multi_account": (
                "If multiple BrandNav accounts are configured (e.g. sales, "
                "ops), pass account='sales' to each call. Use "
                "list_accounts to see what's configured."
            ),
        }

    def list_accounts_handler(self, **_: Any) -> dict[str, Any]:
        return {"accounts": _list_accounts()}

    def check_session(self, account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        resp = _post("/api/about/data/user", r["cookie"], r["user_agent"])
        if "error" in resp:
            return resp
        data = resp.get("data") or {}
        return {
            "success": resp.get("success", False),
            "account": r["account"],
            "user_email": data.get("email") or data.get("emailAddress"),
            "raw_keys": list(data.keys())[:25],
        }

    # =================================================================
    # Discovery
    # =================================================================

    def filters_list(self, account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        return _post("/api/screener/filter/filters-list", r["cookie"], r["user_agent"])

    def filter_details(self, id: str = "", account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        if not id:
            return {"error": "`id` is required (e.g. 'country', 'niche', 'revenue')."}
        return _post(
            "/api/screener/filter/filter-details",
            r["cookie"],
            r["user_agent"],
            body={"id": id},
        )

    def export_columns(self, account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        return _post("/api/screener/export/export-columns", r["cookie"], r["user_agent"])

    def quota(self, account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        return _post("/api/screener/user/quota", r["cookie"], r["user_agent"])

    # =================================================================
    # Searching
    # =================================================================

    def count(self, filters_json: str = "", account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        filters = _parse_json_param("filters_json", filters_json)
        if isinstance(filters, dict) and "_error" in filters:
            return {"error": filters["_error"]}
        if not isinstance(filters, list):
            return {"error": "filters_json must be a JSON array of filter objects."}
        return _post(
            "/api/screener/search/count/",
            r["cookie"],
            r["user_agent"],
            body={"filters": filters},
            timeout=_SEARCH_TIMEOUT,
        )

    def search(
        self,
        filters_json: str = "",
        projection_json: str = "",
        account: str = "",
    ) -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        filters = _parse_json_param("filters_json", filters_json)
        if isinstance(filters, dict) and "_error" in filters:
            return {"error": filters["_error"]}
        if not isinstance(filters, list):
            return {"error": "filters_json must be a JSON array of filter objects."}

        if projection_json:
            projection = _parse_json_param("projection_json", projection_json)
            if isinstance(projection, dict) and "_error" in projection:
                return {"error": projection["_error"]}
            if not isinstance(projection, list):
                return {"error": "projection_json must be a JSON array of column ids."}
        else:
            projection = [
                "domain", "title", "country", "platform", "niche",
                "revenue", "combined_followers", "emails", "phones",
                "instagram_url", "facebook_url", "linkedin_url",
            ]

        return _post(
            "/api/screener/search/search",
            r["cookie"],
            r["user_agent"],
            body={"filters": filters, "projection": projection},
            timeout=_SEARCH_TIMEOUT,
        )

    # =================================================================
    # Exports
    # =================================================================

    def export_request(
        self,
        filters_json: str = "",
        limit: int = 1000,
        name: str = "mcp-export",
        id_list_json: str = "",
        exclude_exports_json: str = "",
        normal_selection: bool = True,
        account: str = "",
    ) -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        filters = _parse_json_param("filters_json", filters_json)
        if isinstance(filters, dict) and "_error" in filters:
            return {"error": filters["_error"]}
        if not isinstance(filters, list):
            return {"error": "filters_json must be a JSON array of filter objects."}

        id_list: list = []
        if id_list_json:
            id_list_parsed = _parse_json_param("id_list_json", id_list_json)
            if isinstance(id_list_parsed, dict) and "_error" in id_list_parsed:
                return {"error": id_list_parsed["_error"]}
            if not isinstance(id_list_parsed, list):
                return {"error": "id_list_json must be a JSON array."}
            id_list = id_list_parsed

        exclude_exports: list = []
        if exclude_exports_json:
            ex_parsed = _parse_json_param("exclude_exports_json", exclude_exports_json)
            if isinstance(ex_parsed, dict) and "_error" in ex_parsed:
                return {"error": ex_parsed["_error"]}
            if not isinstance(ex_parsed, list):
                return {"error": "exclude_exports_json must be a JSON array."}
            exclude_exports = ex_parsed

        body = {
            "filters": filters,
            "limit": int(limit),
            "idList": id_list,
            "name": name,
            "excludeExports": exclude_exports,
            "normalSelection": bool(normal_selection),
        }
        return _post(
            "/api/screener/export/export-request",
            r["cookie"],
            r["user_agent"],
            body=body,
            timeout=_SEARCH_TIMEOUT,
        )

    def export_status(self, request_ids_json: str = "", account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        ids = _parse_json_param("request_ids_json", request_ids_json)
        if isinstance(ids, dict) and "_error" in ids:
            return {"error": ids["_error"]}
        if not isinstance(ids, list):
            return {"error": "request_ids_json must be a JSON array of export request_ids."}
        return _post(
            "/api/screener/export/status",
            r["cookie"],
            r["user_agent"],
            body={"request_ids": ids},
        )

    def exports_list(self, account: str = "") -> dict[str, Any]:
        r = _resolve(account)
        if "error" in r:
            return r
        return _post(
            "/api/screener/export/exports-list",
            r["cookie"],
            r["user_agent"],
        )

    # =================================================================
    # Health
    # =================================================================

    def health_check(self) -> dict[str, Any]:
        # Without per-key context we can only check the host responds.
        try:
            resp = httpx.get(f"{_BASE}/screener", timeout=10.0)
            if resp.status_code in (200, 302, 307):
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
