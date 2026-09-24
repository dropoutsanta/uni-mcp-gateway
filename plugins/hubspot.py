"""HubSpot CRM plugin for the MCP Gateway.

Comprehensive integration covering CRM objects (contacts, companies, deals,
tickets, line items, products, notes, calls, emails, meetings, tasks, leads),
associations, properties, pipelines, owners, lists, search, imports/exports,
workflows, forms, files, and account info. Multi-account via {account}.refresh_token.

Auth uses HubSpot's local dev auth endpoint to exchange a long-lived OAuth
refresh token for a short-lived access token, cached until expiry.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE = "https://api.hubapi.com"
_REFRESH_URL = "https://api.hubapi.com/localdevauth/v1/auth/refresh"

_token_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Multi-account helpers
# ---------------------------------------------------------------------------

def _list_hubspot_accounts() -> list[str]:
    try:
        creds = get_credentials("hubspot")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "refresh_token" in creds or "pat" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str = "") -> dict:
    """Resolve account and return a valid Bearer access token.

    Supports two auth modes per account:
      - PAT (private app token): {account}.pat  → used directly, no refresh needed
      - OAuth refresh token:     {account}.refresh_token → exchanged via localdevauth
    """
    try:
        creds = get_credentials("hubspot")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_hubspot_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple HubSpot accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No HubSpot credentials configured for this key."}

    # Try PAT first (no refresh needed)
    if selected == "default":
        pat = creds.get("pat", "")
        refresh_token = creds.get("refresh_token", "")
    else:
        pat = creds.get(f"{selected}.pat", "")
        refresh_token = creds.get(f"{selected}.refresh_token", "")

    if pat:
        return {"account": selected, "api_key": pat}

    if not refresh_token:
        return {
            "error": f"No HubSpot credentials for account '{selected}'.",
            "available_accounts": _list_hubspot_accounts(),
        }

    access_token = _get_access_token(selected, refresh_token)
    if not access_token:
        return {"error": f"Failed to exchange refresh token for account '{selected}'."}

    return {"account": selected, "api_key": access_token}


def _get_access_token(account: str, refresh_token: str) -> str | None:
    """Exchange refresh token for access token, with caching."""
    cached = _token_cache.get(account)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at - 60:
            return token

    try:
        resp = httpx.post(
            _REFRESH_URL,
            json={"encodedOAuthRefreshToken": refresh_token},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        access_token = data.get("oauthAccessToken", "")
        expires_ms = data.get("expiresAtMillis", 0)
        if access_token and expires_ms:
            _token_cache[account] = (access_token, expires_ms / 1000.0)
        return access_token or None
    except Exception:
        return None


def _req(method: str, path: str, token: str, *,
         params: dict | None = None,
         body: dict | None = None,
         timeout: float = 30.0) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = httpx.request(method, f"{_BASE}{path}",
                             headers=headers, params=params,
                             json=body, timeout=timeout)
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 204:
        return {"success": True}
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        return {"error": f"Rate limited. Retry after {retry}s."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}
    try:
        return resp.json()
    except Exception:
        return {"data": resp.text}


# ---------------------------------------------------------------------------
# Helpers for building filter JSON from strings
# ---------------------------------------------------------------------------

def _parse_props(properties: str) -> list[str] | None:
    if not properties:
        return None
    return [p.strip() for p in properties.split(",") if p.strip()]


def _build_params(properties: str = "", limit: int = 0, after: str = "",
                  archived: bool = False, associations: str = "",
                  **extra: Any) -> dict:
    p: dict[str, Any] = {}
    if properties:
        p["properties"] = properties
    if limit:
        p["limit"] = limit
    if after:
        p["after"] = after
    if archived:
        p["archived"] = "true"
    if associations:
        p["associations"] = associations
    for k, v in extra.items():
        if v not in (None, "", 0):
            p[k] = v
    return p


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class HubSpotPlugin(MCPPlugin):
    name = "hubspot"

    def __init__(self):
        self.tools: dict[str, ToolDef] = {}

        # ---- CRM OBJECTS (generic) ---- #
        _obj_types = (
            "contacts", "companies", "deals", "tickets",
            "products", "line_items", "quotes",
            "calls", "emails", "meetings", "notes", "tasks",
            "leads", "invoices", "orders", "feedback_submissions",
        )
        for obj in _obj_types:
            slug = obj.replace(" ", "_")
            api_obj = obj.replace("_", " ").replace(" ", "_")
            # Some object endpoints use hyphens in the URL
            url_obj = obj.replace("_", "_")  # keep underscores; HubSpot v3 uses the name as-is

            self.tools[f"{slug}_list"] = ToolDef(
                access="read",
                handler=self._make_list(obj),
                description=f"List {obj}. Optional: properties (comma-sep), limit (max 100), after (cursor), associations (comma-sep object types), archived.",
            )
            self.tools[f"{slug}_get"] = ToolDef(
                access="read",
                handler=self._make_get(obj),
                description=f"Get a single {obj.rstrip('s')} by ID. Optional: properties (comma-sep), associations.",
            )
            self.tools[f"{slug}_create"] = ToolDef(
                access="write",
                handler=self._make_create(obj),
                description=f"Create a {obj.rstrip('s')}. Pass properties as JSON object string, e.g. '{{\"email\": \"a@b.com\", \"firstname\": \"Al\"}}'.",
            )
            self.tools[f"{slug}_update"] = ToolDef(
                access="write",
                handler=self._make_update(obj),
                description=f"Update a {obj.rstrip('s')} by ID. Pass properties as JSON object string.",
            )
            self.tools[f"{slug}_delete"] = ToolDef(
                access="write",
                handler=self._make_delete(obj),
                description=f"Archive (soft-delete) a {obj.rstrip('s')} by ID.",
            )
            self.tools[f"{slug}_search"] = ToolDef(
                access="read",
                handler=self._make_search(obj),
                description=(
                    f"Search {obj}. Pass a search body as JSON string with filterGroups, sorts, properties, limit, after.\n"
                    f"Example: '{{\"filterGroups\":[{{\"filters\":[{{\"propertyName\":\"email\",\"operator\":\"EQ\",\"value\":\"a@b.com\"}}]}}],\"limit\":10}}'\n"
                    f"Operators: EQ, NEQ, LT, LTE, GT, GTE, BETWEEN, IN, NOT_IN, HAS_PROPERTY, NOT_HAS_PROPERTY, CONTAINS_TOKEN, NOT_CONTAINS_TOKEN."
                ),
            )
            self.tools[f"{slug}_batch_create"] = ToolDef(
                access="write",
                handler=self._make_batch_create(obj),
                description=f"Batch create {obj}. Pass inputs as JSON array of objects, each with a 'properties' key.",
            )
            self.tools[f"{slug}_batch_update"] = ToolDef(
                access="write",
                handler=self._make_batch_update(obj),
                description=f"Batch update {obj}. Pass inputs as JSON array of objects, each with 'id' and 'properties' keys.",
            )
            self.tools[f"{slug}_batch_read"] = ToolDef(
                access="read",
                handler=self._make_batch_read(obj),
                description=f"Batch read {obj} by IDs. Pass ids as comma-separated string. Optional: properties (comma-sep).",
            )

        # ---- ASSOCIATIONS ---- #
        self.tools["associations_list"] = ToolDef(
            access="read",
            handler=self.associations_list,
            description=(
                "List associations between two object types for a specific record.\n"
                "Params: from_object_type, object_id, to_object_type. Optional: after (cursor), limit."
            ),
        )
        self.tools["associations_create"] = ToolDef(
            access="write",
            handler=self.associations_create,
            description=(
                "Create an association between two records.\n"
                "Params: from_object_type, from_object_id, to_object_type, to_object_id, "
                "association_type_id (int). Optional: association_category (HUBSPOT_DEFINED | USER_DEFINED)."
            ),
        )
        self.tools["associations_delete"] = ToolDef(
            access="write",
            handler=self.associations_delete,
            description=(
                "Remove an association between two records.\n"
                "Params: from_object_type, from_object_id, to_object_type, to_object_id."
            ),
        )
        self.tools["association_types_list"] = ToolDef(
            access="read",
            handler=self.association_types_list,
            description="List available association types between two object types. Params: from_object_type, to_object_type.",
        )

        # ---- PROPERTIES ---- #
        self.tools["properties_list"] = ToolDef(
            access="read",
            handler=self.properties_list,
            description="List all properties for an object type. Params: object_type (e.g. contacts, deals).",
        )
        self.tools["properties_get"] = ToolDef(
            access="read",
            handler=self.properties_get,
            description="Get a single property. Params: object_type, property_name.",
        )
        self.tools["properties_create"] = ToolDef(
            access="write",
            handler=self.properties_create,
            description=(
                "Create a property. Params: object_type, body_json (JSON string with name, label, type, fieldType, groupName).\n"
                "Types: string, number, date, datetime, enumeration, bool. "
                "Field types: text, textarea, number, date, file, checkbox, select, radio, booleancheckbox."
            ),
        )
        self.tools["properties_update"] = ToolDef(
            access="write",
            handler=self.properties_update,
            description="Update a property. Params: object_type, property_name, body_json (JSON with fields to update).",
        )
        self.tools["properties_delete"] = ToolDef(
            access="write",
            handler=self.properties_delete,
            description="Delete a property. Params: object_type, property_name.",
        )
        self.tools["property_groups_list"] = ToolDef(
            access="read",
            handler=self.property_groups_list,
            description="List property groups for an object type. Params: object_type.",
        )

        # ---- PIPELINES ---- #
        self.tools["pipelines_list"] = ToolDef(
            access="read",
            handler=self.pipelines_list,
            description="List pipelines for an object type (deals or tickets). Params: object_type.",
        )
        self.tools["pipelines_get"] = ToolDef(
            access="read",
            handler=self.pipelines_get,
            description="Get a pipeline by ID. Params: object_type, pipeline_id.",
        )
        self.tools["pipelines_create"] = ToolDef(
            access="write",
            handler=self.pipelines_create,
            description="Create a pipeline. Params: object_type, body_json (JSON with label, displayOrder, stages array).",
        )
        self.tools["pipelines_update"] = ToolDef(
            access="write",
            handler=self.pipelines_update,
            description="Update a pipeline. Params: object_type, pipeline_id, body_json.",
        )
        self.tools["pipelines_delete"] = ToolDef(
            access="write",
            handler=self.pipelines_delete,
            description="Delete a pipeline. Params: object_type, pipeline_id.",
        )
        self.tools["pipeline_stages_list"] = ToolDef(
            access="read",
            handler=self.pipeline_stages_list,
            description="List stages for a pipeline. Params: object_type, pipeline_id.",
        )
        self.tools["pipeline_stages_create"] = ToolDef(
            access="write",
            handler=self.pipeline_stages_create,
            description="Create a stage in a pipeline. Params: object_type, pipeline_id, body_json (label, displayOrder, metadata).",
        )

        # ---- OWNERS ---- #
        self.tools["owners_list"] = ToolDef(
            access="read",
            handler=self.owners_list,
            description="List all owners (users assignable to CRM records). Optional: email filter, after cursor, limit, archived.",
        )
        self.tools["owners_get"] = ToolDef(
            access="read",
            handler=self.owners_get,
            description="Get an owner by ID. Params: owner_id.",
        )

        # ---- LISTS ---- #
        self.tools["lists_list"] = ToolDef(
            access="read",
            handler=self.lists_list,
            description="List all CRM lists. Optional: after (cursor), limit.",
        )
        self.tools["lists_get"] = ToolDef(
            access="read",
            handler=self.lists_get,
            description="Get a list by ID. Params: list_id.",
        )
        self.tools["lists_create"] = ToolDef(
            access="write",
            handler=self.lists_create,
            description="Create a list. Params: body_json (JSON with name, objectTypeId, processingType: MANUAL|DYNAMIC, filterBranch for dynamic).",
        )
        self.tools["lists_delete"] = ToolDef(
            access="write",
            handler=self.lists_delete,
            description="Delete a list by ID. Params: list_id.",
        )
        self.tools["list_memberships_get"] = ToolDef(
            access="read",
            handler=self.list_memberships_get,
            description="Get records in a list. Params: list_id. Optional: after, limit.",
        )
        self.tools["list_memberships_add"] = ToolDef(
            access="write",
            handler=self.list_memberships_add,
            description="Add records to a static list. Params: list_id, record_ids (comma-sep).",
        )
        self.tools["list_memberships_remove"] = ToolDef(
            access="write",
            handler=self.list_memberships_remove,
            description="Remove records from a static list. Params: list_id, record_ids (comma-sep).",
        )

        # ---- IMPORTS / EXPORTS ---- #
        self.tools["imports_list"] = ToolDef(
            access="read",
            handler=self.imports_list,
            description="List CRM imports. Optional: after, limit.",
        )
        self.tools["imports_get"] = ToolDef(
            access="read",
            handler=self.imports_get,
            description="Get import status by ID. Params: import_id.",
        )
        self.tools["imports_cancel"] = ToolDef(
            access="write",
            handler=self.imports_cancel,
            description="Cancel an active import. Params: import_id.",
        )

        # ---- WORKFLOWS ---- #
        self.tools["workflows_list"] = ToolDef(
            access="read",
            handler=self.workflows_list,
            description="List all workflows.",
        )
        self.tools["workflows_get"] = ToolDef(
            access="read",
            handler=self.workflows_get,
            description="Get a workflow by ID. Params: workflow_id.",
        )

        # ---- FORMS ---- #
        self.tools["forms_list"] = ToolDef(
            access="read",
            handler=self.forms_list,
            description="List marketing forms. Optional: after, limit.",
        )
        self.tools["forms_get"] = ToolDef(
            access="read",
            handler=self.forms_get,
            description="Get a form by ID. Params: form_id.",
        )
        self.tools["form_submissions_list"] = ToolDef(
            access="read",
            handler=self.form_submissions_list,
            description="List submissions for a form. Params: form_id. Optional: after, limit.",
        )

        # ---- FILES ---- #
        self.tools["files_search"] = ToolDef(
            access="read",
            handler=self.files_search,
            description="Search files in the file manager. Optional: query, after, limit.",
        )
        self.tools["files_get"] = ToolDef(
            access="read",
            handler=self.files_get,
            description="Get file metadata by ID. Params: file_id.",
        )
        self.tools["folders_list"] = ToolDef(
            access="read",
            handler=self.folders_list,
            description="List folders in the file manager. Optional: parent_folder_id, after, limit.",
        )

        # ---- COMMUNICATION PREFERENCES ---- #
        self.tools["subscriptions_list"] = ToolDef(
            access="read",
            handler=self.subscriptions_list,
            description="List email subscription types/definitions.",
        )
        self.tools["subscription_status_get"] = ToolDef(
            access="read",
            handler=self.subscription_status_get,
            description="Get subscription status for an email. Params: email_address.",
        )

        # ---- ACCOUNT INFO ---- #
        self.tools["account_info"] = ToolDef(
            access="read",
            handler=self.account_info,
            description="Get account info (portal ID, time zone, currency, etc.) for the authenticated HubSpot account.",
        )

        # ---- CUSTOM OBJECTS SCHEMA ---- #
        self.tools["custom_objects_schemas_list"] = ToolDef(
            access="read",
            handler=self.custom_objects_schemas_list,
            description="List all custom object schemas.",
        )
        self.tools["custom_objects_schema_get"] = ToolDef(
            access="read",
            handler=self.custom_objects_schema_get,
            description="Get a custom object schema by objectType or fullyQualifiedName. Params: object_type.",
        )

        # ---- ANALYTICS ---- #
        self.tools["analytics_reports"] = ToolDef(
            access="read",
            handler=self.analytics_reports,
            description=(
                "Get analytics data. Params: report_type (e.g. totals/summarize/daily), "
                "object_type (e.g. landing-pages, standard-pages, blog-posts, knowledge-articles), "
                "time_period (e.g. total, weekly, monthly, daily). Optional: start, end (dates YYYYMMDD)."
            ),
        )

        # ---- META ---- #
        self.tools["list_accounts"] = ToolDef(
            access="read",
            handler=self.list_accounts_handler,
            description="List configured HubSpot accounts for the current key.",
        )
        self.tools["token_scopes"] = ToolDef(
            access="read",
            handler=self.token_scopes,
            description="Introspect the current access token to see which OAuth scopes it has.",
        )

    # =====================================================================
    # FACTORY METHODS for CRM object CRUD
    # =====================================================================

    def _make_list(self, obj: str):
        def handler(properties: str = "", limit: int = 10, after: str = "",
                    associations: str = "", archived: bool = False,
                    account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            p = _build_params(properties, limit, after, archived, associations)
            return _req("GET", f"/crm/v3/objects/{obj}", r["api_key"], params=p)
        return handler

    def _make_get(self, obj: str):
        def handler(object_id: str, properties: str = "", associations: str = "",
                    account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            p = _build_params(properties, associations=associations)
            return _req("GET", f"/crm/v3/objects/{obj}/{object_id}", r["api_key"], params=p)
        return handler

    def _make_create(self, obj: str):
        def handler(properties_json: str, associations_json: str = "",
                    account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            try:
                props = json.loads(properties_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid properties JSON: {e}"}
            body: dict[str, Any] = {"properties": props}
            if associations_json:
                try:
                    body["associations"] = json.loads(associations_json)
                except json.JSONDecodeError:
                    pass
            return _req("POST", f"/crm/v3/objects/{obj}", r["api_key"], body=body)
        return handler

    def _make_update(self, obj: str):
        def handler(object_id: str, properties_json: str,
                    account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            try:
                props = json.loads(properties_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid properties JSON: {e}"}
            return _req("PATCH", f"/crm/v3/objects/{obj}/{object_id}", r["api_key"],
                        body={"properties": props})
        return handler

    def _make_delete(self, obj: str):
        def handler(object_id: str, account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            return _req("DELETE", f"/crm/v3/objects/{obj}/{object_id}", r["api_key"])
        return handler

    def _make_search(self, obj: str):
        def handler(search_json: str, account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            try:
                body = json.loads(search_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid search JSON: {e}"}
            return _req("POST", f"/crm/v3/objects/{obj}/search", r["api_key"],
                        body=body, timeout=30.0)
        return handler

    def _make_batch_create(self, obj: str):
        def handler(inputs_json: str, account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            try:
                inputs = json.loads(inputs_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid JSON: {e}"}
            return _req("POST", f"/crm/v3/objects/{obj}/batch/create", r["api_key"],
                        body={"inputs": inputs}, timeout=60.0)
        return handler

    def _make_batch_update(self, obj: str):
        def handler(inputs_json: str, account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            try:
                inputs = json.loads(inputs_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid JSON: {e}"}
            return _req("POST", f"/crm/v3/objects/{obj}/batch/update", r["api_key"],
                        body={"inputs": inputs}, timeout=60.0)
        return handler

    def _make_batch_read(self, obj: str):
        def handler(ids: str, properties: str = "", account: str = "") -> dict:
            r = _resolve(account)
            if "error" in r:
                return r
            id_list = [{"id": i.strip()} for i in ids.split(",") if i.strip()]
            body: dict[str, Any] = {"inputs": id_list}
            if properties:
                body["properties"] = _parse_props(properties)
            return _req("POST", f"/crm/v3/objects/{obj}/batch/read", r["api_key"],
                        body=body, timeout=60.0)
        return handler

    # =====================================================================
    # ASSOCIATIONS
    # =====================================================================

    def associations_list(self, from_object_type: str, object_id: str,
                          to_object_type: str, after: str = "", limit: int = 100,
                          account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p: dict[str, Any] = {}
        if after:
            p["after"] = after
        if limit:
            p["limit"] = limit
        return _req("GET",
                     f"/crm/v4/objects/{from_object_type}/{object_id}/associations/{to_object_type}",
                     r["api_key"], params=p)

    def associations_create(self, from_object_type: str, from_object_id: str,
                            to_object_type: str, to_object_id: str,
                            association_type_id: int,
                            association_category: str = "HUBSPOT_DEFINED",
                            account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body = [{"associationCategory": association_category,
                 "associationTypeId": association_type_id}]
        return _req("PUT",
                     f"/crm/v4/objects/{from_object_type}/{from_object_id}/associations/{to_object_type}/{to_object_id}",
                     r["api_key"], body=body)

    def associations_delete(self, from_object_type: str, from_object_id: str,
                            to_object_type: str, to_object_id: str,
                            account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("DELETE",
                     f"/crm/v4/objects/{from_object_type}/{from_object_id}/associations/{to_object_type}/{to_object_id}",
                     r["api_key"])

    def association_types_list(self, from_object_type: str, to_object_type: str,
                               account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET",
                     f"/crm/v4/associations/{from_object_type}/{to_object_type}/labels",
                     r["api_key"])

    # =====================================================================
    # PROPERTIES
    # =====================================================================

    def properties_list(self, object_type: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/properties/{object_type}", r["api_key"])

    def properties_get(self, object_type: str, property_name: str,
                       account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/properties/{object_type}/{property_name}", r["api_key"])

    def properties_create(self, object_type: str, body_json: str,
                          account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("POST", f"/crm/v3/properties/{object_type}", r["api_key"], body=body)

    def properties_update(self, object_type: str, property_name: str,
                          body_json: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("PATCH", f"/crm/v3/properties/{object_type}/{property_name}",
                     r["api_key"], body=body)

    def properties_delete(self, object_type: str, property_name: str,
                          account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("DELETE", f"/crm/v3/properties/{object_type}/{property_name}",
                     r["api_key"])

    def property_groups_list(self, object_type: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/properties/{object_type}/groups", r["api_key"])

    # =====================================================================
    # PIPELINES
    # =====================================================================

    def pipelines_list(self, object_type: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/pipelines/{object_type}", r["api_key"])

    def pipelines_get(self, object_type: str, pipeline_id: str,
                      account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/pipelines/{object_type}/{pipeline_id}", r["api_key"])

    def pipelines_create(self, object_type: str, body_json: str,
                         account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("POST", f"/crm/v3/pipelines/{object_type}", r["api_key"], body=body)

    def pipelines_update(self, object_type: str, pipeline_id: str,
                         body_json: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("PATCH", f"/crm/v3/pipelines/{object_type}/{pipeline_id}",
                     r["api_key"], body=body)

    def pipelines_delete(self, object_type: str, pipeline_id: str,
                         account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("DELETE", f"/crm/v3/pipelines/{object_type}/{pipeline_id}",
                     r["api_key"])

    def pipeline_stages_list(self, object_type: str, pipeline_id: str,
                             account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/pipelines/{object_type}/{pipeline_id}/stages",
                     r["api_key"])

    def pipeline_stages_create(self, object_type: str, pipeline_id: str,
                               body_json: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("POST", f"/crm/v3/pipelines/{object_type}/{pipeline_id}/stages",
                     r["api_key"], body=body)

    # =====================================================================
    # OWNERS
    # =====================================================================

    def owners_list(self, email: str = "", after: str = "", limit: int = 100,
                    archived: bool = False, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p: dict[str, Any] = {}
        if email:
            p["email"] = email
        if after:
            p["after"] = after
        if limit:
            p["limit"] = limit
        if archived:
            p["archived"] = "true"
        return _req("GET", "/crm/v3/owners/", r["api_key"], params=p)

    def owners_get(self, owner_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/owners/{owner_id}", r["api_key"])

    # =====================================================================
    # LISTS
    # =====================================================================

    def lists_list(self, after: str = "", limit: int = 25, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p = _build_params(limit=limit, after=after)
        return _req("GET", "/crm/v3/lists/", r["api_key"], params=p)

    def lists_get(self, list_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/lists/{list_id}", r["api_key"])

    def lists_create(self, body_json: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}"}
        return _req("POST", "/crm/v3/lists/", r["api_key"], body=body)

    def lists_delete(self, list_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("DELETE", f"/crm/v3/lists/{list_id}", r["api_key"])

    def list_memberships_get(self, list_id: str, after: str = "", limit: int = 100,
                             account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p = _build_params(limit=limit, after=after)
        return _req("GET", f"/crm/v3/lists/{list_id}/memberships", r["api_key"], params=p)

    def list_memberships_add(self, list_id: str, record_ids: str,
                             account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        ids = [i.strip() for i in record_ids.split(",") if i.strip()]
        return _req("PUT", f"/crm/v3/lists/{list_id}/memberships/add", r["api_key"],
                     body={"recordIdsToAdd": ids})

    def list_memberships_remove(self, list_id: str, record_ids: str,
                                account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        ids = [i.strip() for i in record_ids.split(",") if i.strip()]
        return _req("PUT", f"/crm/v3/lists/{list_id}/memberships/remove", r["api_key"],
                     body={"recordIdsToRemove": ids})

    # =====================================================================
    # IMPORTS / EXPORTS
    # =====================================================================

    def imports_list(self, after: str = "", limit: int = 20,
                     account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p = _build_params(limit=limit, after=after)
        return _req("GET", "/crm/v3/imports/", r["api_key"], params=p)

    def imports_get(self, import_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/imports/{import_id}", r["api_key"])

    def imports_cancel(self, import_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("POST", f"/crm/v3/imports/{import_id}/cancel", r["api_key"])

    # =====================================================================
    # WORKFLOWS
    # =====================================================================

    def workflows_list(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", "/automation/v3/workflows/", r["api_key"])

    def workflows_get(self, workflow_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/automation/v3/workflows/{workflow_id}", r["api_key"])

    # =====================================================================
    # FORMS
    # =====================================================================

    def forms_list(self, after: str = "", limit: int = 20,
                   account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p = _build_params(limit=limit, after=after)
        return _req("GET", "/marketing/v3/forms/", r["api_key"], params=p)

    def forms_get(self, form_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/marketing/v3/forms/{form_id}", r["api_key"])

    def form_submissions_list(self, form_id: str, after: str = "",
                              limit: int = 20, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p = _build_params(limit=limit, after=after)
        return _req("GET", f"/form-integrations/v1/submissions/forms/{form_id}",
                     r["api_key"], params=p)

    # =====================================================================
    # FILES
    # =====================================================================

    def files_search(self, query: str = "", after: str = "", limit: int = 20,
                     account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p: dict[str, Any] = {}
        if query:
            p["name"] = query
        if after:
            p["after"] = after
        if limit:
            p["limit"] = limit
        return _req("GET", "/files/v3/files", r["api_key"], params=p)

    def files_get(self, file_id: str, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/files/v3/files/{file_id}", r["api_key"])

    def folders_list(self, parent_folder_id: str = "", after: str = "",
                     limit: int = 20, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p: dict[str, Any] = {}
        if parent_folder_id:
            p["parentFolderId"] = parent_folder_id
        if after:
            p["after"] = after
        if limit:
            p["limit"] = limit
        return _req("GET", "/files/v3/files/search", r["api_key"], params=p)

    # =====================================================================
    # COMMUNICATION PREFERENCES
    # =====================================================================

    def subscriptions_list(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", "/communication-preferences/v3/definitions", r["api_key"])

    def subscription_status_get(self, email_address: str,
                                account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/communication-preferences/v3/status/email/{email_address}",
                     r["api_key"])

    # =====================================================================
    # ACCOUNT INFO
    # =====================================================================

    def account_info(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", "/integrations/v1/me", r["api_key"])

    # =====================================================================
    # CUSTOM OBJECTS SCHEMA
    # =====================================================================

    def custom_objects_schemas_list(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", "/crm/v3/schemas", r["api_key"])

    def custom_objects_schema_get(self, object_type: str,
                                 account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        return _req("GET", f"/crm/v3/schemas/{object_type}", r["api_key"])

    # =====================================================================
    # ANALYTICS
    # =====================================================================

    def analytics_reports(self, report_type: str = "totals/summarize/daily",
                          object_type: str = "landing-pages",
                          time_period: str = "total",
                          start: str = "", end: str = "",
                          account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        p: dict[str, Any] = {}
        if start:
            p["start"] = start
        if end:
            p["end"] = end
        return _req("GET", f"/analytics/v2/reports/{report_type}/{object_type}",
                     r["api_key"], params=p)

    # =====================================================================
    # META
    # =====================================================================

    def token_scopes(self, account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        token = r["api_key"]
        return _req("GET", f"/oauth/v1/access-tokens/{token}", token)

    def list_accounts_handler(self) -> dict:
        return {"accounts": _list_hubspot_accounts()}

    def health_check(self) -> dict[str, Any]:
        try:
            r = _resolve()
            if "error" in r:
                return {"status": "no_credentials", "detail": r["error"]}
            resp = httpx.get(
                f"{_BASE}/integrations/v1/me",
                headers={"Authorization": f"Bearer {r['api_key']}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
