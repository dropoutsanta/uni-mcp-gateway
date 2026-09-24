"""Day.ai CRM plugin for the MCP Gateway.

Reverse-engineered GraphQL API with Supabase JWT auth.
Auth flow: refresh_token → Supabase → access_token (1hr) → Day.ai GQL.
Multi-account via {account}.refresh_token credentials.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_SUPABASE_PROJECT = "ffdfsbwhgoaivsfgdupn"
_SUPABASE_URL = f"https://{_SUPABASE_PROJECT}.supabase.co"
_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZmZGZzYndoZ29haXZzZmdkdXBuIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE2ODQ4OTkzMDgsImV4cCI6MjAwMDQ3NTMwOH0."
    "gPI2ZElvdr5Ka8FDPZY0r79dYZ8YxjJ8_uwwCB8HokU"
)
_GQL_URL = "https://day.ai/api/graphql"

_token_cache: dict[str, tuple[str, str, float]] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("dayai")
    except RuntimeError:
        return []
    accounts: set[str] = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "refresh_token" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve_account(account: str = "") -> dict:
    try:
        creds = get_credentials("dayai")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {"error": "Multiple Day.ai accounts. Specify `account`.",
                    "available_accounts": available}
        else:
            return {"error": "No Day.ai credentials configured for this key."}

    prefix = "" if selected == "default" else f"{selected}."
    rt = creds.get(f"{prefix}refresh_token", "")
    if not rt:
        return {"error": f"No refresh_token for account '{selected}'."}

    wsid = creds.get(f"{prefix}workspace_id", "")
    return {"account": selected, "refresh_token": rt, "workspace_id": wsid}


def _get_jwt(account: str, refresh_token: str) -> tuple[str, str] | None:
    """Return (access_token, new_refresh_token) or None."""
    cached = _token_cache.get(account)
    if cached:
        jwt, rt, exp = cached
        if time.time() < exp - 120:
            return jwt, rt

    try:
        resp = httpx.post(
            f"{_SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": _SUPABASE_ANON_KEY,
                     "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        jwt = data.get("access_token", "")
        new_rt = data.get("refresh_token", refresh_token)
        expires_in = data.get("expires_in", 3600)
        if jwt:
            _token_cache[account] = (jwt, new_rt, time.time() + expires_in)
            return jwt, new_rt
    except Exception:
        pass
    return None


def _auth(account: str = "") -> dict:
    """Resolve account and get a valid JWT."""
    resolved = _resolve_account(account)
    if "error" in resolved:
        return resolved

    result = _get_jwt(resolved["account"], resolved["refresh_token"])
    if not result:
        return {"error": f"Failed to refresh JWT for account '{resolved['account']}'."}

    return {"account": resolved["account"],
            "jwt": result[0],
            "workspace_id": resolved.get("workspace_id", "")}


def _gql(jwt: str, query: str, variables: dict | None = None,
         timeout: float = 30.0) -> dict:
    """Execute a GraphQL query against Day.ai."""
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    try:
        resp = httpx.post(
            _GQL_URL,
            headers={
                "Authorization": f"Bearer {jwt}",
                "auth-provider": "supabase",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    try:
        data = resp.json()
    except Exception:
        return {"error": f"Invalid JSON: {resp.text[:200]}"}

    if "errors" in data:
        msgs = "; ".join(e.get("message", "?") for e in data["errors"][:3])
        return {"error": msgs}

    return data.get("data", {})


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class DayAIPlugin(MCPPlugin):
    name = "dayai"

    def __init__(self):
        self.tools = {
            # ---- Search & Read ----
            "search": ToolDef(
                access="read", handler=self.search,
                description=(
                    "Search Day.ai CRM objects. Params: object_type (native_contact, "
                    "native_organization, native_meetingrecording, native_opportunity, "
                    "native_action, native_page), where_json (optional filter JSON, e.g. "
                    "'{\"propertyId\":\"email\",\"operator\":\"contains\",\"value\":\"@acme.com\"}'), "
                    "limit (default 50), offset (default 0)."
                ),
            ),
            "get_objects": ToolDef(
                access="read", handler=self.get_objects,
                description=(
                    "Get full details for specific objects by IDs. "
                    "Params: object_type, object_ids (comma-separated IDs), "
                    "fetch_relationships (bool, default false)."
                ),
            ),
            "recent_objects": ToolDef(
                access="read", handler=self.recent_objects,
                description="Get recently updated objects across all types.",
            ),
            "list_recordings": ToolDef(
                access="read", handler=self.list_recordings,
                description=(
                    "List meeting recordings. Params: limit (default 50), offset (default 0). "
                    "Returns IDs — use get_recording for full details/transcript."
                ),
            ),
            "get_recording": ToolDef(
                access="read", handler=self.get_recording,
                description=(
                    "Get full meeting recording details including summary, transcript, "
                    "and participants. Params: recording_id."
                ),
            ),
            "get_recording_transcript": ToolDef(
                access="read", handler=self.get_recording_transcript,
                description="Get the VTT transcript of a meeting recording. Params: recording_id.",
            ),

            # ---- Contacts & Orgs ----
            "get_contact": ToolDef(
                access="read", handler=self.get_contact,
                description="Get a contact by email. Params: email.",
            ),
            "get_organization": ToolDef(
                access="read", handler=self.get_organization,
                description="Get an organization by domain. Params: domain.",
            ),

            # ---- Write ----
            "update_object": ToolDef(
                access="write", handler=self.update_object,
                description=(
                    "Update a property on a CRM object. "
                    "Params: object_type, object_id, property_key, property_value."
                ),
            ),
            "create_object": ToolDef(
                access="write", handler=self.create_object,
                description=(
                    "Create a CRM object from web enrichment. "
                    "Params: object_type (native_contact or native_organization), "
                    "identifier (email for contacts, domain for orgs)."
                ),
            ),
            "create_relationship": ToolDef(
                access="write", handler=self.create_relationship,
                description=(
                    "Create a relationship between two objects. "
                    "Params: from_type, from_id, to_type, to_id, relationship_type."
                ),
            ),

            # ---- Pipeline ----
            "get_pipeline": ToolDef(
                access="read", handler=self.get_pipeline,
                description="Get pipeline details with stages and opportunities.",
            ),

            # ---- Discovery ----
            "list_accounts": ToolDef(
                access="read", handler=self.list_accounts_handler,
                description="List configured Day.ai accounts for the current key.",
            ),
        }

    # ---- Search & List ----

    def search(self, object_type: str, where_json: str = "",
               limit: int = 50, offset: int = 0,
               account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a

        q = """query($wsid: String!, $objectType: String!, $offset: Int, $limit: Int) {
          workspaceObjectsIds(
            workspaceId: $wsid, objectType: $objectType,
            offset: $offset, limit: $limit
          ) { objectId objectType updatedAt }
        }"""
        result = _gql(a["jwt"], q, {
            "wsid": a["workspace_id"], "objectType": object_type,
            "offset": offset, "limit": limit,
        })

        if isinstance(result, dict) and "error" in result:
            return result

        ids_list = result.get("workspaceObjectsIds", [])
        if not ids_list or not where_json:
            return {"objects": ids_list, "count": len(ids_list)}

        try:
            where = json.loads(where_json)
        except json.JSONDecodeError:
            return {"objects": ids_list, "count": len(ids_list)}

        obj_ids = [o["objectId"] for o in ids_list]
        details = self._fetch_objects(a["jwt"], a["workspace_id"],
                                       object_type, obj_ids)
        if isinstance(details, dict) and "error" in details:
            return details

        prop_id = where.get("propertyId", "")
        op = where.get("operator", "contains")
        val = str(where.get("value", "")).lower()

        filtered = []
        for obj in details:
            std = obj.get("properties", {}).get("standard", {})
            if isinstance(std, str):
                try:
                    std = json.loads(std)
                except Exception:
                    std = {}
            field_val = str(std.get(prop_id, "")).lower()
            if op == "contains" and val in field_val:
                filtered.append(obj)
            elif op == "eq" and field_val == val:
                filtered.append(obj)

        return {"objects": filtered, "count": len(filtered)}

    def get_objects(self, object_type: str, object_ids: str,
                    fetch_relationships: bool = False,
                    account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        ids = [x.strip() for x in object_ids.split(",") if x.strip()]
        return {"objects": self._fetch_objects(
            a["jwt"], a["workspace_id"], object_type, ids,
            fetch_relationships)}

    def recent_objects(self, account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """query($wsid: String!) {
          workspaceObjectsUpdatedRecent(workspaceId: $wsid) {
            objectId objectType updatedAt createdAt
            properties { standard custom }
            relationships
          }
        }"""
        result = _gql(a["jwt"], q, {"wsid": a["workspace_id"]})
        if isinstance(result, dict) and "error" in result:
            return result
        objs = result.get("workspaceObjectsUpdatedRecent", [])
        return {"objects": self._parse_objects(objs), "count": len(objs)}

    def list_recordings(self, limit: int = 50, offset: int = 0,
                        account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """query($wsid: String!, $offset: Int, $limit: Int) {
          workspaceObjectsIds(
            workspaceId: $wsid,
            objectType: "native_meetingrecording",
            offset: $offset, limit: $limit
          ) { objectId objectType updatedAt }
        }"""
        result = _gql(a["jwt"], q, {
            "wsid": a["workspace_id"], "offset": offset, "limit": limit,
        })
        if isinstance(result, dict) and "error" in result:
            return result
        ids = result.get("workspaceObjectsIds", [])
        return {"recordings": ids, "count": len(ids)}

    def get_recording(self, recording_id: str, account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """query($wsid: String!, $id: String!) {
          workspaceMeetingRecording(workspaceId: $wsid, id: $id) {
            id title startedAt endedAt
            statusHistory { level status reason message createdAt }
            summary { status output }
            participants { email }
          }
        }"""
        result = _gql(a["jwt"], q, {"wsid": a["workspace_id"], "id": recording_id},
                       timeout=60.0)
        if isinstance(result, dict) and "error" in result:
            return result
        return result.get("workspaceMeetingRecording", {})

    def get_recording_transcript(self, recording_id: str,
                                  account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a

        objs = self._fetch_objects(a["jwt"], a["workspace_id"],
                                    "native_meetingrecording", [recording_id])
        if isinstance(objs, dict) and "error" in objs:
            return objs
        if not objs:
            return {"error": "Recording not found."}

        obj = objs[0]
        std = obj.get("properties", {}).get("standard", {})
        if isinstance(std, str):
            try:
                std = json.loads(std)
            except Exception:
                std = {}

        return {
            "title": std.get("title", ""),
            "transcript_vtt": std.get("transcript/vtt", ""),
            "summary_short": std.get("summaryShort", ""),
            "summary_long": std.get("summaryLong", ""),
            "notes": std.get("notes", ""),
        }

    # ---- Contacts & Orgs ----

    def get_contact(self, email: str, account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        objs = self._fetch_objects(a["jwt"], a["workspace_id"],
                                    "native_contact", [email])
        if isinstance(objs, dict) and "error" in objs:
            return objs
        return {"contact": objs[0] if objs else None}

    def get_organization(self, domain: str, account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        objs = self._fetch_objects(a["jwt"], a["workspace_id"],
                                    "native_organization", [domain])
        if isinstance(objs, dict) and "error" in objs:
            return objs
        return {"organization": objs[0] if objs else None}

    # ---- Write ----

    def update_object(self, object_type: str, object_id: str,
                      property_key: str, property_value: str,
                      account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """mutation($wsid: String!, $objectType: String!, $objectId: String!,
                        $propertyKey: String!, $propertyValue: String!) {
          updateObjectProperty(
            workspaceId: $wsid, objectType: $objectType, objectId: $objectId,
            propertyKey: $propertyKey, propertyValue: $propertyValue
          )
        }"""
        return _gql(a["jwt"], q, {
            "wsid": a["workspace_id"], "objectType": object_type,
            "objectId": object_id, "propertyKey": property_key,
            "propertyValue": property_value,
        })

    def create_object(self, object_type: str, identifier: str,
                      account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """mutation($wsid: String!, $objectType: String!, $identifier: String!) {
          createObjectFromWeb(
            workspaceId: $wsid, objectType: $objectType, identifier: $identifier
          ) { objectId objectType }
        }"""
        return _gql(a["jwt"], q, {
            "wsid": a["workspace_id"], "objectType": object_type,
            "identifier": identifier,
        })

    def create_relationship(self, from_type: str, from_id: str,
                            to_type: str, to_id: str,
                            relationship_type: str = "related",
                            account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """mutation($wsid: String!, $fromType: String!, $fromId: String!,
                        $toType: String!, $toId: String!, $relType: String!) {
          createObjectRelationship(
            workspaceId: $wsid,
            fromObjectType: $fromType, fromObjectId: $fromId,
            toObjectType: $toType, toObjectId: $toId,
            relationshipType: $relType
          ) { id }
        }"""
        return _gql(a["jwt"], q, {
            "wsid": a["workspace_id"], "fromType": from_type,
            "fromId": from_id, "toType": to_type, "toId": to_id,
            "relType": relationship_type,
        })

    # ---- Pipeline ----

    def get_pipeline(self, account: str = "") -> dict:
        a = _auth(account)
        if "error" in a:
            return a
        q = """query($wsid: String!) {
          workspacePipeline(workspaceId: $wsid) {
            id title description stages {
              id title type position likelihoodToClose
              opportunities { id title domain expectedRevenue }
            }
          }
        }"""
        result = _gql(a["jwt"], q, {"wsid": a["workspace_id"]})
        if isinstance(result, dict) and "error" in result:
            return result
        return result.get("workspacePipeline", {})

    # ---- Discovery ----

    def list_accounts_handler(self) -> dict:
        return {"accounts": _list_accounts()}

    # ---- Internal helpers ----

    def _fetch_objects(self, jwt: str, wsid: str, object_type: str,
                       object_ids: list[str],
                       fetch_relationships: bool = False) -> list[dict] | dict:
        if not object_ids:
            return []
        q = """query($wsid: String!, $objectType: String!, $objectIds: [String!]!,
                     $fetchRel: Boolean) {
          workspaceObjectsByIds(
            workspaceId: $wsid, objectType: $objectType,
            objectIds: $objectIds, fetchRelationships: $fetchRel
          ) {
            workspaceId objectId objectType updatedAt createdAt
            properties { standard custom }
            relationships
          }
        }"""
        result = _gql(jwt, q, {
            "wsid": wsid, "objectType": object_type,
            "objectIds": object_ids, "fetchRel": fetch_relationships,
        })
        if isinstance(result, dict) and "error" in result:
            return result
        objs = result.get("workspaceObjectsByIds", [])
        return self._parse_objects(objs)

    @staticmethod
    def _parse_objects(objs: list[dict]) -> list[dict]:
        parsed = []
        for obj in objs:
            props = obj.get("properties", {})
            std = props.get("standard", {})
            if isinstance(std, str):
                try:
                    std = json.loads(std)
                except Exception:
                    pass
            custom = props.get("custom", {})
            if isinstance(custom, str):
                try:
                    custom = json.loads(custom)
                except Exception:
                    pass
            rels = obj.get("relationships", {})
            if isinstance(rels, str):
                try:
                    rels = json.loads(rels)
                except Exception:
                    pass
            parsed.append({
                "objectId": obj.get("objectId"),
                "objectType": obj.get("objectType"),
                "updatedAt": obj.get("updatedAt"),
                "createdAt": obj.get("createdAt"),
                "properties": std,
                "customProperties": custom,
                "relationships": rels,
            })
        return parsed

    def health_check(self) -> dict[str, Any]:
        try:
            a = _auth()
            if "error" in a:
                return {"status": "no_credentials", "detail": a["error"]}
            result = _gql(a["jwt"], "query { __typename }")
            if isinstance(result, dict) and "error" not in result:
                return {"status": "ok"}
            return {"status": "error", "detail": result.get("error", "?")}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
