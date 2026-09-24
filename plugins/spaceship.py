"""Spaceship domain registrar plugin for the MCP Gateway.

Full domain + DNS + contacts management via the Spaceship public API.
Multi-account via {account}.api_key + {account}.api_secret credentials.

Auth headers: X-API-Key + X-API-Secret
Base URL: https://spaceship.dev/api/v1
Docs: https://docs.spaceship.dev/
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE = "https://spaceship.dev/api/v1"
_TIMEOUT = 90.0


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("spaceship")
    except RuntimeError:
        return []
    accounts: set[str] = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".", 1)[0])
    if "api_key" in creds and "api_secret" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str = "") -> dict:
    try:
        creds = get_credentials("spaceship")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple Spaceship accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No Spaceship credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
        api_secret = creds.get("api_secret", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")
        api_secret = creds.get(f"{selected}.api_secret", "")

    if not api_key or not api_secret:
        return {
            "error": f"No Spaceship credentials for account '{selected}' (need api_key + api_secret).",
            "available_accounts": _list_accounts(),
        }
    return {"account": selected, "api_key": api_key, "api_secret": api_secret}


def _parse_json(value: str, label: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid JSON in {label}: {exc}"}


def _req(
    method: str,
    path: str,
    api_key: str,
    api_secret: str,
    *,
    params: dict | None = None,
    body: Any = None,
) -> dict:
    headers = {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = httpx.request(
            method,
            f"{_BASE}{path}",
            headers=headers,
            params=params,
            json=body,
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException:
        return {"error": f"Spaceship API timeout after {_TIMEOUT}s ({method} {path})"}
    except httpx.HTTPError as exc:
        return {"error": f"Spaceship API request failed: {exc}"}

    async_id = (
        resp.headers.get("spaceship-async-operationid")
        or resp.headers.get("Spaceship-Async-Operationid")
        or resp.headers.get("spaceship-async-operation-id")
    )

    if resp.status_code == 204:
        out: dict[str, Any] = {"success": True, "status_code": 204}
        if async_id:
            out["async_operation_id"] = async_id
        return out

    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": resp.text[:2000]}

    if resp.status_code == 202:
        out = data if isinstance(data, dict) else {"data": data}
        out = {**out, "status_code": 202, "accepted": True}
        if async_id:
            out["async_operation_id"] = async_id
            out["note"] = (
                "Async operation started. Poll with spaceship_get_async_operation "
                f"using operation_id={async_id}"
            )
        return out

    if resp.status_code >= 400:
        return {
            "error": f"Spaceship API HTTP {resp.status_code}",
            "details": data,
            "status_code": resp.status_code,
        }

    if isinstance(data, dict):
        if async_id:
            data = {**data, "async_operation_id": async_id}
        return data
    return {"data": data}


def _need(resolved: dict, domain: str = "") -> dict | None:
    if "error" in resolved:
        return resolved
    if domain is not None and domain != "" and not str(domain).strip():
        return {"error": "domain is required"}
    return None


class SpaceshipPlugin(MCPPlugin):
    name = "spaceship"

    def __init__(self):
        self.tools = {
            # ── meta ──────────────────────────────────────────────────────
            "list_accounts": ToolDef(
                access="read",
                handler=self.list_accounts,
                description="List configured Spaceship accounts for the current gateway key.",
            ),
            "get_async_operation": ToolDef(
                access="read",
                handler=self.get_async_operation,
                description=(
                    "Poll an async operation (registration, transfer, renew, etc.). "
                    "Pass operation_id from spaceship-async-operationid / async_operation_id."
                ),
            ),
            # ── domains ───────────────────────────────────────────────────
            "list_domains": ToolDef(
                access="read",
                handler=self.list_domains,
                description="List domains. Params: take (1-100, default 100), skip (default 0), order_by optional.",
            ),
            "get_domain": ToolDef(
                access="read",
                handler=self.get_domain,
                description="Get full details for a domain (contacts, NS, status, privacy, dates).",
            ),
            "check_availability": ToolDef(
                access="read",
                handler=self.check_availability,
                description=(
                    "Bulk check domain availability. "
                    "domains_json: JSON array of names, e.g. '[\"example.com\",\"example.io\"]'."
                ),
            ),
            "check_domain_available": ToolDef(
                access="read",
                handler=self.check_domain_available,
                description="Check availability of a single domain.",
            ),
            "register_domain": ToolDef(
                access="write",
                handler=self.register_domain,
                description=(
                    "BUY / register a domain (async — returns async_operation_id).\n"
                    "Requires domains:billing scope on the Spaceship API key.\n"
                    "Params:\n"
                    "- domain\n"
                    "- years (1-10, default 1)\n"
                    "- auto_renew (bool, default false)\n"
                    "- privacy_level (high|medium|low/contactForm etc., default high)\n"
                    "- contacts_json: {\"registrant\":\"ID\",\"admin\":\"ID\",\"tech\":\"ID\",\"billing\":\"ID\"}\n"
                    "  Get contact IDs via save_contact or from an existing domain's get_domain.\n"
                    "Poll get_async_operation until status=success."
                ),
            ),
            "renew_domain": ToolDef(
                access="write",
                handler=self.renew_domain,
                description="Renew a domain for N years (async). Params: domain, years (default 1).",
            ),
            "restore_domain": ToolDef(
                access="write",
                handler=self.restore_domain,
                description="Restore a domain in redemption (async).",
            ),
            "delete_domain": ToolDef(
                access="write",
                handler=self.delete_domain,
                description="Delete/cancel a domain (destructive).",
            ),
            "set_autorenew": ToolDef(
                access="write",
                handler=self.set_autorenew,
                description="Enable/disable auto-renew. Params: domain, enabled (bool).",
            ),
            "set_nameservers": ToolDef(
                access="write",
                handler=self.set_nameservers,
                description=(
                    "Set nameservers. nameservers_json: '[\"ns1...\",\"ns2...\"]'. "
                    "Uses provider=custom."
                ),
            ),
            "set_domain_contacts": ToolDef(
                access="write",
                handler=self.set_domain_contacts,
                description=(
                    "Update domain contacts. contacts_json must include registrant "
                    "(and optionally admin/tech/billing contact IDs)."
                ),
            ),
            "set_privacy": ToolDef(
                access="write",
                handler=self.set_privacy,
                description=(
                    "Update WHOIS privacy preference. "
                    "privacy_json e.g. '{\"level\":\"high\",\"userConsent\":true}'."
                ),
            ),
            "set_email_protection": ToolDef(
                access="write",
                handler=self.set_email_protection,
                description=(
                    "Update email protection preference. "
                    "preference_json per Spaceship API."
                ),
            ),
            # ── transfer ──────────────────────────────────────────────────
            "transfer_domain": ToolDef(
                access="write",
                handler=self.transfer_domain,
                description=(
                    "Request inbound domain transfer (async). "
                    "Requires auth_code + contacts_json (+ optional years/auto_renew/privacy)."
                ),
            ),
            "get_transfer": ToolDef(
                access="read",
                handler=self.get_transfer,
                description="Get inbound transfer status/details for a domain.",
            ),
            "get_auth_code": ToolDef(
                access="read",
                handler=self.get_auth_code,
                description="Get EPP/auth code for transferring a domain out.",
            ),
            "set_transfer_lock": ToolDef(
                access="write",
                handler=self.set_transfer_lock,
                description="Lock or unlock domain transfer. Params: domain, locked (bool).",
            ),
            # ── personal nameservers ──────────────────────────────────────
            "list_personal_nameservers": ToolDef(
                access="read",
                handler=self.list_personal_nameservers,
                description="List personal nameserver hosts configured on a domain.",
            ),
            "get_personal_nameserver": ToolDef(
                access="read",
                handler=self.get_personal_nameserver,
                description="Get a personal nameserver host config. Params: domain, host.",
            ),
            "set_personal_nameserver": ToolDef(
                access="write",
                handler=self.set_personal_nameserver,
                description=(
                    "Create/update personal nameserver host. "
                    "body_json: Spaceship personal NS payload (addresses etc.)."
                ),
            ),
            "delete_personal_nameserver": ToolDef(
                access="write",
                handler=self.delete_personal_nameserver,
                description="Delete a personal nameserver host. Params: domain, host.",
            ),
            # ── contacts ──────────────────────────────────────────────────
            "save_contact": ToolDef(
                access="write",
                handler=self.save_contact,
                description=(
                    "Create/save a contact; returns contactId for registration. "
                    "contact_json fields: firstName, lastName, email, address1, city, "
                    "country, stateProvince, postalCode, phone (+ optional org/fax/etc)."
                ),
            ),
            "get_contact": ToolDef(
                access="read",
                handler=self.get_contact,
                description="Read contact details by contact_id.",
            ),
            "save_contact_attributes": ToolDef(
                access="write",
                handler=self.save_contact_attributes,
                description="Save contact attributes (TLD-specific). attributes_json per Spaceship API.",
            ),
            "get_contact_attributes": ToolDef(
                access="read",
                handler=self.get_contact_attributes,
                description="Read contact attributes by contact_id.",
            ),
            # ── DNS ───────────────────────────────────────────────────────
            "get_dns_records": ToolDef(
                access="read",
                handler=self.get_dns_records,
                description=(
                    "List DNS resource records for a domain. "
                    "Params: domain, take (default 100), skip (default 0)."
                ),
            ),
            "set_dns_records": ToolDef(
                access="write",
                handler=self.set_dns_records,
                description=(
                    "Replace/save DNS records (PUT). records_json: JSON array of records "
                    "(type, name, ttl, + type fields like address/exchange/value). "
                    "force=true by default."
                ),
            ),
            "delete_dns_records": ToolDef(
                access="write",
                handler=self.delete_dns_records,
                description=(
                    "Delete matching DNS records. records_json: JSON array of record "
                    "selectors (type/name/address etc.)."
                ),
            ),
        }

    # ── helpers used by handlers ──────────────────────────────────────────

    def list_accounts(self) -> dict:
        return {"accounts": _list_accounts()}

    def get_async_operation(self, operation_id: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        oid = (operation_id or "").strip()
        if not oid:
            return {"error": "operation_id is required"}
        return _req("GET", f"/async-operations/{oid}", resolved["api_key"], resolved["api_secret"])

    def list_domains(
        self, take: int = 100, skip: int = 0, order_by: str = "", account: str = ""
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        params: dict[str, Any] = {"take": max(1, min(int(take), 100)), "skip": max(0, int(skip))}
        if order_by:
            params["orderBy"] = order_by
        return _req("GET", "/domains", resolved["api_key"], resolved["api_secret"], params=params)

    def get_domain(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def check_availability(self, domains_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        domains = _parse_json(domains_json, "domains_json")
        if isinstance(domains, dict) and "error" in domains:
            return domains
        if not isinstance(domains, list) or not domains:
            return {"error": "domains_json must be a non-empty JSON array of domain names"}
        return _req(
            "POST",
            "/domains/available",
            resolved["api_key"],
            resolved["api_secret"],
            body={"domains": domains},
        )

    def check_domain_available(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}/available",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def register_domain(
        self,
        domain: str,
        contacts_json: str,
        years: int = 1,
        auto_renew: bool = False,
        privacy_level: str = "high",
        privacy_json: str = "",
        account: str = "",
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        contacts = _parse_json(contacts_json, "contacts_json")
        if isinstance(contacts, dict) and "error" in contacts:
            return contacts
        if not isinstance(contacts, dict) or "registrant" not in contacts:
            return {"error": "contacts_json must be an object including at least registrant contact ID"}

        if privacy_json and privacy_json.strip():
            privacy = _parse_json(privacy_json, "privacy_json")
            if isinstance(privacy, dict) and "error" in privacy:
                return privacy
        else:
            privacy = {"level": privacy_level or "high", "userConsent": True}

        body = {
            "autoRenew": bool(auto_renew),
            "years": max(1, min(int(years), 10)),
            "privacyProtection": privacy,
            "contacts": contacts,
        }
        return _req(
            "POST",
            f"/domains/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
            body=body,
        )

    def renew_domain(self, domain: str, years: int = 1, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "POST",
            f"/domains/{domain.strip().lower()}/renew",
            resolved["api_key"],
            resolved["api_secret"],
            body={"years": max(1, min(int(years), 10))},
        )

    def restore_domain(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "POST",
            f"/domains/{domain.strip().lower()}/restore",
            resolved["api_key"],
            resolved["api_secret"],
            body={},
        )

    def delete_domain(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "DELETE",
            f"/domains/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def set_autorenew(self, domain: str, enabled: bool = True, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/autorenew",
            resolved["api_key"],
            resolved["api_secret"],
            body={"isEnabled": bool(enabled)},
        )

    def set_nameservers(self, domain: str, nameservers_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        nameservers = _parse_json(nameservers_json, "nameservers_json")
        if isinstance(nameservers, dict) and "error" in nameservers:
            return nameservers
        if not isinstance(nameservers, list) or not nameservers:
            return {"error": "nameservers_json must be a non-empty JSON array of NS hostnames"}
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/nameservers",
            resolved["api_key"],
            resolved["api_secret"],
            body={"provider": "custom", "hosts": nameservers},
        )

    def set_domain_contacts(self, domain: str, contacts_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        contacts = _parse_json(contacts_json, "contacts_json")
        if isinstance(contacts, dict) and "error" in contacts:
            return contacts
        if not isinstance(contacts, dict) or "registrant" not in contacts:
            return {"error": "contacts_json must include registrant"}
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/contacts",
            resolved["api_key"],
            resolved["api_secret"],
            body=contacts,
        )

    def set_privacy(self, domain: str, privacy_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        privacy = _parse_json(privacy_json, "privacy_json")
        if isinstance(privacy, dict) and "error" in privacy:
            return privacy
        if not isinstance(privacy, dict):
            return {"error": "privacy_json must be a JSON object"}
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/privacy/preference",
            resolved["api_key"],
            resolved["api_secret"],
            body=privacy,
        )

    def set_email_protection(self, domain: str, preference_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        pref = _parse_json(preference_json, "preference_json")
        if isinstance(pref, dict) and "error" in pref:
            return pref
        if not isinstance(pref, dict):
            return {"error": "preference_json must be a JSON object"}
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/privacy/email-protection-preference",
            resolved["api_key"],
            resolved["api_secret"],
            body=pref,
        )

    def transfer_domain(
        self,
        domain: str,
        auth_code: str,
        contacts_json: str,
        auto_renew: bool = False,
        privacy_level: str = "high",
        privacy_json: str = "",
        account: str = "",
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        if not (auth_code or "").strip():
            return {"error": "auth_code is required"}
        contacts = _parse_json(contacts_json, "contacts_json")
        if isinstance(contacts, dict) and "error" in contacts:
            return contacts
        if not isinstance(contacts, dict) or "registrant" not in contacts:
            return {"error": "contacts_json must include registrant"}
        if privacy_json and privacy_json.strip():
            privacy = _parse_json(privacy_json, "privacy_json")
            if isinstance(privacy, dict) and "error" in privacy:
                return privacy
        else:
            privacy = {"level": privacy_level or "high", "userConsent": True}
        body = {
            "autoRenew": bool(auto_renew),
            "privacyProtection": privacy,
            "contacts": contacts,
            "authCode": auth_code.strip(),
        }
        return _req(
            "POST",
            f"/domains/{domain.strip().lower()}/transfer",
            resolved["api_key"],
            resolved["api_secret"],
            body=body,
        )

    def get_transfer(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}/transfer",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def get_auth_code(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}/transfer/auth-code",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def set_transfer_lock(self, domain: str, locked: bool = True, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/transfer/lock",
            resolved["api_key"],
            resolved["api_secret"],
            body={"isLocked": bool(locked)},
        )

    def list_personal_nameservers(self, domain: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}/personal-nameservers",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def get_personal_nameserver(self, domain: str, host: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        if not (host or "").strip():
            return {"error": "host is required"}
        return _req(
            "GET",
            f"/domains/{domain.strip().lower()}/personal-nameservers/{host.strip()}",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def set_personal_nameserver(
        self, domain: str, host: str, body_json: str, account: str = ""
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        if not (host or "").strip():
            return {"error": "host is required"}
        body = _parse_json(body_json, "body_json")
        if isinstance(body, dict) and "error" in body:
            return body
        if not isinstance(body, dict):
            return {"error": "body_json must be a JSON object"}
        return _req(
            "PUT",
            f"/domains/{domain.strip().lower()}/personal-nameservers/{host.strip()}",
            resolved["api_key"],
            resolved["api_secret"],
            body=body,
        )

    def delete_personal_nameserver(self, domain: str, host: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        if not (host or "").strip():
            return {"error": "host is required"}
        return _req(
            "DELETE",
            f"/domains/{domain.strip().lower()}/personal-nameservers/{host.strip()}",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def save_contact(self, contact_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        contact = _parse_json(contact_json, "contact_json")
        if isinstance(contact, dict) and "error" in contact:
            return contact
        if not isinstance(contact, dict):
            return {"error": "contact_json must be a JSON object"}
        return _req("PUT", "/contacts", resolved["api_key"], resolved["api_secret"], body=contact)

    def get_contact(self, contact_id: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        cid = (contact_id or "").strip()
        if not cid:
            return {"error": "contact_id is required"}
        return _req("GET", f"/contacts/{cid}", resolved["api_key"], resolved["api_secret"])

    def save_contact_attributes(self, attributes_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        attrs = _parse_json(attributes_json, "attributes_json")
        if isinstance(attrs, dict) and "error" in attrs:
            return attrs
        if not isinstance(attrs, dict):
            return {"error": "attributes_json must be a JSON object"}
        return _req(
            "PUT",
            "/contacts/attributes",
            resolved["api_key"],
            resolved["api_secret"],
            body=attrs,
        )

    def get_contact_attributes(self, contact_id: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved)
        if err:
            return err
        cid = (contact_id or "").strip()
        if not cid:
            return {"error": "contact_id is required"}
        return _req(
            "GET",
            f"/contacts/attributes/{cid}",
            resolved["api_key"],
            resolved["api_secret"],
        )

    def get_dns_records(
        self, domain: str, take: int = 100, skip: int = 0, account: str = ""
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        return _req(
            "GET",
            f"/dns/records/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
            params={"take": max(1, min(int(take), 500)), "skip": max(0, int(skip))},
        )

    def set_dns_records(
        self, domain: str, records_json: str, force: bool = True, account: str = ""
    ) -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        records = _parse_json(records_json, "records_json")
        if isinstance(records, dict) and "error" in records:
            return records
        if not isinstance(records, list):
            return {"error": "records_json must be a JSON array of DNS record objects"}
        return _req(
            "PUT",
            f"/dns/records/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
            body={"force": bool(force), "items": records},
        )

    def delete_dns_records(self, domain: str, records_json: str, account: str = "") -> dict:
        resolved = _resolve(account)
        err = _need(resolved, domain)
        if err:
            return err
        records = _parse_json(records_json, "records_json")
        if isinstance(records, dict) and "error" in records:
            return records
        if not isinstance(records, list) or not records:
            return {"error": "records_json must be a non-empty JSON array"}
        return _req(
            "DELETE",
            f"/dns/records/{domain.strip().lower()}",
            resolved["api_key"],
            resolved["api_secret"],
            body=records,
        )
