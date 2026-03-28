"""Slack MCP Gateway plugin.

Wraps the Slack Web API (https://api.slack.com/methods).
Credentials come from gateway get_credentials("slack") -> {"bot_token": "xoxb-..."}.
Auth: Authorization: Bearer xoxb-TOKEN header.
"""

import json
from typing import Any, Optional

import requests

from plugin_base import MCPPlugin, ToolDef, get_credentials

_SLACK_BASE = "https://slack.com/api"


def _slack_request(
    method: str,
    endpoint: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    files: Optional[dict] = None,
) -> dict:
    """Make an authenticated request to the Slack Web API."""
    creds = get_credentials("slack")
    token = creds.get("bot_token", "")
    if not token:
        return {"error": "Not authenticated. Configure Slack bot_token via gateway credentials."}

    url = f"{_SLACK_BASE}/{endpoint}"
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if not files:
        headers["Content-Type"] = "application/json; charset=utf-8"

    kwargs: dict[str, Any] = {"headers": headers, "timeout": 30.0}

    if method.upper() == "GET":
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        try:
            resp = requests.get(url, **kwargs)
        except requests.Timeout:
            return {"error": "Request timed out after 30s"}
        except requests.RequestException as exc:
            return {"error": f"Request failed: {exc}"}
    else:
        if files:
            if body:
                kwargs["data"] = {k: v for k, v in body.items() if v is not None}
            kwargs["files"] = files
        elif body:
            kwargs["json"] = {k: v for k, v in body.items() if v is not None}
        try:
            resp = requests.post(url, **kwargs)
        except requests.Timeout:
            return {"error": "Request timed out after 30s"}
        except requests.RequestException as exc:
            return {"error": f"Request failed: {exc}"}

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        return {"error": f"HTTP {resp.status_code}", "details": err}

    try:
        data = resp.json()
    except Exception:
        return {"error": "Invalid JSON response from Slack API"}

    if not data.get("ok"):
        return {"error": data.get("error", "unknown_error"), "response_metadata": data.get("response_metadata")}

    return data


# ── Auth ──────────────────────────────────────────────────────────────────────


def auth_test() -> dict:
    """Test Slack authentication. Returns info about the authenticated bot/user: team, user, bot_id, and scopes."""
    return _slack_request("POST", "auth.test")


# ── Conversations ─────────────────────────────────────────────────────────────


def conversations_list(
    types: Optional[str] = None,
    exclude_archived: Optional[str] = None,
    limit: Optional[str] = None,
    cursor: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List channels the bot has access to. types: comma-separated list of 'public_channel,private_channel,mpim,im'. Default: public_channel. limit: 1-1000 (default 100). Returns paginated list with response_metadata.next_cursor for pagination."""
    params: dict[str, Any] = {}
    if types:
        params["types"] = types
    if exclude_archived:
        params["exclude_archived"] = exclude_archived
    if limit:
        params["limit"] = limit
    if cursor:
        params["cursor"] = cursor
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "conversations.list", params=params)


def conversations_info(
    channel: str,
    include_locale: Optional[str] = None,
    include_num_members: Optional[str] = None,
) -> dict:
    """Get detailed info about a channel/conversation by its ID. Returns name, topic, purpose, member count, creation date, and more."""
    params: dict[str, Any] = {"channel": channel}
    if include_locale:
        params["include_locale"] = include_locale
    if include_num_members:
        params["include_num_members"] = include_num_members
    return _slack_request("GET", "conversations.info", params=params)


def conversations_history(
    channel: str,
    cursor: Optional[str] = None,
    inclusive: Optional[str] = None,
    latest: Optional[str] = None,
    limit: Optional[str] = None,
    oldest: Optional[str] = None,
    include_all_metadata: Optional[str] = None,
) -> dict:
    """Fetch message history from a channel. Returns messages in reverse chronological order. Use oldest/latest (Unix timestamps) to filter by date range. limit: 1-1000 (default 100). Paginate with cursor from response_metadata.next_cursor."""
    params: dict[str, Any] = {"channel": channel}
    if cursor:
        params["cursor"] = cursor
    if inclusive:
        params["inclusive"] = inclusive
    if latest:
        params["latest"] = latest
    if limit:
        params["limit"] = limit
    if oldest:
        params["oldest"] = oldest
    if include_all_metadata:
        params["include_all_metadata"] = include_all_metadata
    return _slack_request("GET", "conversations.history", params=params)


def conversations_replies(
    channel: str,
    ts: str,
    cursor: Optional[str] = None,
    inclusive: Optional[str] = None,
    latest: Optional[str] = None,
    limit: Optional[str] = None,
    oldest: Optional[str] = None,
    include_all_metadata: Optional[str] = None,
) -> dict:
    """Get replies in a message thread. channel: the channel containing the thread. ts: the thread_ts (timestamp of the parent message). Returns all replies plus the parent message."""
    params: dict[str, Any] = {"channel": channel, "ts": ts}
    if cursor:
        params["cursor"] = cursor
    if inclusive:
        params["inclusive"] = inclusive
    if latest:
        params["latest"] = latest
    if limit:
        params["limit"] = limit
    if oldest:
        params["oldest"] = oldest
    if include_all_metadata:
        params["include_all_metadata"] = include_all_metadata
    return _slack_request("GET", "conversations.replies", params=params)


def conversations_members(
    channel: str,
    cursor: Optional[str] = None,
    limit: Optional[str] = None,
) -> dict:
    """List member user IDs in a channel. Paginate with cursor. Returns an array of user IDs."""
    params: dict[str, Any] = {"channel": channel}
    if cursor:
        params["cursor"] = cursor
    if limit:
        params["limit"] = limit
    return _slack_request("GET", "conversations.members", params=params)


def conversations_create(
    name: str,
    is_private: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Create a new public or private channel. name: channel name (lowercase, no spaces, max 80 chars). is_private: 'true' to create a private channel."""
    body: dict[str, Any] = {"name": name}
    if is_private:
        body["is_private"] = is_private.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "conversations.create", body=body)


