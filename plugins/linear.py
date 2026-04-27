"""Linear MCP Gateway plugin.

Wraps the Linear GraphQL API (https://api.linear.app/graphql).
Credentials come from gateway get_credentials("linear") -> {"api_key": "..."}.
Auth header: Authorization: <api_key> (raw key, not Bearer).
"""

import base64
import json
import re
from typing import Any, Optional

import requests

from plugin_base import MCPPlugin, ToolDef, get_credentials

_UPLOAD_URL_RE = re.compile(r"https://uploads\.linear\.app/[^\s\)\"'>]+", re.IGNORECASE)
_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # skip images > 2MB

_LINEAR_ENDPOINT = "https://api.linear.app/graphql"


def _linear_request(query: str, variables: Optional[dict] = None) -> dict:
    """Execute a GraphQL request against the Linear API."""
    creds = get_credentials("linear")
    api_key = creds.get("api_key", "")
    if not api_key:
        return {
            "error": "Not authenticated. Configure Linear API key via gateway credentials."
        }

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        resp = requests.post(
            _LINEAR_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=30.0,
        )
    except requests.Timeout:
        return {"error": "Request timed out after 30s"}
    except requests.RequestException as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code >= 400:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        return {"error": f"HTTP {resp.status_code}", "details": err_body}

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return {"error": "Invalid JSON response from Linear API"}

    if "errors" in data and data["errors"]:
        messages = [e.get("message", str(e)) for e in data["errors"]]
        return {"error": "; ".join(messages), "graphql_errors": data["errors"]}

    return data.get("data", {})


def _fetch_linear_image(url: str, api_key: str) -> dict | None:
    """Download an image from uploads.linear.app and return as base64.

    Returns {url, base64, mime_type} or None on failure.
    Linear file storage accepts both 'Authorization: <key>' and 'Bearer <key>'.
    """
    for auth_val in [api_key, f"Bearer {api_key}"]:
        try:
            resp = requests.get(
                url,
                headers={"Authorization": auth_val},
                timeout=15,
                stream=True,
            )
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                if not ct.startswith("image/"):
                    return None
                data = resp.content
                if len(data) > _MAX_IMAGE_BYTES:
                    return None
                return {
                    "url": url,
                    "base64": base64.b64encode(data).decode(),
                    "mime_type": ct.split(";")[0].strip(),
                }
        except Exception:
            continue
    return None


def _extract_images_from_body(body: str, api_key: str) -> list[dict]:
    """Find uploads.linear.app URLs in markdown body and download them."""
    if not body or "uploads.linear.app" not in body:
        return []
    urls = _UPLOAD_URL_RE.findall(body)
    images = []
    for url in urls:
        img = _fetch_linear_image(url, api_key)
        if img:
            images.append(img)
    return images


def _clean_team(node: dict) -> dict:
    """Extract useful team fields from GraphQL node."""
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "key": node.get("key"),
        "description": node.get("description"),
        "color": node.get("color"),
        "icon": node.get("icon"),
        "archivedAt": node.get("archivedAt"),
    }


def _clean_issue(node: dict) -> dict:
    """Extract useful issue fields from GraphQL node."""
    assignee = node.get("assignee") or {}
    state = node.get("state") or {}
    team = node.get("team") or {}
    return {
        "id": node.get("id"),
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        "description": node.get("description"),
        "priority": node.get("priority"),
        "state": {"id": state.get("id"), "name": state.get("name")} if state else None,
        "assignee": {"id": assignee.get("id"), "name": assignee.get("name")} if assignee else None,
        "team": {"id": team.get("id"), "name": team.get("name")} if team else None,
        "createdAt": node.get("createdAt"),
        "updatedAt": node.get("updatedAt"),
        "url": node.get("url"),
    }


def _clean_user(node: dict) -> dict:
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "email": node.get("email"),
        "avatarUrl": node.get("avatarUrl"),
    }


# ── Read tools ────────────────────────────────────────────────────────────────


