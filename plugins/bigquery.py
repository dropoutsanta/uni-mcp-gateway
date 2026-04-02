"""Google BigQuery plugin for the MCP Gateway.

Run SQL queries, manage datasets and tables in Google BigQuery.
Uses service account JSON keys for authentication.
Multi-account support via {account}.service_account_json credential pattern.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx
import jwt

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE_URL = "https://bigquery.googleapis.com/bigquery/v2"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/bigquery"

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


# ── Token management ─────────────────────────────────────────────────────────

_token_cache: dict[str, tuple[str, float]] = {}


def _get_access_token(sa_json: str) -> str:
    sa = json.loads(sa_json)
    now = int(time.time())
    payload = {
        "iss": sa["client_email"],
        "scope": _SCOPE,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signed = jwt.encode(payload, sa["private_key"], algorithm="RS256")
    resp = httpx.post(_TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed,
    }, timeout=15.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def _get_cached_token(account: str, sa_json: str) -> str:
    now = time.time()
    if account in _token_cache:
        token, expiry = _token_cache[account]
        if now < expiry - 60:
            return token
    token = _get_access_token(sa_json)
    _token_cache[account] = (token, now + 3600)
    return token


# ── Multi-account credential resolution ──────────────────────────────────────

def _list_bigquery_accounts() -> list[str]:
    try:
        creds = get_credentials("bigquery")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "service_account_json" in creds:
        accounts.add("default")
    return sorted(accounts)


def _get_bigquery_account_credentials(account: str = "") -> dict:
    try:
        creds = get_credentials("bigquery")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_bigquery_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple BigQuery accounts configured. You MUST specify the `account` parameter.",
                "available_accounts": available,
            }
        else:
            return {"error": "No BigQuery credentials configured for this key."}

    if selected == "default":
        sa_json = creds.get("service_account_json", "")
    else:
        sa_json = creds.get(f"{selected}.service_account_json", "")

    if not sa_json:
        return {
            "error": f"No BigQuery credentials found for account '{selected}'.",
            "available_accounts": _list_bigquery_accounts(),
        }

    return {"account": selected, "service_account_json": sa_json}


def _get_project_id(sa_json: str) -> str:
    try:
        return json.loads(sa_json).get("project_id", "")
    except Exception:
        return ""


# ── HTTP request helper ──────────────────────────────────────────────────────

def _bigquery_request(method: str, path: str, params: dict, field_mappings: dict, account: str = "") -> dict:
    resolved = _get_bigquery_account_credentials(account)
    if "error" in resolved:
        return resolved

    sa_json = resolved["service_account_json"]

    if "projectId" not in params or not params.get("projectId"):
        params["projectId"] = _get_project_id(sa_json)

    try:
        access_token = _get_cached_token(resolved["account"], sa_json)
    except RuntimeError as exc:
        return {"error": str(exc)}

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
        elif location == "body_raw":
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        body.update(parsed)
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass

    url = f"{_BASE_URL}{url_path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    req_kwargs: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers,
        "timeout": 60.0,
    }
    if query:
        req_kwargs["params"] = query
    if body:
        headers["Content-Type"] = "application/json"
        req_kwargs["json"] = body

    try:
        resp = httpx.request(**req_kwargs)
    except httpx.TimeoutException:
        return {"error": "Request timed out after 60s"}
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

ADMIN_TOOLS = {
    "datasets_delete",
    "tables_delete",
    "jobs_cancel",
}


def _access_for_command(cmd_def: dict) -> str:
    name = cmd_def["name"]
    method = cmd_def["method"]
    if name in ADMIN_TOOLS:
        return "admin"
    if method == "GET":
        return "read"
    return "write"


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

# ── Datasets ─────────────────────────────────────────────────────────────────

cmd("datasets_list", "List all datasets in the project.", "GET", "/projects/{projectId}/datasets",
    [p("projectId", "str", False, "Project ID (defaults to service account project)"),
     p("maxResults", "int", False, "Max datasets to return"),
     p("pageToken", "str", False, "Pagination token"),
     p("all", "bool", False, "Include hidden datasets")],
    {"projectId": "path", "maxResults": "query", "pageToken": "query", "all": "query"})

cmd("datasets_get", "Get metadata for a dataset.", "GET", "/projects/{projectId}/datasets/{datasetId}",
    [p("datasetId", d="Dataset ID"),
     p("projectId", "str", False, "Project ID")],
    {"datasetId": "path", "projectId": "path"})

cmd("datasets_create", "Create a dataset. Pass resource_json with the full dataset resource.", "POST", "/projects/{projectId}/datasets",
    [p("resource_json", d="Dataset resource JSON (must include datasetReference.datasetId). Example: {\"datasetReference\": {\"datasetId\": \"my_dataset\"}, \"location\": \"US\"}"),
     p("projectId", "str", False, "Project ID")],
    {"resource_json": "body_raw", "projectId": "path"})

cmd("datasets_patch", "Update a dataset. Pass resource_json with fields to update.", "PATCH", "/projects/{projectId}/datasets/{datasetId}",
    [p("datasetId", d="Dataset ID"),
     p("resource_json", d="Partial dataset resource JSON with fields to update"),
     p("projectId", "str", False, "Project ID")],
    {"datasetId": "path", "resource_json": "body_raw", "projectId": "path"})

cmd("datasets_delete", "Delete a dataset.", "DELETE", "/projects/{projectId}/datasets/{datasetId}",
    [p("datasetId", d="Dataset ID"),
     p("projectId", "str", False, "Project ID"),
     p("deleteContents", "bool", False, "Delete all tables in the dataset")],
    {"datasetId": "path", "projectId": "path", "deleteContents": "query"})


# ── Tables ───────────────────────────────────────────────────────────────────

cmd("tables_list", "List all tables in a dataset.", "GET", "/projects/{projectId}/datasets/{datasetId}/tables",
    [p("datasetId", d="Dataset ID"),
     p("projectId", "str", False, "Project ID"),
     p("maxResults", "int", False, "Max tables to return"),
     p("pageToken", "str", False, "Pagination token")],
    {"datasetId": "path", "projectId": "path", "maxResults": "query", "pageToken": "query"})

cmd("tables_get", "Get table metadata and schema.", "GET", "/projects/{projectId}/datasets/{datasetId}/tables/{tableId}",
    [p("datasetId", d="Dataset ID"),
     p("tableId", d="Table ID"),
     p("projectId", "str", False, "Project ID"),
     p("selectedFields", "str", False, "Comma-separated list of fields to return in schema")],
    {"datasetId": "path", "tableId": "path", "projectId": "path", "selectedFields": "query"})

cmd("tables_create", "Create a table. Pass resource_json with the full table resource.", "POST", "/projects/{projectId}/datasets/{datasetId}/tables",
    [p("datasetId", d="Dataset ID"),
     p("resource_json", d="Table resource JSON (must include tableReference.tableId and schema). Example: {\"tableReference\": {\"tableId\": \"my_table\"}, \"schema\": {\"fields\": [{\"name\": \"col1\", \"type\": \"STRING\"}]}}"),
     p("projectId", "str", False, "Project ID")],
    {"datasetId": "path", "resource_json": "body_raw", "projectId": "path"})

cmd("tables_patch", "Update a table. Pass resource_json with fields to update.", "PATCH", "/projects/{projectId}/datasets/{datasetId}/tables/{tableId}",
    [p("datasetId", d="Dataset ID"),
     p("tableId", d="Table ID"),
     p("resource_json", d="Partial table resource JSON with fields to update"),
     p("projectId", "str", False, "Project ID")],
    {"datasetId": "path", "tableId": "path", "resource_json": "body_raw", "projectId": "path"})

cmd("tables_delete", "Delete a table.", "DELETE", "/projects/{projectId}/datasets/{datasetId}/tables/{tableId}",
    [p("datasetId", d="Dataset ID"),
     p("tableId", d="Table ID"),
     p("projectId", "str", False, "Project ID")],
    {"datasetId": "path", "tableId": "path", "projectId": "path"})


# ── Table Data ───────────────────────────────────────────────────────────────

cmd("tabledata_list", "List/preview rows from a table.", "GET", "/projects/{projectId}/datasets/{datasetId}/tables/{tableId}/data",
    [p("datasetId", d="Dataset ID"),
     p("tableId", d="Table ID"),
     p("projectId", "str", False, "Project ID"),
     p("maxResults", "int", False, "Max rows to return"),
     p("startIndex", "str", False, "Zero-based row index to start from"),
     p("pageToken", "str", False, "Pagination token"),
     p("selectedFields", "str", False, "Comma-separated fields to return")],
    {"datasetId": "path", "tableId": "path", "projectId": "path",
     "maxResults": "query", "startIndex": "query", "pageToken": "query", "selectedFields": "query"})

cmd("tabledata_insert", "Insert rows into a table via streaming insert.", "POST", "/projects/{projectId}/datasets/{datasetId}/tables/{tableId}/insertAll",
    [p("datasetId", d="Dataset ID"),
     p("tableId", d="Table ID"),
     p("rows", d="JSON array of row objects. Each: {\"insertId\": \"optional-dedup-id\", \"json\": {\"col1\": \"val1\", ...}}"),
     p("projectId", "str", False, "Project ID"),
     p("skipInvalidRows", "bool", False, "Insert valid rows even if some are invalid"),
     p("ignoreUnknownValues", "bool", False, "Ignore unknown column names")],
    {"datasetId": "path", "tableId": "path", "projectId": "path",
     "rows": "body", "skipInvalidRows": "body", "ignoreUnknownValues": "body"})


# ── Jobs ─────────────────────────────────────────────────────────────────────

cmd("jobs_list", "List BigQuery jobs.", "GET", "/projects/{projectId}/jobs",
    [p("projectId", "str", False, "Project ID"),
     p("maxResults", "int", False, "Max jobs to return"),
     p("pageToken", "str", False, "Pagination token"),
     p("stateFilter", "str", False, "Filter by state: done, pending, running"),
     p("allUsers", "bool", False, "Show jobs from all users"),
     p("projection", "str", False, "full or minimal")],
    {"projectId": "path", "maxResults": "query", "pageToken": "query",
     "stateFilter": "query", "allUsers": "query", "projection": "query"})

cmd("jobs_get", "Get job details and results.", "GET", "/projects/{projectId}/jobs/{jobId}",
    [p("jobId", d="Job ID"),
     p("projectId", "str", False, "Project ID"),
     p("location", "str", False, "Job location")],
    {"jobId": "path", "projectId": "path", "location": "query"})

cmd("jobs_cancel", "Cancel a running job.", "POST", "/projects/{projectId}/jobs/{jobId}/cancel",
    [p("jobId", d="Job ID"),
     p("projectId", "str", False, "Project ID"),
     p("location", "str", False, "Job location")],
    {"jobId": "path", "projectId": "path", "location": "query"})

cmd("query_results", "Get paginated results for a completed query job.", "GET", "/projects/{projectId}/queries/{jobId}",
    [p("jobId", d="Job ID from a previous query"),
     p("projectId", "str", False, "Project ID"),
     p("maxResults", "int", False, "Max rows per page"),
     p("pageToken", "str", False, "Pagination token"),
     p("startIndex", "str", False, "Zero-based start row"),
     p("location", "str", False, "Job location")],
    {"jobId": "path", "projectId": "path", "maxResults": "query",
     "pageToken": "query", "startIndex": "query", "location": "query"})


# ══════════════════════════════════════════════════════════════════════════════
# PLUGIN CLASS
# ══════════════════════════════════════════════════════════════════════════════

class BigQueryPlugin(MCPPlugin):
    name = "bigquery"

    def __init__(self):
        self.tools: dict[str, ToolDef] = {}
        self._register_meta_tools()
        self._register_query_tool()
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
    return _bigquery_request("{method}", "{path}", _p, {repr(field_mappings)}, account=account)
'''

        ns: dict[str, Any] = {
            "Optional": Optional,
            "_bigquery_request": _bigquery_request,
        }
        exec(fn_code, ns)
        fn = ns[name]

        access = _access_for_command(cmd_def)
        self.tools[name] = ToolDef(access=access, handler=fn, description=desc)

    def _register_query_tool(self):
        def query(sql: str, projectId: str = "", maxResults: int = 100,
                  useLegacySql: bool = False, timeoutMs: int = 30000,
                  location: str = "", account: str = "") -> dict:
            """Run a SQL query against BigQuery. Returns rows with schema. Standard SQL by default."""
            resolved = _get_bigquery_account_credentials(account)
            if "error" in resolved:
                return resolved

            sa_json = resolved["service_account_json"]
            pid = projectId or _get_project_id(sa_json)

            try:
                access_token = _get_cached_token(resolved["account"], sa_json)
            except RuntimeError as exc:
                return {"error": str(exc)}

            body: dict[str, Any] = {
                "query": sql,
                "useLegacySql": useLegacySql,
                "maxResults": maxResults,
                "timeoutMs": timeoutMs,
            }
            if location:
                body["location"] = location

            try:
                resp = httpx.post(
                    f"{_BASE_URL}/projects/{pid}/queries",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=120.0,
                )
            except httpx.TimeoutException:
                return {"error": "Query timed out after 120s"}
            except httpx.RequestError as exc:
                return {"error": f"Request failed: {exc}"}

            if resp.status_code >= 400:
                try:
                    return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
                except Exception:
                    return {"error": f"HTTP {resp.status_code}", "details": resp.text}

            result = resp.json()
            job_complete = result.get("jobComplete", False)
            if not job_complete:
                return {
                    "status": "pending",
                    "jobId": result.get("jobReference", {}).get("jobId"),
                    "hint": "Query is still running. Use query_results with this jobId to poll for results.",
                }

            return result

        self.tools["query"] = ToolDef(
            access="write",
            handler=query,
            description=query.__doc__,
        )

    def _register_meta_tools(self):
        def get_account_credentials(account: str = "") -> dict:
            """Get the service account details for a configured BigQuery account."""
            resolved = _get_bigquery_account_credentials(account)
            if "error" in resolved:
                return resolved
            sa = json.loads(resolved["service_account_json"])
            return {
                "account": resolved["account"],
                "project_id": sa.get("project_id", ""),
                "client_email": sa.get("client_email", ""),
                "hint": "Use these details for BigQuery operations. The project_id is auto-used if not specified.",
            }

        def list_accounts() -> dict:
            """List all configured BigQuery accounts for the current key."""
            accounts = _list_bigquery_accounts()
            return {"accounts": accounts, "hint": "Pass the account name as the `account` parameter to any BigQuery tool."}

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
            resolved = _get_bigquery_account_credentials()
            if "error" in resolved:
                return {"status": "no_credentials", "detail": resolved["error"]}
            sa_json = resolved["service_account_json"]
            pid = _get_project_id(sa_json)
            access_token = _get_cached_token(resolved["account"], sa_json)
            resp = httpx.get(
                f"{_BASE_URL}/projects/{pid}/datasets",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"maxResults": 1},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