def conversations_archive(channel: str) -> dict:
    """Archive a channel. The channel will be read-only but searchable."""
    return _slack_request("POST", "conversations.archive", body={"channel": channel})


def conversations_unarchive(channel: str) -> dict:
    """Unarchive a channel, making it active again."""
    return _slack_request("POST", "conversations.unarchive", body={"channel": channel})


def conversations_invite(channel: str, users: str) -> dict:
    """Invite users to a channel. users: comma-separated list of user IDs (e.g. 'U123,U456')."""
    return _slack_request("POST", "conversations.invite", body={"channel": channel, "users": users})


def conversations_kick(channel: str, user: str) -> dict:
    """Remove a user from a channel."""
    return _slack_request("POST", "conversations.kick", body={"channel": channel, "user": user})


def conversations_join(channel: str) -> dict:
    """Join a public channel. The bot will become a member."""
    return _slack_request("POST", "conversations.join", body={"channel": channel})


def conversations_leave(channel: str) -> dict:
    """Leave a channel."""
    return _slack_request("POST", "conversations.leave", body={"channel": channel})


def conversations_rename(channel: str, name: str) -> dict:
    """Rename a channel. name: new channel name (lowercase, no spaces, max 80 chars)."""
    return _slack_request("POST", "conversations.rename", body={"channel": channel, "name": name})


def conversations_set_topic(channel: str, topic: str) -> dict:
    """Set the topic of a channel."""
    return _slack_request("POST", "conversations.setTopic", body={"channel": channel, "topic": topic})


def conversations_set_purpose(channel: str, purpose: str) -> dict:
    """Set the purpose of a channel."""
    return _slack_request("POST", "conversations.setPurpose", body={"channel": channel, "purpose": purpose})


def conversations_open(
    channel: Optional[str] = None,
    users: Optional[str] = None,
    return_im: Optional[str] = None,
    prevent_creation: Optional[str] = None,
) -> dict:
    """Open or resume a direct message or multi-person DM. Provide either channel (existing conversation ID) or users (comma-separated user IDs to open a DM with). For group DMs, pass multiple user IDs."""
    body: dict[str, Any] = {}
    if channel:
        body["channel"] = channel
    if users:
        body["users"] = users
    if return_im:
        body["return_im"] = return_im.lower() == "true"
    if prevent_creation:
        body["prevent_creation"] = prevent_creation.lower() == "true"
    return _slack_request("POST", "conversations.open", body=body)


# ── Chat (Messages) ──────────────────────────────────────────────────────────


def chat_post_message(
    channel: str,
    text: Optional[str] = None,
    blocks: Optional[str] = None,
    attachments: Optional[str] = None,
    thread_ts: Optional[str] = None,
    reply_broadcast: Optional[str] = None,
    unfurl_links: Optional[str] = None,
    unfurl_media: Optional[str] = None,
    mrkdwn: Optional[str] = None,
    metadata: Optional[str] = None,
) -> dict:
    """Send a message to a channel. channel: channel ID. text: message text (supports mrkdwn). blocks: JSON string of Block Kit blocks for rich layouts. attachments: JSON string of legacy attachments. thread_ts: timestamp of parent message to reply in a thread. reply_broadcast: 'true' to also post to the channel when replying in a thread."""
    body: dict[str, Any] = {"channel": channel}
    if text:
        body["text"] = text
    if blocks:
        body["blocks"] = json.loads(blocks) if isinstance(blocks, str) else blocks
    if attachments:
        body["attachments"] = json.loads(attachments) if isinstance(attachments, str) else attachments
    if thread_ts:
        body["thread_ts"] = thread_ts
    if reply_broadcast:
        body["reply_broadcast"] = reply_broadcast.lower() == "true"
    if unfurl_links:
        body["unfurl_links"] = unfurl_links.lower() == "true"
    if unfurl_media:
        body["unfurl_media"] = unfurl_media.lower() == "true"
    if mrkdwn:
        body["mrkdwn"] = mrkdwn.lower() == "true"
    if metadata:
        body["metadata"] = json.loads(metadata) if isinstance(metadata, str) else metadata
    return _slack_request("POST", "chat.postMessage", body=body)