def list_teams(
    limit: int = 50,
    after_cursor: Optional[str] = None,
    include_archived: bool = False,
) -> dict:
    """List all teams in the Linear workspace."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    archived = "true" if include_archived else "false"
    query = f"""
    query {{
      teams(first: {min(limit, 250)}{after}, includeArchived: {archived}) {{
        nodes {{
          id name key description color icon archivedAt
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    teams_data = data.get("teams", {})
    nodes = teams_data.get("nodes", [])
    page_info = teams_data.get("pageInfo", {})
    return {
        "teams": [_clean_team(n) for n in nodes],
        "pageInfo": {
            "hasNextPage": page_info.get("hasNextPage"),
            "endCursor": page_info.get("endCursor"),
        },
    }


def get_team(team_id: str) -> dict:
    """Get a specific team by ID."""
    query = """
    query($id: String!) {
      team(id: $id) {
        id name key description color icon archivedAt
      }
    }
    """
    data = _linear_request(query, {"id": team_id})
    if "error" in data:
        return data
    team = data.get("team")
    if not team:
        return {"error": "Team not found"}
    return {"team": _clean_team(team)}


def list_issues(
    team_id: Optional[str] = None,
    status: Optional[str] = None,
    assignee_id: Optional[str] = None,
    limit: int = 50,
    after_cursor: Optional[str] = None,
) -> dict:
    """List/filter issues with pagination."""
    filter_parts = []
    if team_id:
        filter_parts.append(f'team: {{ id: {{ eq: "{team_id}" }} }}')
    if status:
        filter_parts.append(f'state: {{ name: {{ eqIgnoreCase: "{status}" }} }}')
    if assignee_id:
        if assignee_id.lower() == "me":
            filter_parts.append("assignee: { isMe: { eq: true } }")
        else:
            filter_parts.append(f'assignee: {{ id: {{ eq: "{assignee_id}" }} }}')
    filter_str = ", filter: { " + ", ".join(filter_parts) + " }" if filter_parts else ""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    query = f"""
    query {{
      issues(first: {min(limit, 250)}{after}{filter_str}) {{
        nodes {{
          id identifier title description priority createdAt updatedAt url
          state {{ id name }}
          assignee {{ id name }}
          team {{ id name }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    issues_data = data.get("issues", {})
    nodes = issues_data.get("nodes", [])
    page_info = issues_data.get("pageInfo", {})
    return {
        "issues": [_clean_issue(n) for n in nodes],
        "pageInfo": {
            "hasNextPage": page_info.get("hasNextPage"),
            "endCursor": page_info.get("endCursor"),
        },
    }


def get_issue(issue_id: str) -> dict:
    """Get issue details by ID or identifier (e.g. LIN-123)."""
    query = """
    query($id: String!) {
      issue(id: $id) {
        id identifier title description priority createdAt updatedAt url
        state { id name }
        assignee { id name email }
        team { id name key }
        project { id name }
        cycle { id name }
        labels { nodes { id name color } }
        attachments { nodes { id title url metadata } }
        branchName
      }
    }
    """
    data = _linear_request(query, {"id": issue_id})
    if "error" in data:
        return data
    issue = data.get("issue")
    if not issue:
        return {"error": "Issue not found"}
    result: dict[str, Any] = {"issue": _clean_issue(issue) | {
        "project": {"id": (p := issue.get("project") or {}).get("id"), "name": p.get("name")} if p else None,
        "cycle": {"id": (c := issue.get("cycle") or {}).get("id"), "name": c.get("name")} if c else None,
        "labels": [{"id": l.get("id"), "name": l.get("name"), "color": l.get("color")} for l in (issue.get("labels") or {}).get("nodes", [])],
        "attachments": [{"id": a.get("id"), "title": a.get("title"), "url": a.get("url")} for a in (issue.get("attachments") or {}).get("nodes", [])],
        "branchName": issue.get("branchName"),
    }}
    api_key = get_credentials("linear").get("api_key", "")
    if api_key:
        desc_images = _extract_images_from_body(issue.get("description", ""), api_key)
        if desc_images:
            result["_images"] = desc_images
    return result


def search_issues(query: str, limit: int = 50) -> dict:
    """Search issues by text query."""
    q = """
    query($query: String!, $first: Int!) {
      issueSearch(query: $query, first: $first) {
        nodes {
          id identifier title description priority createdAt updatedAt
          state { id name }
          assignee { id name }
          team { id name }
        }
      }
    }
    """
    data = _linear_request(q, {"query": query, "first": min(limit, 250)})
    if "error" in data:
        return data
    nodes = (data.get("issueSearch") or {}).get("nodes", [])
    return {"issues": [_clean_issue(n) for n in nodes]}


def list_issue_statuses(team_id: Optional[str] = None) -> dict:
    """List workflow states (statuses) for a team or all teams."""
    if team_id:
        query = """
        query($teamId: ID!) {
          workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name type color position }
          }
        }
        """
        data = _linear_request(query, {"teamId": team_id})
    else:
        query = """
        query {
          workflowStates { nodes { id name type color position team { id name } } }
        }
        """
        data = _linear_request(query)
    if "error" in data:
        return data
    nodes = (data.get("workflowStates") or {}).get("nodes", [])
    return {"statuses": [{"id": n.get("id"), "name": n.get("name"), "type": n.get("type"), "color": n.get("color")} for n in nodes]}


def list_issue_labels(team_id: Optional[str] = None) -> dict:
    """List labels for a team or all teams."""
    if team_id:
        query = """
        query($teamId: ID!) {
          issueLabels(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name color description }
          }
        }
        """
        data = _linear_request(query, {"teamId": team_id})
    else:
        query = """
        query { issueLabels { nodes { id name color description team { id name } } } }
        """
        data = _linear_request(query)
    if "error" in data:
        return data
    nodes = (data.get("issueLabels") or {}).get("nodes", [])
    return {"labels": [{"id": n.get("id"), "name": n.get("name"), "color": n.get("color"), "description": n.get("description")} for n in nodes]}


def list_projects(
    team_id: Optional[str] = None,
    limit: int = 50,
    after_cursor: Optional[str] = None,
) -> dict:
    """List projects, optionally filtered by team."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    filter_str = f', filter: {{ team: {{ id: {{ eq: "{team_id}" }} }} }}' if team_id else ""
    query = f"""
    query {{
      projects(first: {min(limit, 250)}{after}{filter_str}) {{
        nodes {{ id name slug description state targetDate icon }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    proj = data.get("projects", {})
    nodes = proj.get("nodes", [])
    pi = proj.get("pageInfo", {})
    return {
        "projects": [{"id": n.get("id"), "name": n.get("name"), "slug": n.get("slug"), "state": n.get("state"), "description": n.get("description")} for n in nodes],
        "pageInfo": {"hasNextPage": pi.get("hasNextPage"), "endCursor": pi.get("endCursor")},
    }


def get_project(project_id: str) -> dict:
    """Get project details by ID."""
    query = """
    query($id: String!) {
      project(id: $id) {
        id name slug description state targetDate icon color
        lead { id name email }
      }
    }
    """
    data = _linear_request(query, {"id": project_id})
    if "error" in data:
        return data
    p = data.get("project")
    if not p:
        return {"error": "Project not found"}
    lead = p.get("lead") or {}
    return {"project": {
        "id": p.get("id"), "name": p.get("name"), "slug": p.get("slug"),
        "description": p.get("description"), "state": p.get("state"),
        "targetDate": p.get("targetDate"), "icon": p.get("icon"), "color": p.get("color"),
        "lead": {"id": lead.get("id"), "name": lead.get("name")} if lead else None,
    }}


def list_milestones(limit: int = 50, after_cursor: Optional[str] = None) -> dict:
    """List milestones/initiatives."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    query = f"""
    query {{
      projectMilestones(first: {min(limit, 250)}{after}) {{
        nodes {{ id name description targetDate sortOrder project {{ id name }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    pm = data.get("projectMilestones", {})
    nodes = pm.get("nodes", [])
    pi = pm.get("pageInfo", {})
    return {
        "milestones": [{"id": n.get("id"), "name": n.get("name"), "description": n.get("description"), "targetDate": n.get("targetDate"), "project": n.get("project")} for n in nodes],
        "pageInfo": {"hasNextPage": pi.get("hasNextPage"), "endCursor": pi.get("endCursor")},
    }


def get_milestone(milestone_id: str) -> dict:
    """Get milestone details by ID."""
    query = """
    query($id: String!) {
      projectMilestone(id: $id) {
        id name description targetDate sortOrder
        project { id name }
      }
    }
    """
    data = _linear_request(query, {"id": milestone_id})
    if "error" in data:
        return data
    m = data.get("projectMilestone")
    if not m:
        return {"error": "Milestone not found"}
    return {"milestone": {"id": m.get("id"), "name": m.get("name"), "description": m.get("description"), "targetDate": m.get("targetDate"), "project": m.get("project")}}


def list_cycles(
    team_id: Optional[str] = None,
    limit: int = 50,
    after_cursor: Optional[str] = None,
) -> dict:
    """List cycles for a team."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    filter_str = f', filter: {{ team: {{ id: {{ eq: "{team_id}" }} }} }}' if team_id else ""
    query = f"""
    query {{
      cycles(first: {min(limit, 250)}{after}{filter_str}) {{
        nodes {{ id name startsAt endsAt number }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    cy = data.get("cycles", {})
    nodes = cy.get("nodes", [])
    pi = cy.get("pageInfo", {})
    return {
        "cycles": [{"id": n.get("id"), "name": n.get("name"), "startsAt": n.get("startsAt"), "endsAt": n.get("endsAt"), "number": n.get("number")} for n in nodes],
        "pageInfo": {"hasNextPage": pi.get("hasNextPage"), "endCursor": pi.get("endCursor")},
    }


def list_users(limit: int = 50, after_cursor: Optional[str] = None) -> dict:
    """List workspace members."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    query = f"""
    query {{
      users(first: {min(limit, 250)}{after}) {{
        nodes {{ id name email avatarUrl }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    u = data.get("users", {})
    nodes = u.get("nodes", [])
    pi = u.get("pageInfo", {})
    return {"users": [_clean_user(n) for n in nodes], "pageInfo": pi}


def get_user(user_id: str) -> dict:
    """Get user details by ID."""
    query = """
    query($id: String!) {
      user(id: $id) { id name email avatarUrl }
    }
    """
    data = _linear_request(query, {"id": user_id})
    if "error" in data:
        return data
    u = data.get("user")
    if not u:
        return {"error": "User not found"}
    return {"user": _clean_user(u)}


def list_comments(issue_id: str, limit: int = 50, after_cursor: Optional[str] = None, include_images: bool = True) -> dict:
    """List comments on an issue. Images from uploads.linear.app are
    automatically downloaded and returned inline as base64 by default."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    query = f"""
    query($issueId: ID!) {{
      issue(id: $issueId) {{
        comments(first: {min(limit, 250)}{after}) {{
          nodes {{ id body createdAt updatedAt user {{ id name email }} }}
          pageInfo {{ hasNextPage endCursor }}
        }}
      }}
    }}
    """
    data = _linear_request(query, {"issueId": issue_id})
    if "error" in data:
        return data
    issue = data.get("issue")
    if not issue:
        return {"error": "Issue not found"}
    cm = issue.get("comments", {})
    nodes = cm.get("nodes", [])
    pi = cm.get("pageInfo", {})

    api_key = get_credentials("linear").get("api_key", "") if include_images else ""
    comments = []
    all_images: list[dict] = []
    for n in nodes:
        comment: dict[str, Any] = {
            "id": n.get("id"),
            "body": n.get("body"),
            "createdAt": n.get("createdAt"),
            "user": _clean_user(n.get("user")),
        }
        if api_key:
            imgs = _extract_images_from_body(n.get("body", ""), api_key)
            if imgs:
                comment["image_count"] = len(imgs)
                all_images.extend(imgs)
        comments.append(comment)

    result: dict[str, Any] = {"comments": comments, "pageInfo": pi}
    if all_images:
        result["_images"] = all_images
    return result


def list_documents(limit: int = 50, after_cursor: Optional[str] = None) -> dict:
    """List documents."""
    after = f', after: "{after_cursor}"' if after_cursor else ""
    query = f"""
    query {{
      documents(first: {min(limit, 250)}{after}) {{
        nodes {{ id title content createdAt updatedAt creator {{ id name }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    data = _linear_request(query)
    if "error" in data:
        return data
    d = data.get("documents", {})
    nodes = d.get("nodes", [])
    pi = d.get("pageInfo", {})
    return {"documents": [{"id": n.get("id"), "title": n.get("title"), "creator": _clean_user(n.get("creator"))} for n in nodes], "pageInfo": pi}


def get_document(document_id: str) -> dict:
    """Get document details by ID."""
    query = """
    query($id: String!) {
      document(id: $id) {
        id title content createdAt updatedAt
        creator { id name email }
        project { id name }
      }
    }
    """
    data = _linear_request(query, {"id": document_id})
    if "error" in data:
        return data
    d = data.get("document")
    if not d:
        return {"error": "Document not found"}
    return {"document": {"id": d.get("id"), "title": d.get("title"), "content": d.get("content"), "createdAt": d.get("createdAt"), "project": d.get("project"), "creator": _clean_user(d.get("creator"))}}


def get_attachment(attachment_id: str) -> dict:
    """Get attachment details by ID."""
    query = """
    query($id: String!) {
      attachment(id: $id) {
        id title url subtitle metadata issue { id identifier }
      }
    }
    """
    data = _linear_request(query, {"id": attachment_id})
    if "error" in data:
        return data
    a = data.get("attachment")
    if not a:
        return {"error": "Attachment not found"}
    return {"attachment": {"id": a.get("id"), "title": a.get("title"), "url": a.get("url"), "subtitle": a.get("subtitle"), "issue": a.get("issue")}}


# ── Write tools ──────────────────────────────────────────────────────────────


def save_issue(
    team_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[int] = None,
    state_id: Optional[str] = None,
    assignee_id: Optional[str] = None,
    label_ids: Optional[list] = None,
    project_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
    issue_id_for_update: Optional[str] = None,
) -> dict:
    """Create or update an issue. If issue_id_for_update is provided, updates; otherwise creates. For create, team_id and title are required."""
    if issue_id_for_update:
        # Update existing issue
        input_parts = []
        if title is not None:
            input_parts.append(f'title: "{title.replace(chr(34), chr(92)+chr(34))}"')
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        if priority is not None:
            input_parts.append(f"priority: {priority}")
        if state_id is not None:
            input_parts.append(f'stateId: "{state_id}"')
        if assignee_id is not None:
            if assignee_id:
                input_parts.append(f'assigneeId: "{assignee_id}"')
            else:
                input_parts.append("assigneeId: null")
        if label_ids is not None:
            ids = json.dumps([{"id": lid} for lid in label_ids])
            input_parts.append(f"labelIds: {ids}")
        if project_id is not None:
            input_parts.append(f'projectId: "{project_id}"')
        if cycle_id is not None:
            input_parts.append(f'cycleId: "{cycle_id}"')
        if not input_parts:
            return {"error": "No fields to update"}
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          issueUpdate(id: "{issue_id_for_update}", input: {{ {inp} }}) {{
            success issue {{ id identifier title state {{ name }} }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("issueUpdate", {})
        if not res.get("success"):
            return {"error": "Update failed", "details": data}
        return {"issue": res.get("issue"), "updated": True}
    else:
        if not team_id or not title:
            return {"error": "team_id and title are required to create an issue"}
        input_parts = [f'teamId: "{team_id}"', f'title: "{title.replace(chr(34), chr(92)+chr(34))}"']
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        if priority is not None:
            input_parts.append(f"priority: {priority}")
        if state_id is not None:
            input_parts.append(f'stateId: "{state_id}"')
        if assignee_id is not None:
            input_parts.append(f'assigneeId: "{assignee_id}"')
        if label_ids:
            ids = json.dumps([{"id": lid} for lid in label_ids])
            input_parts.append(f"labelIds: {ids}")
        if project_id is not None:
            input_parts.append(f'projectId: "{project_id}"')
        if cycle_id is not None:
            input_parts.append(f'cycleId: "{cycle_id}"')
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          issueCreate(input: {{ {inp} }}) {{
            success issue {{ id identifier title state {{ name }} }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("issueCreate", {})
        if not res.get("success"):
            return {"error": "Create failed", "details": data}
        return {"issue": res.get("issue"), "created": True}


def save_comment(
    issue_id: str,
    body: str,
    comment_id_for_update: Optional[str] = None,
) -> dict:
    """Create or update a comment. If comment_id_for_update provided, updates; otherwise creates. For create, issue_id and body are required."""
    if comment_id_for_update:
        escaped = body.replace('"', '\\"').replace("\n", "\\n")
        mutation = f'''
        mutation {{
          commentUpdate(id: "{comment_id_for_update}", input: {{ body: "{escaped}" }}) {{
            success comment {{ id body }}
          }}
        }}
        '''
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("commentUpdate", {})
        if not res.get("success"):
            return {"error": "Update failed"}
        return {"comment": res.get("comment"), "updated": True}
    else:
        if not issue_id or not body:
            return {"error": "issue_id and body are required to create a comment"}
        escaped = body.replace('"', '\\"').replace("\n", "\\n")
        mutation = f'''
        mutation {{
          commentCreate(input: {{ issueId: "{issue_id}", body: "{escaped}" }}) {{
            success comment {{ id body }}
          }}
        }}
        '''
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("commentCreate", {})
        if not res.get("success"):
            return {"error": "Create failed"}
        return {"comment": res.get("comment"), "created": True}


def save_project(
    name: Optional[str] = None,
    description: Optional[str] = None,
    team_ids: Optional[list] = None,
    state: Optional[str] = None,
    project_id_for_update: Optional[str] = None,
) -> dict:
    """Create or update a project. If project_id_for_update provided, updates; otherwise creates. For create, name and team_ids are required."""
    if project_id_for_update:
        input_parts = []
        if name is not None:
            input_parts.append(f'name: "{name.replace(chr(34), chr(92)+chr(34))}"')
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        if state is not None:
            input_parts.append(f'state: "{state}"')
        if team_ids is not None:
            input_parts.append(f'teamIds: {json.dumps(team_ids)}')
        if not input_parts:
            return {"error": "No fields to update"}
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          projectUpdate(id: "{project_id_for_update}", input: {{ {inp} }}) {{
            success project {{ id name slug }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("projectUpdate", {})
        if not res.get("success"):
            return {"error": "Update failed"}
        return {"project": res.get("project"), "updated": True}
    else:
        if not name or not team_ids:
            return {"error": "name and team_ids are required to create a project"}
        input_parts = [f'name: "{name.replace(chr(34), chr(92)+chr(34))}"', f'teamIds: {json.dumps(team_ids)}']
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        if state is not None:
            input_parts.append(f'state: "{state}"')
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          projectCreate(input: {{ {inp} }}) {{
            success project {{ id name slug }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("projectCreate", {})
        if not res.get("success"):
            return {"error": "Create failed"}
        return {"project": res.get("project"), "created": True}


def save_milestone(
    name: Optional[str] = None,
    description: Optional[str] = None,
    milestone_id_for_update: Optional[str] = None,
) -> dict:
    """Create or update a milestone (initiative). For create, name is required. Note: project context may be needed for create."""
    if milestone_id_for_update:
        input_parts = []
        if name is not None:
            input_parts.append(f'name: "{name.replace(chr(34), chr(92)+chr(34))}"')
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        if not input_parts:
            return {"error": "No fields to update"}
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          projectMilestoneUpdate(id: "{milestone_id_for_update}", input: {{ {inp} }}) {{
            success projectMilestone {{ id name description }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("projectMilestoneUpdate", {})
        if not res.get("success"):
            return {"error": "Update failed"}
        return {"milestone": res.get("projectMilestone"), "updated": True}
    else:
        if not name:
            return {"error": "name is required to create a milestone. Consider using a project ID for project-specific milestones."}
        input_parts = [f'name: "{name.replace(chr(34), chr(92)+chr(34))}"']
        if description is not None:
            input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
        inp = ", ".join(input_parts)
        mutation = f"""
        mutation {{
          projectMilestoneCreate(input: {{ {inp} }}) {{
            success projectMilestone {{ id name description }}
          }}
        }}
        """
        data = _linear_request(mutation)
        if "error" in data:
            return data
        res = data.get("projectMilestoneCreate", {})
        if not res.get("success"):
            return {"error": "Create failed"}
        return {"milestone": res.get("projectMilestone"), "created": True}


def create_issue_label(
    team_id: str,
    name: str,
    color: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Create a label for a team."""
    input_parts = [f'teamId: "{team_id}"', f'name: "{name.replace(chr(34), chr(92)+chr(34))}"']
    if color:
        input_parts.append(f'color: "{color}"')
    if description:
        input_parts.append(f'description: "{description.replace(chr(34), chr(92)+chr(34))}"')
    inp = ", ".join(input_parts)
    mutation = f"""
    mutation {{
      issueLabelCreate(input: {{ {inp} }}) {{
        success issueLabel {{ id name color description }}
      }}
    }}
    """
    data = _linear_request(mutation)
    if "error" in data:
        return data
    res = data.get("issueLabelCreate", {})
    if not res.get("success"):
        return {"error": "Create failed"}
    return {"label": res.get("issueLabel"), "created": True}


def create_document(
    title: str,
    content: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create a document."""
    input_parts = [f'title: "{title.replace(chr(34), chr(92)+chr(34))}"']
    if content is not None:
        input_parts.append(f'content: "{content.replace(chr(34), chr(92)+chr(34)).replace(chr(92), chr(92)+chr(92))}"')
    if project_id:
        input_parts.append(f'projectId: "{project_id}"')
    inp = ", ".join(input_parts)
    mutation = f"""
    mutation {{
      documentCreate(input: {{ {inp} }}) {{
        success document {{ id title }}
      }}
    }}
    """
    data = _linear_request(mutation)
    if "error" in data:
        return data
    res = data.get("documentCreate", {})
    if not res.get("success"):
        return {"error": "Create failed"}
    return {"document": res.get("document"), "created": True}


def update_document(
    document_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> dict:
    """Update a document."""
    input_parts = []
    if title is not None:
        input_parts.append(f'title: "{title.replace(chr(34), chr(92)+chr(34))}"')
    if content is not None:
        input_parts.append(f'content: "{content.replace(chr(34), chr(92)+chr(34)).replace(chr(92), chr(92)+chr(92))}"')
    if not input_parts:
        return {"error": "No fields to update"}
    inp = ", ".join(input_parts)
    mutation = f"""
    mutation {{
      documentUpdate(id: "{document_id}", input: {{ {inp} }}) {{
        success document {{ id title }}
      }}
    }}
    """
    data = _linear_request(mutation)
    if "error" in data:
        return data
    res = data.get("documentUpdate", {})
    if not res.get("success"):
        return {"error": "Update failed"}
    return {"document": res.get("document"), "updated": True}


def create_attachment(
    issue_id: str,
    title: Optional[str] = None,
    url: Optional[str] = None,
    subtitle: Optional[str] = None,
    icon_url: Optional[str] = None,
) -> dict:
    """Create an attachment (link) on an issue. Requires url for link attachments."""
    if not url:
        return {"error": "url is required to create a link attachment"}
    input_parts = [f'issueId: "{issue_id}"', f'url: "{url}"']
    if title:
        input_parts.append(f'title: "{title.replace(chr(34), chr(92)+chr(34))}"')
    if subtitle:
        input_parts.append(f'subtitle: "{subtitle.replace(chr(34), chr(92)+chr(34))}"')
    if icon_url:
        input_parts.append(f'iconUrl: "{icon_url}"')
    inp = ", ".join(input_parts)
    mutation = f"""
    mutation {{
      attachmentLinkCreate(input: {{ {inp} }}) {{
        success attachment {{ id title url subtitle }}
      }}
    }}
    """
    data = _linear_request(mutation)
    if "error" in data:
        return data
    res = data.get("attachmentLinkCreate", {})
    if not res.get("success"):
        return {"error": "Create failed", "details": data}
    return {"attachment": res.get("attachment"), "created": True}


# ── Admin/Delete tools ───────────────────────────────────────────────────────


def delete_comment(comment_id: str) -> dict:
    """Delete a comment."""
    mutation = f'''
    mutation {{
      commentDelete(id: "{comment_id}") {{
        success
      }}
    }}
    '''
    data = _linear_request(mutation)
    if "error" in data:
        return data
    if not data.get("commentDelete", {}).get("success"):
        return {"error": "Delete failed"}
    return {"success": True}


def delete_attachment(attachment_id: str) -> dict:
    """Delete an attachment."""
    mutation = f'''
    mutation {{
      attachmentDelete(id: "{attachment_id}") {{
        success
      }}
    }}
    '''
    data = _linear_request(mutation)
    if "error" in data:
        return data
    if not data.get("attachmentDelete", {}).get("success"):
        return {"error": "Delete failed"}
    return {"success": True}


# ── Plugin definition ────────────────────────────────────────────────────────


class LinearPlugin(MCPPlugin):
    """Linear API plugin for MCP Gateway."""

    name = "linear"
    tools = {
        "list_teams": ToolDef(
            access="read",
            handler=list_teams,
            description="List all teams in the Linear workspace.",
        ),
        "get_team": ToolDef(
            access="read",
            handler=get_team,
            description="Get a specific team by ID.",
        ),
        "list_issues": ToolDef(
            access="read",
            handler=list_issues,
            description="List/filter issues with pagination. Filters: team_id, status, assignee_id. Use assignee_id='me' for current user's issues.",
        ),
        "get_issue": ToolDef(
            access="read",
            handler=get_issue,
            description="Get issue details by ID or identifier (e.g. LIN-123).",
        ),
        "search_issues": ToolDef(
            access="read",
            handler=search_issues,
            description="Search issues by text query.",
        ),
        "list_issue_statuses": ToolDef(
            access="read",
            handler=list_issue_statuses,
            description="List workflow states (statuses) for a team or all teams.",
        ),
        "list_issue_labels": ToolDef(
            access="read",
            handler=list_issue_labels,
            description="List labels for a team or all teams.",
        ),
        "list_projects": ToolDef(
            access="read",
            handler=list_projects,
            description="List projects, optionally filtered by team_id.",
        ),
        "get_project": ToolDef(
            access="read",
            handler=get_project,
            description="Get project details by ID.",
        ),
        "list_milestones": ToolDef(
            access="read",
            handler=list_milestones,
            description="List milestones/initiatives.",
        ),
        "get_milestone": ToolDef(
            access="read",
            handler=get_milestone,
            description="Get milestone details by ID.",
        ),
        "list_cycles": ToolDef(
            access="read",
            handler=list_cycles,
            description="List cycles for a team.",
        ),
        "list_users": ToolDef(
            access="read",
            handler=list_users,
            description="List workspace members.",
        ),
        "get_user": ToolDef(
            access="read",
            handler=get_user,
            description="Get user details by ID.",
        ),
        "list_comments": ToolDef(
            access="read",
            handler=list_comments,
            description="List comments on an issue. Images from uploads.linear.app are auto-downloaded and returned inline. Pass include_images=false to skip.",
        ),
        "list_documents": ToolDef(
            access="read",
            handler=list_documents,
            description="List documents.",
        ),
        "get_document": ToolDef(
            access="read",
            handler=get_document,
            description="Get document details by ID.",
        ),
        "get_attachment": ToolDef(
            access="read",
            handler=get_attachment,
            description="Get attachment details by ID.",
        ),
        "save_issue": ToolDef(
            access="write",
            handler=save_issue,
            description="Create or update an issue. Pass issue_id_for_update to update. Priority: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low.",
        ),
        "save_comment": ToolDef(
            access="write",
            handler=save_comment,
            description="Create or update a comment on an issue.",
        ),
        "save_project": ToolDef(
            access="write",
            handler=save_project,
            description="Create or update a project.",
        ),
        "save_milestone": ToolDef(
            access="write",
            handler=save_milestone,
            description="Create or update a milestone (initiative).",
        ),
        "create_issue_label": ToolDef(
            access="write",
            handler=create_issue_label,
            description="Create a label for a team.",
        ),
        "create_document": ToolDef(
            access="write",
            handler=create_document,
            description="Create a document.",
        ),
        "update_document": ToolDef(
            access="write",
            handler=update_document,
            description="Update a document.",
        ),
        "create_attachment": ToolDef(
            access="write",
            handler=create_attachment,
            description="Create a link attachment on an issue (requires url).",
        ),
        "delete_comment": ToolDef(
            access="admin",
            handler=delete_comment,
            description="Delete a comment.",
        ),
        "delete_attachment": ToolDef(
            access="admin",
            handler=delete_attachment,
            description="Delete an attachment.",
        ),
    }

    def health_check(self) -> dict:
        """Check if Linear API is reachable and credentials work."""
        creds = get_credentials("linear")
        api_key = creds.get("api_key", "")
        if not api_key:
            return {"status": "error", "message": "No API key configured"}
        data = _linear_request("{ viewer { id name } }")
        if "error" in data:
            return {"status": "error", "message": data.get("error", "Unknown error")}
        viewer = data.get("viewer", {})
        return {"status": "ok", "viewer": viewer.get("name", viewer.get("id", "authenticated"))}
