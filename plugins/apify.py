"""Apify web scraping/automation plugin for the MCP Gateway.

Run Apify Actors, fetch datasets, and search the Apify Store.
Multi-account via {account}.api_key credentials on each gateway key.

Auth: Authorization: Bearer <APIFY_TOKEN>
Base URL: https://api.apify.com/v2
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

import auth
from plugin_base import MCPPlugin, ToolDef, get_context, get_credentials

_BASE = "https://api.apify.com/v2"
_TIMEOUT = 120


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("apify")
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
        creds = get_credentials("apify")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Apify accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Apify credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No Apify credentials for account '{selected}'.",
            "available_accounts": _list_accounts(),
        }
    return {"account": selected, "api_key": api_key}


def _req(
    method: str,
    path: str,
    api_key: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = _TIMEOUT,
    check_spend: bool = False,
) -> dict:
    if check_spend:
        try:
            ctx = get_context()
            spend_err = auth.check_daily_spend_limit(ctx.key_id)
            if spend_err:
                return {"error": spend_err}
        except RuntimeError:
            pass

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{_BASE}{path}"
    try:
        resp = httpx.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": str(exc)}

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        return {"error": f"Apify HTTP {resp.status_code}", "detail": detail}

    if resp.status_code == 204 or not resp.content:
        return {"success": True}

    try:
        data = resp.json()
    except Exception:
        return {"raw": resp.text[:5000]}

    try:
        ctx = get_context()
        amount = auth.extract_spend_from_result(data)
        if amount > 0:
            auth.record_spend(ctx.key_id, amount)
    except RuntimeError:
        pass

    return data


class ApifyPlugin(MCPPlugin):
    name = "apify"

    def __init__(self):
        self.tools = {
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts,
                description="List Apify accounts configured for the current gateway key.",
            ),
            "search_actors": ToolDef(
                access="read",
                handler=self.search_actors,
                description=(
                    "Search the Apify Store for Actors (scrapers/automation tools).\n"
                    "Params: search (query string), limit (default 20, max 100), account (optional)."
                ),
            ),
            "get_actor": ToolDef(
                access="read",
                handler=self.get_actor,
                description=(
                    "Get Actor details by ID (e.g. apify/web-scraper) or username~actor-name.\n"
                    "Params: actor_id (required), account (optional)."
                ),
            ),
            "run_actor": ToolDef(
                access="write",
                handler=self.run_actor,
                description=(
                    "Start an Actor run. Returns run metadata including dataset ID.\n"
                    "Params: actor_id (required), input_json (JSON string of Actor input, default {}), "
                    "wait_for_finish (bool, default false), timeout_secs (default 300), account (optional).\n"
                    "Counts against the gateway key's daily spend limit when the run completes."
                ),
            ),
            "get_run": ToolDef(
                access="read",
                handler=self.get_run,
                description=(
                    "Get Actor run status and usage/cost fields.\n"
                    "Params: run_id (required), account (optional)."
                ),
            ),
            "get_dataset_items": ToolDef(
                access="read",
                handler=self.get_dataset_items,
                description=(
                    "Fetch items from a dataset produced by an Actor run.\n"
                    "Params: dataset_id (required), limit (default 100, max 1000), offset (default 0), "
                    "format (json|csv, default json), account (optional)."
                ),
            ),
            "get_usage": ToolDef(
                access="read",
                handler=self.get_usage,
                description=(
                    "Get Apify account monthly usage summary from Apify API.\n"
                    "Params: account (optional)."
                ),
            ),
            "get_daily_spend": ToolDef(
                access="read",
                handler=self.get_daily_spend,
                description=(
                    "Get this gateway key's daily spend tracking (UTC day) and configured limit."
                ),
            ),
        }

    def list_accounts(self) -> dict:
        return {"accounts": _list_accounts()}

    def search_actors(self, search: str = "", limit: int = 20, account: str = "") -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        limit = max(1, min(limit, 100))
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return _req("GET", "/store", resolved["api_key"], params=params)

    def get_actor(self, actor_id: str, account: str = "") -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        actor_id = actor_id.strip().lstrip("/")
        return _req("GET", f"/acts/{actor_id}", resolved["api_key"])

    def run_actor(
        self,
        actor_id: str,
        input_json: str = "{}",
        wait_for_finish: bool = False,
        timeout_secs: int = 300,
        account: str = "",
    ) -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        try:
            actor_input = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid input_json: {exc}"}

        actor_id = actor_id.strip().lstrip("/")
        params = {}
        if wait_for_finish:
            params["waitForFinish"] = max(1, min(timeout_secs, 300))

        return _req(
            "POST",
            f"/acts/{actor_id}/runs",
            resolved["api_key"],
            params=params or None,
            body=actor_input,
            timeout=max(_TIMEOUT, timeout_secs + 30 if wait_for_finish else _TIMEOUT),
            check_spend=True,
        )

    def get_run(self, run_id: str, account: str = "") -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        return _req("GET", f"/actor-runs/{run_id.strip()}", resolved["api_key"])

    def get_dataset_items(
        self,
        dataset_id: str,
        limit: int = 100,
        offset: int = 0,
        format: str = "json",
        account: str = "",
    ) -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        fmt = "csv" if format.lower() == "csv" else "json"
        params = {"limit": limit, "offset": offset, "format": fmt}
        return _req("GET", f"/datasets/{dataset_id.strip()}/items", resolved["api_key"], params=params)

    def get_usage(self, account: str = "") -> dict:
        resolved = _resolve(account)
        if "error" in resolved:
            return resolved
        return _req("GET", "/users/me/usage/monthly", resolved["api_key"])

    def get_daily_spend(self) -> dict:
        try:
            ctx = get_context()
        except RuntimeError:
            return {"error": "No request context available."}
        return auth.get_daily_spend_status(ctx.key_id)