def chat_update(
    channel: str,
    ts: str,
    text: Optional[str] = None,
    blocks: Optional[str] = None,
    attachments: Optional[str] = None,
) -> dict:
    """Update an existing message. channel: channel containing the message. ts: timestamp of the message to update. Provide new text, blocks, or attachments."""
    body: dict[str, Any] = {"channel": channel, "ts": ts}
    if text:
        body["text"] = text
    if blocks:
        body["blocks"] = json.loads(blocks) if isinstance(blocks, str) else blocks
    if attachments:
        body["attachments"] = json.loads(attachments) if isinstance(attachments, str) else attachments
    return _slack_request("POST", "chat.update", body=body)


def chat_delete(channel: str, ts: str) -> dict:
    """Delete a message. channel: channel containing the message. ts: timestamp of the message to delete."""
    return _slack_request("POST", "chat.delete", body={"channel": channel, "ts": ts})


def chat_schedule_message(
    channel: str,
    post_at: str,
    text: Optional[str] = None,
    blocks: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """Schedule a message to be sent later. post_at: Unix timestamp for when the message should be sent (must be in the future, within 120 days)."""
    body: dict[str, Any] = {"channel": channel, "post_at": int(post_at)}
    if text:
        body["text"] = text
    if blocks:
        body["blocks"] = json.loads(blocks) if isinstance(blocks, str) else blocks
    if thread_ts:
        body["thread_ts"] = thread_ts
    return _slack_request("POST", "chat.scheduleMessage", body=body)


def chat_scheduled_messages_list(
    channel: Optional[str] = None,
    cursor: Optional[str] = None,
    latest: Optional[str] = None,
    limit: Optional[str] = None,
    oldest: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List scheduled messages. Optionally filter by channel and date range."""
    body: dict[str, Any] = {}
    if channel:
        body["channel"] = channel
    if cursor:
        body["cursor"] = cursor
    if latest:
        body["latest"] = latest
    if limit:
        body["limit"] = int(limit)
    if oldest:
        body["oldest"] = oldest
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "chat.scheduledMessages.list", body=body)


def chat_delete_scheduled_message(channel: str, scheduled_message_id: str) -> dict:
    """Delete a scheduled message before it is sent. scheduled_message_id comes from chat_schedule_message or chat_scheduled_messages_list."""
    return _slack_request("POST", "chat.deleteScheduledMessage", body={
        "channel": channel,
        "scheduled_message_id": scheduled_message_id,
    })


# ── Reactions ─────────────────────────────────────────────────────────────────


def reactions_add(channel: str, name: str, timestamp: str) -> dict:
    """Add an emoji reaction to a message. name: emoji name without colons (e.g. 'thumbsup'). timestamp: the message ts to react to."""
    return _slack_request("POST", "reactions.add", body={
        "channel": channel, "name": name, "timestamp": timestamp,
    })


def reactions_remove(channel: str, name: str, timestamp: str) -> dict:
    """Remove an emoji reaction from a message."""
    return _slack_request("POST", "reactions.remove", body={
        "channel": channel, "name": name, "timestamp": timestamp,
    })


def reactions_get(
    channel: str,
    timestamp: str,
    full: Optional[str] = None,
) -> dict:
    """Get reactions for a specific message. Returns list of reactions with emoji names and users who reacted."""
    params: dict[str, Any] = {"channel": channel, "timestamp": timestamp}
    if full:
        params["full"] = full
    return _slack_request("GET", "reactions.get", params=params)


# ── Pins ──────────────────────────────────────────────────────────────────────


def pins_add(channel: str, timestamp: str) -> dict:
    """Pin a message in a channel. timestamp: the message ts to pin."""
    return _slack_request("POST", "pins.add", body={"channel": channel, "timestamp": timestamp})


def pins_remove(channel: str, timestamp: str) -> dict:
    """Unpin a message from a channel."""
    return _slack_request("POST", "pins.remove", body={"channel": channel, "timestamp": timestamp})


def pins_list(channel: str) -> dict:
    """List all pinned items in a channel."""
    return _slack_request("GET", "pins.list", params={"channel": channel})


# ── Users ─────────────────────────────────────────────────────────────────────


def users_list(
    cursor: Optional[str] = None,
    limit: Optional[str] = None,
    include_locale: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List all users in the workspace. Returns display name, real name, email, status, timezone, and more. Paginate with cursor. limit: 1-1000 (default 200)."""
    params: dict[str, Any] = {}
    if cursor:
        params["cursor"] = cursor
    if limit:
        params["limit"] = limit
    if include_locale:
        params["include_locale"] = include_locale
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "users.list", params=params)


def users_info(
    user: str,
    include_locale: Optional[str] = None,
) -> dict:
    """Get detailed info about a user by their ID. Returns real name, display name, email, status, timezone, profile picture, and admin status."""
    params: dict[str, Any] = {"user": user}
    if include_locale:
        params["include_locale"] = include_locale
    return _slack_request("GET", "users.info", params=params)


def users_profile_get(
    user: Optional[str] = None,
    include_labels: Optional[str] = None,
) -> dict:
    """Get a user's profile. Returns display name, status text/emoji, title, phone, email, and custom profile fields. If user is omitted, returns the bot's own profile."""
    params: dict[str, Any] = {}
    if user:
        params["user"] = user
    if include_labels:
        params["include_labels"] = include_labels
    return _slack_request("GET", "users.profile.get", params=params)


def users_conversations(
    user: Optional[str] = None,
    types: Optional[str] = None,
    exclude_archived: Optional[str] = None,
    limit: Optional[str] = None,
    cursor: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List channels a user is a member of. If user omitted, lists the bot's channels. types: comma-separated (public_channel, private_channel, mpim, im)."""
    params: dict[str, Any] = {}
    if user:
        params["user"] = user
    if types:
        params["types"] = types
    if exclude_archived:
        params["exclude_archived"] = exclude_archived
    if limit:
        params["limit"] = limit
    if cursor:
        params["cursor"] = cursor
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "users.conversations", params=params)


def users_get_presence(user: str) -> dict:
    """Check if a user is currently active or away."""
    return _slack_request("GET", "users.getPresence", params={"user": user})


# ── Files ─────────────────────────────────────────────────────────────────────


def files_list(
    channel: Optional[str] = None,
    user: Optional[str] = None,
    ts_from: Optional[str] = None,
    ts_to: Optional[str] = None,
    types: Optional[str] = None,
    count: Optional[str] = None,
    page: Optional[str] = None,
    show_files_hidden_by_limit: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List files shared in the workspace. Filter by channel, user, date range, or file type. types: comma-separated (spaces, snippets, images, gdocs, zips, pdfs, all). Paginated with count + page."""
    params: dict[str, Any] = {}
    if channel:
        params["channel"] = channel
    if user:
        params["user"] = user
    if ts_from:
        params["ts_from"] = ts_from
    if ts_to:
        params["ts_to"] = ts_to
    if types:
        params["types"] = types
    if count:
        params["count"] = count
    if page:
        params["page"] = page
    if show_files_hidden_by_limit:
        params["show_files_hidden_by_limit"] = show_files_hidden_by_limit
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "files.list", params=params)


def files_info(
    file: str,
    count: Optional[str] = None,
    page: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: Optional[str] = None,
) -> dict:
    """Get info about a file. Returns name, type, size, URL, channels shared to, and comments."""
    params: dict[str, Any] = {"file": file}
    if count:
        params["count"] = count
    if page:
        params["page"] = page
    if cursor:
        params["cursor"] = cursor
    if limit:
        params["limit"] = limit
    return _slack_request("GET", "files.info", params=params)


def files_delete(file: str) -> dict:
    """Delete a file from the workspace."""
    return _slack_request("POST", "files.delete", body={"file": file})


def files_upload_v2(
    channel_id: str,
    content: str,
    filename: str,
    title: Optional[str] = None,
    initial_comment: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """Upload a file (text content) to a channel using the v2 upload flow. Steps: (1) get upload URL, (2) upload content, (3) complete upload. content: the text content of the file. filename: name for the file. title: optional display title."""
    creds = get_credentials("slack")
    token = creds.get("bot_token", "")
    if not token:
        return {"error": "Not authenticated. Configure Slack bot_token via gateway credentials."}

    content_bytes = content.encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        step1 = requests.get(
            f"{_SLACK_BASE}/files.getUploadURLExternal",
            headers=headers,
            params={"filename": filename, "length": str(len(content_bytes))},
            timeout=30.0,
        )
        step1_data = step1.json()
    except Exception as exc:
        return {"error": f"Step 1 (getUploadURLExternal) failed: {exc}"}

    if not step1_data.get("ok"):
        return {"error": step1_data.get("error", "getUploadURLExternal failed"), "step": 1}

    upload_url = step1_data.get("upload_url", "")
    file_id = step1_data.get("file_id", "")

    try:
        step2 = requests.post(upload_url, data=content_bytes, timeout=30.0)
        if step2.status_code >= 400:
            return {"error": f"Step 2 upload failed: HTTP {step2.status_code}", "step": 2}
    except Exception as exc:
        return {"error": f"Step 2 upload failed: {exc}", "step": 2}

    complete_body: dict[str, Any] = {
        "files": [{"id": file_id, "title": title or filename}],
        "channel_id": channel_id,
    }
    if initial_comment:
        complete_body["initial_comment"] = initial_comment
    if thread_ts:
        complete_body["thread_ts"] = thread_ts

    try:
        step3 = requests.post(
            f"{_SLACK_BASE}/files.completeUploadExternal",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json=complete_body,
            timeout=30.0,
        )
        step3_data = step3.json()
    except Exception as exc:
        return {"error": f"Step 3 (completeUploadExternal) failed: {exc}", "step": 3}

    if not step3_data.get("ok"):
        return {"error": step3_data.get("error", "completeUploadExternal failed"), "step": 3}

    return step3_data


# ── Search ────────────────────────────────────────────────────────────────────


def search_messages(
    query: str,
    sort: Optional[str] = None,
    sort_dir: Optional[str] = None,
    count: Optional[str] = None,
    page: Optional[str] = None,
    highlight: Optional[str] = None,
) -> dict:
    """Search messages across the workspace. query: search query (supports Slack search operators like 'from:@user', 'in:#channel', 'has:link', 'before:2025-01-01'). sort: 'score' or 'timestamp'. sort_dir: 'asc' or 'desc'. Paginated with count + page."""
    params: dict[str, Any] = {"query": query}
    if sort:
        params["sort"] = sort
    if sort_dir:
        params["sort_dir"] = sort_dir
    if count:
        params["count"] = count
    if page:
        params["page"] = page
    if highlight:
        params["highlight"] = highlight
    return _slack_request("GET", "search.messages", params=params)


def search_files(
    query: str,
    sort: Optional[str] = None,
    sort_dir: Optional[str] = None,
    count: Optional[str] = None,
    page: Optional[str] = None,
    highlight: Optional[str] = None,
) -> dict:
    """Search files across the workspace. query: search query (supports Slack search operators). Paginated with count + page."""
    params: dict[str, Any] = {"query": query}
    if sort:
        params["sort"] = sort
    if sort_dir:
        params["sort_dir"] = sort_dir
    if count:
        params["count"] = count
    if page:
        params["page"] = page
    if highlight:
        params["highlight"] = highlight
    return _slack_request("GET", "search.files", params=params)


# ── Bookmarks ─────────────────────────────────────────────────────────────────


def bookmarks_add(
    channel_id: str,
    title: str,
    type: str,
    link: Optional[str] = None,
    emoji: Optional[str] = None,
    entity_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> dict:
    """Add a bookmark to a channel. type: 'link' or 'file'. For link bookmarks, provide link URL. title: display name."""
    body: dict[str, Any] = {"channel_id": channel_id, "title": title, "type": type}
    if link:
        body["link"] = link
    if emoji:
        body["emoji"] = emoji
    if entity_id:
        body["entity_id"] = entity_id
    if parent_id:
        body["parent_id"] = parent_id
    return _slack_request("POST", "bookmarks.add", body=body)


def bookmarks_list(channel_id: str) -> dict:
    """List all bookmarks in a channel."""
    return _slack_request("POST", "bookmarks.list", body={"channel_id": channel_id})


def bookmarks_remove(
    channel_id: str,
    bookmark_id: str,
) -> dict:
    """Remove a bookmark from a channel."""
    return _slack_request("POST", "bookmarks.remove", body={
        "channel_id": channel_id, "bookmark_id": bookmark_id,
    })


def bookmarks_edit(
    channel_id: str,
    bookmark_id: str,
    title: Optional[str] = None,
    link: Optional[str] = None,
    emoji: Optional[str] = None,
) -> dict:
    """Edit an existing bookmark in a channel."""
    body: dict[str, Any] = {"channel_id": channel_id, "bookmark_id": bookmark_id}
    if title:
        body["title"] = title
    if link:
        body["link"] = link
    if emoji:
        body["emoji"] = emoji
    return _slack_request("POST", "bookmarks.edit", body=body)


# ── Reminders ─────────────────────────────────────────────────────────────────


def reminders_add(
    text: str,
    time: str,
    user: Optional[str] = None,
) -> dict:
    """Create a reminder. text: reminder message. time: when to remind — Unix timestamp, or natural language like 'in 15 minutes', 'every Thursday'. user: user ID to remind (defaults to the authenticated user)."""
    body: dict[str, Any] = {"text": text, "time": time}
    if user:
        body["user"] = user
    return _slack_request("POST", "reminders.add", body=body)


def reminders_list() -> dict:
    """List all reminders for the authenticated user."""
    return _slack_request("GET", "reminders.list")


def reminders_info(reminder: str) -> dict:
    """Get info about a specific reminder by its ID."""
    return _slack_request("GET", "reminders.info", params={"reminder": reminder})


def reminders_delete(reminder: str) -> dict:
    """Delete a reminder."""
    return _slack_request("POST", "reminders.delete", body={"reminder": reminder})


def reminders_complete(reminder: str) -> dict:
    """Mark a reminder as complete."""
    return _slack_request("POST", "reminders.complete", body={"reminder": reminder})


# ── Team ──────────────────────────────────────────────────────────────────────


def team_info(team: Optional[str] = None) -> dict:
    """Get info about the Slack workspace (team). Returns team name, domain, email domain, icon, and plan."""
    params: dict[str, Any] = {}
    if team:
        params["team"] = team
    return _slack_request("GET", "team.info", params=params)


# ── Emoji ─────────────────────────────────────────────────────────────────────


def emoji_list(include_categories: Optional[str] = None) -> dict:
    """List all custom emoji in the workspace. Returns a map of emoji name to URL."""
    params: dict[str, Any] = {}
    if include_categories:
        params["include_categories"] = include_categories
    return _slack_request("GET", "emoji.list", params=params)


# ── Stars (Saved Items) ──────────────────────────────────────────────────────


def stars_add(
    channel: Optional[str] = None,
    timestamp: Optional[str] = None,
    file: Optional[str] = None,
) -> dict:
    """Star (save) an item. Provide channel + timestamp for a message, or file for a file."""
    body: dict[str, Any] = {}
    if channel:
        body["channel"] = channel
    if timestamp:
        body["timestamp"] = timestamp
    if file:
        body["file"] = file
    return _slack_request("POST", "stars.add", body=body)


def stars_remove(
    channel: Optional[str] = None,
    timestamp: Optional[str] = None,
    file: Optional[str] = None,
) -> dict:
    """Remove a star (unsave) from an item."""
    body: dict[str, Any] = {}
    if channel:
        body["channel"] = channel
    if timestamp:
        body["timestamp"] = timestamp
    if file:
        body["file"] = file
    return _slack_request("POST", "stars.remove", body=body)


def stars_list(
    cursor: Optional[str] = None,
    limit: Optional[str] = None,
    count: Optional[str] = None,
    page: Optional[str] = None,
) -> dict:
    """List starred (saved) items for the authenticated user."""
    params: dict[str, Any] = {}
    if cursor:
        params["cursor"] = cursor
    if limit:
        params["limit"] = limit
    if count:
        params["count"] = count
    if page:
        params["page"] = page
    return _slack_request("GET", "stars.list", params=params)


# ── Usergroups ────────────────────────────────────────────────────────────────


def usergroups_list(
    include_count: Optional[str] = None,
    include_disabled: Optional[str] = None,
    include_users: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List user groups (e.g. @engineering, @design). Returns group handle, name, description, and member count."""
    params: dict[str, Any] = {}
    if include_count:
        params["include_count"] = include_count
    if include_disabled:
        params["include_disabled"] = include_disabled
    if include_users:
        params["include_users"] = include_users
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "usergroups.list", params=params)


def usergroups_create(
    name: str,
    handle: Optional[str] = None,
    description: Optional[str] = None,
    channels: Optional[str] = None,
    include_count: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Create a user group. name: display name. handle: the @mention handle. channels: comma-separated default channel IDs."""
    body: dict[str, Any] = {"name": name}
    if handle:
        body["handle"] = handle
    if description:
        body["description"] = description
    if channels:
        body["channels"] = channels
    if include_count:
        body["include_count"] = include_count.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "usergroups.create", body=body)


def usergroups_update(
    usergroup: str,
    name: Optional[str] = None,
    handle: Optional[str] = None,
    description: Optional[str] = None,
    channels: Optional[str] = None,
    include_count: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Update an existing user group. usergroup: the usergroup ID."""
    body: dict[str, Any] = {"usergroup": usergroup}
    if name:
        body["name"] = name
    if handle:
        body["handle"] = handle
    if description:
        body["description"] = description
    if channels:
        body["channels"] = channels
    if include_count:
        body["include_count"] = include_count.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "usergroups.update", body=body)


def usergroups_disable(
    usergroup: str,
    include_count: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Disable (deactivate) a user group. Members are removed from the group."""
    body: dict[str, Any] = {"usergroup": usergroup}
    if include_count:
        body["include_count"] = include_count.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "usergroups.disable", body=body)


def usergroups_enable(
    usergroup: str,
    include_count: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Re-enable a previously disabled user group."""
    body: dict[str, Any] = {"usergroup": usergroup}
    if include_count:
        body["include_count"] = include_count.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "usergroups.enable", body=body)


def usergroups_users_list(
    usergroup: str,
    include_disabled: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """List user IDs in a user group."""
    params: dict[str, Any] = {"usergroup": usergroup}
    if include_disabled:
        params["include_disabled"] = include_disabled
    if team_id:
        params["team_id"] = team_id
    return _slack_request("GET", "usergroups.users.list", params=params)


def usergroups_users_update(
    usergroup: str,
    users: str,
    include_count: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Replace all members of a user group. users: comma-separated list of user IDs that will be the new full member list."""
    body: dict[str, Any] = {"usergroup": usergroup, "users": users}
    if include_count:
        body["include_count"] = include_count.lower() == "true"
    if team_id:
        body["team_id"] = team_id
    return _slack_request("POST", "usergroups.users.update", body=body)


# ── Plugin definition ────────────────────────────────────────────────────────


class SlackPlugin(MCPPlugin):
    """Slack Web API plugin for MCP Gateway."""

    name = "slack"

    tools = {
        # Auth
        "auth_test": ToolDef(access="read", handler=auth_test,
            description="Test Slack authentication. Returns info about the authenticated bot/user: team, user, bot_id, and scopes."),

        # Conversations — read
        "conversations_list": ToolDef(access="read", handler=conversations_list,
            description="List channels the bot has access to. types: comma-separated 'public_channel,private_channel,mpim,im'. limit: 1-1000. Paginate with cursor."),
        "conversations_info": ToolDef(access="read", handler=conversations_info,
            description="Get detailed info about a channel/conversation by its ID."),
        "conversations_history": ToolDef(access="read", handler=conversations_history,
            description="Fetch message history from a channel. Use oldest/latest (Unix timestamps) to filter. limit: 1-1000. Paginate with cursor."),
        "conversations_replies": ToolDef(access="read", handler=conversations_replies,
            description="Get replies in a message thread. ts: thread_ts (timestamp of parent message)."),
        "conversations_members": ToolDef(access="read", handler=conversations_members,
            description="List member user IDs in a channel. Paginate with cursor."),

        # Conversations — write
        "conversations_create": ToolDef(access="write", handler=conversations_create,
            description="Create a new channel. name: lowercase, no spaces, max 80 chars. is_private: 'true' for private channel."),
        "conversations_archive": ToolDef(access="write", handler=conversations_archive,
            description="Archive a channel, making it read-only."),
        "conversations_unarchive": ToolDef(access="write", handler=conversations_unarchive,
            description="Unarchive a channel, making it active again."),
        "conversations_invite": ToolDef(access="write", handler=conversations_invite,
            description="Invite users to a channel. users: comma-separated user IDs."),
        "conversations_kick": ToolDef(access="write", handler=conversations_kick,
            description="Remove a user from a channel."),
        "conversations_join": ToolDef(access="write", handler=conversations_join,
            description="Join a public channel."),
        "conversations_leave": ToolDef(access="write", handler=conversations_leave,
            description="Leave a channel."),
        "conversations_rename": ToolDef(access="write", handler=conversations_rename,
            description="Rename a channel."),
        "conversations_set_topic": ToolDef(access="write", handler=conversations_set_topic,
            description="Set the topic of a channel."),
        "conversations_set_purpose": ToolDef(access="write", handler=conversations_set_purpose,
            description="Set the purpose/description of a channel."),
        "conversations_open": ToolDef(access="write", handler=conversations_open,
            description="Open or resume a DM or multi-person DM. Provide channel (existing) or users (comma-separated IDs for new DM)."),

        # Chat — write
        "chat_post_message": ToolDef(access="write", handler=chat_post_message,
            description="Send a message to a channel. Supports text (mrkdwn), blocks (JSON string of Block Kit), attachments, and threading (thread_ts)."),
        "chat_update": ToolDef(access="write", handler=chat_update,
            description="Update an existing message. Provide channel + ts + new text/blocks/attachments."),
        "chat_delete": ToolDef(access="write", handler=chat_delete,
            description="Delete a message by channel + ts."),
        "chat_schedule_message": ToolDef(access="write", handler=chat_schedule_message,
            description="Schedule a message for later. post_at: Unix timestamp (future, within 120 days)."),
        "chat_scheduled_messages_list": ToolDef(access="read", handler=chat_scheduled_messages_list,
            description="List scheduled messages. Optionally filter by channel."),
        "chat_delete_scheduled_message": ToolDef(access="write", handler=chat_delete_scheduled_message,
            description="Delete a scheduled message before it sends."),

        # Reactions
        "reactions_add": ToolDef(access="write", handler=reactions_add,
            description="Add an emoji reaction to a message. name: emoji without colons (e.g. 'thumbsup')."),
        "reactions_remove": ToolDef(access="write", handler=reactions_remove,
            description="Remove an emoji reaction from a message."),
        "reactions_get": ToolDef(access="read", handler=reactions_get,
            description="Get reactions on a specific message."),

        # Pins
        "pins_add": ToolDef(access="write", handler=pins_add,
            description="Pin a message in a channel."),
        "pins_remove": ToolDef(access="write", handler=pins_remove,
            description="Unpin a message from a channel."),
        "pins_list": ToolDef(access="read", handler=pins_list,
            description="List all pinned items in a channel."),

        # Users — read
        "users_list": ToolDef(access="read", handler=users_list,
            description="List all users in the workspace with profiles. limit: 1-1000. Paginate with cursor."),
        "users_info": ToolDef(access="read", handler=users_info,
            description="Get detailed info about a user by their ID."),
        "users_profile_get": ToolDef(access="read", handler=users_profile_get,
            description="Get a user's profile (status, title, phone, email, custom fields)."),
        "users_conversations": ToolDef(access="read", handler=users_conversations,
            description="List channels a user is in. types: comma-separated channel types."),
        "users_get_presence": ToolDef(access="read", handler=users_get_presence,
            description="Check if a user is currently active or away."),

        # Files
        "files_list": ToolDef(access="read", handler=files_list,
            description="List files. Filter by channel, user, date range, type. Paginated with count + page."),
        "files_info": ToolDef(access="read", handler=files_info,
            description="Get info about a file (name, type, size, URL, comments)."),
        "files_delete": ToolDef(access="write", handler=files_delete,
            description="Delete a file from the workspace."),
        "files_upload_v2": ToolDef(access="write", handler=files_upload_v2,
            description="Upload text content as a file to a channel. Uses the v2 upload flow (getUploadURLExternal + completeUploadExternal)."),

        # Search
        "search_messages": ToolDef(access="read", handler=search_messages,
            description="Search messages across the workspace. Supports Slack operators: from:@user, in:#channel, has:link, before:date."),
        "search_files": ToolDef(access="read", handler=search_files,
            description="Search files across the workspace."),

        # Bookmarks
        "bookmarks_add": ToolDef(access="write", handler=bookmarks_add,
            description="Add a bookmark to a channel. type: 'link' or 'file'."),
        "bookmarks_list": ToolDef(access="read", handler=bookmarks_list,
            description="List all bookmarks in a channel."),
        "bookmarks_remove": ToolDef(access="write", handler=bookmarks_remove,
            description="Remove a bookmark from a channel."),
        "bookmarks_edit": ToolDef(access="write", handler=bookmarks_edit,
            description="Edit an existing bookmark in a channel."),

        # Reminders
        "reminders_add": ToolDef(access="write", handler=reminders_add,
            description="Create a reminder. time: Unix timestamp or natural language like 'in 15 minutes'."),
        "reminders_list": ToolDef(access="read", handler=reminders_list,
            description="List all reminders for the authenticated user."),
        "reminders_info": ToolDef(access="read", handler=reminders_info,
            description="Get info about a specific reminder by ID."),
        "reminders_delete": ToolDef(access="write", handler=reminders_delete,
            description="Delete a reminder."),
        "reminders_complete": ToolDef(access="write", handler=reminders_complete,
            description="Mark a reminder as complete."),

        # Team
        "team_info": ToolDef(access="read", handler=team_info,
            description="Get info about the Slack workspace: name, domain, icon, plan."),

        # Emoji
        "emoji_list": ToolDef(access="read", handler=emoji_list,
            description="List all custom emoji in the workspace."),

        # Stars
        "stars_add": ToolDef(access="write", handler=stars_add,
            description="Star (save) an item. Provide channel+timestamp for a message, or file for a file."),
        "stars_remove": ToolDef(access="write", handler=stars_remove,
            description="Remove a star (unsave) from an item."),
        "stars_list": ToolDef(access="read", handler=stars_list,
            description="List starred (saved) items."),

        # Usergroups
        "usergroups_list": ToolDef(access="read", handler=usergroups_list,
            description="List user groups (@engineering, @design, etc)."),
        "usergroups_create": ToolDef(access="write", handler=usergroups_create,
            description="Create a user group. name: display name. handle: @mention handle."),
        "usergroups_update": ToolDef(access="write", handler=usergroups_update,
            description="Update an existing user group."),
        "usergroups_disable": ToolDef(access="write", handler=usergroups_disable,
            description="Disable a user group."),
        "usergroups_enable": ToolDef(access="write", handler=usergroups_enable,
            description="Re-enable a disabled user group."),
        "usergroups_users_list": ToolDef(access="read", handler=usergroups_users_list,
            description="List user IDs in a user group."),
        "usergroups_users_update": ToolDef(access="write", handler=usergroups_users_update,
            description="Replace all members of a user group. users: comma-separated user IDs."),
    }

    def health_check(self) -> dict:
        """Check if Slack API is reachable and the bot token is valid."""
        creds = get_credentials("slack")
        token = creds.get("bot_token", "")
        if not token:
            return {"status": "error", "message": "No bot_token configured"}
        try:
            resp = requests.post(
                f"{_SLACK_BASE}/auth.test",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=10.0,
            )
            data = resp.json()
            if data.get("ok"):
                return {"status": "ok", "team": data.get("team"), "user": data.get("user")}
            return {"status": "error", "message": data.get("error", "auth.test failed")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
