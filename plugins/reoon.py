"""Reoon Email Verifier plugin for the MCP Gateway.

Verify single emails (quick/power mode), run bulk verification tasks,
and check account balance. Multi-account support via {account}.api_key.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE_URL = "https://emailverifier.reoon.com/api/v1"


def _list_reoon_accounts() -> list[str]:
    try:
        creds = get_credentials("reoon")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _get_reoon_key(account: str = "") -> dict:
    try:
        creds = get_credentials("reoon")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_reoon_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Reoon accounts configured. Specify the `account` parameter.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Reoon credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No Reoon credentials found for account '{selected}'.",
            "available_accounts": _list_reoon_accounts(),
        }

    return {"account": selected, "api_key": api_key}


class ReoonPlugin(MCPPlugin):
    name = "reoon"

    def __init__(self):
        self.tools = {
            "verify_email": ToolDef(
                access="write",
                handler=self.verify_email,
                description=(
                    "Verify a single email address. Two modes:\n"
                    "- quick: <0.5s, checks syntax/disposable/MX/domain (no inbox check)\n"
                    "- power: deep SMTP verification, checks inbox existence, catch-all, spamtrap\n\n"
                    "Statuses (quick): valid, invalid, disposable, spamtrap\n"
                    "Statuses (power): safe, invalid, disabled, disposable, inbox_full, "
                    "catch_all, role_account, spamtrap, unknown"
                ),
            ),
            "bulk_verify": ToolDef(
                access="write",
                handler=self.bulk_verify,
                description=(
                    "Create a bulk email verification task. Submit up to 50,000 emails.\n"
                    "All emails verified in power mode. Returns a task_id to poll results.\n"
                    "Use get_bulk_results with the task_id to check progress and download results."
                ),
            ),
            "get_bulk_results": ToolDef(
                access="read",
                handler=self.get_bulk_results,
                description=(
                    "Get results of a bulk verification task. Returns progress and, when completed,\n"
                    "full verification results for every email. Poll until status is 'completed'."
                ),
            ),
            "check_balance": ToolDef(
                access="read",
                handler=self.check_balance,
                description="Check remaining daily and instant verification credits.",
            ),
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts,
                description="List all configured Reoon accounts for the current key.",
            ),
        }

    def verify_email(self, email: str, mode: str = "power", account: str = "") -> dict:
        resolved = _get_reoon_key(account)
        if "error" in resolved:
            return resolved

        try:
            resp = httpx.get(
                f"{_BASE_URL}/verify",
                params={"email": email, "key": resolved["api_key"], "mode": mode},
                timeout=90.0,
            )
        except httpx.TimeoutException:
            return {"error": "Verification timed out (90s). Power mode can be slow for some providers."}
        except httpx.RequestError as exc:
            return {"error": f"Request failed: {exc}"}

        if resp.status_code == 429:
            return {"error": "Rate limited. Wait a few seconds and retry."}
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}

        try:
            return resp.json()
        except Exception:
            return {"data": resp.text}

    def bulk_verify(self, emails: str, name: str = "Gateway bulk task", account: str = "") -> dict:
        resolved = _get_reoon_key(account)
        if "error" in resolved:
            return resolved

        if isinstance(emails, str):
            try:
                email_list = json.loads(emails)
            except json.JSONDecodeError:
                email_list = [e.strip() for e in emails.split(",") if e.strip()]
        else:
            email_list = emails

        if not email_list:
            return {"error": "No emails provided. Pass a JSON array or comma-separated list."}
        if len(email_list) < 10:
            return {"error": "Bulk endpoint requires at least 10 emails. Use verify_email for smaller lists."}

        try:
            resp = httpx.post(
                f"{_BASE_URL}/create-bulk-verification-task/",
                json={"name": name[:25], "emails": email_list, "key": resolved["api_key"]},
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            return {"error": f"Request failed: {exc}"}

        if resp.status_code == 201:
            result = resp.json()
            result["_hint"] = f"Use get_bulk_results(task_id={result.get('task_id')}) to poll for results."
            return result
        if resp.status_code >= 400:
            try:
                return resp.json()
            except Exception:
                return {"error": f"HTTP {resp.status_code}", "details": resp.text}

        try:
            return resp.json()
        except Exception:
            return {"data": resp.text}

    def get_bulk_results(self, task_id: str, account: str = "") -> dict:
        resolved = _get_reoon_key(account)
        if "error" in resolved:
            return resolved

        try:
            resp = httpx.get(
                f"{_BASE_URL}/get-result-bulk-verification-task/",
                params={"key": resolved["api_key"], "task-id": task_id},
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            return {"error": f"Request failed: {exc}"}

        if resp.status_code >= 400:
            try:
                return resp.json()
            except Exception:
                return {"error": f"HTTP {resp.status_code}", "details": resp.text}

        try:
            return resp.json()
        except Exception:
            return {"data": resp.text}

    def check_balance(self, account: str = "") -> dict:
        resolved = _get_reoon_key(account)
        if "error" in resolved:
            return resolved

        try:
            resp = httpx.get(
                f"{_BASE_URL}/check-account-balance/",
                params={"key": resolved["api_key"]},
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            return {"error": f"Request failed: {exc}"}

        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}

        try:
            return resp.json()
        except Exception:
            return {"data": resp.text}

    def list_accounts(self) -> dict:
        accounts = _list_reoon_accounts()
        return {"accounts": accounts}

    def health_check(self) -> dict[str, Any]:
        try:
            resolved = _get_reoon_key()
            if "error" in resolved:
                return {"status": "no_credentials", "detail": resolved["error"]}
            resp = httpx.get(
                f"{_BASE_URL}/check-account-balance/",
                params={"key": resolved["api_key"]},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
