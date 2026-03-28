"""Gmail MCP Gateway plugin.

Wraps the Gmail API (https://developers.google.com/gmail/api/reference/rest).
Credentials from get_credentials("gmail") -> {"client_id", "client_secret", "refresh_token"}.
Handles OAuth2 token refresh automatically.
"""

import base64
import json
import time
import email.mime.text
import email.mime.multipart
import email.mime.base
import email.utils
from typing import Any, Optional

import requests

from plugin_base import MCPPlugin, ToolDef, get_credentials

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

_token_cache: dict[str, tuple[str, float]] = {}


def _get_access_token() -> str:
    creds = get_credentials("gmail")
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    refresh_token = creds.get("refresh_token", "")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError("Gmail OAuth credentials (client_id, client_secret, refresh_token) not configured.")

    cache_key = refresh_token[:16]
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    resp = requests.post(_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text}")
    data = resp.json()
    access_token = data["access_token"]
    expires_in = data.get("expires_in", 3500)
    _token_cache[cache_key] = (access_token, time.time() + expires_in - 60)
    return access_token


def _gmail(method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None, raw_body: Optional[str] = None) -> dict:
    token = _get_access_token()
    url = f"{_GMAIL_BASE}/{path}" if path else _GMAIL_BASE
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}

    kwargs: dict[str, Any] = {"headers": headers, "timeout": 60}

    if raw_body is not None:
        headers["Content-Type"] = "message/rfc822"
        kwargs["data"] = raw_body.encode("utf-8")
    elif body:
        headers["Content-Type"] = "application/json"
        kwargs["json"] = body

    if params:
        kwargs["params"] = {k: v for k, v in params.items() if v is not None}

    try:
        resp = requests.request(method, url, **kwargs)
    except requests.Timeout:
        return {"error": "Request timed out"}
    except requests.RequestException as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 204:
        return {"success": True}

    try:
        data = resp.json()
    except Exception:
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:500]}"}
        return {"success": True, "status_code": resp.status_code}

    if resp.status_code >= 400:
        err = data.get("error", {})
        return {"error": err.get("message", str(err)), "code": err.get("code", resp.status_code)}

    return data


def _build_mime_message(to: str, subject: str, body_text: Optional[str] = None,
                        body_html: Optional[str] = None, cc: Optional[str] = None,
                        bcc: Optional[str] = None, in_reply_to: Optional[str] = None,
                        references: Optional[str] = None, from_alias: Optional[str] = None) -> str:
    if body_html and body_text:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText(body_text, "plain"))
        msg.attach(email.mime.text.MIMEText(body_html, "html"))
    elif body_html:
        msg = email.mime.text.MIMEText(body_html, "html")
    else:
        msg = email.mime.text.MIMEText(body_text or "", "plain")

    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if from_alias:
        msg["From"] = from_alias
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ── Profile ───────────────────────────────────────────────────────────────────


def get_profile() -> dict:
    """Get the authenticated user's Gmail profile: email address, messages total, threads total, history ID."""
    return _gmail("GET", "profile")


# ── Messages ──────────────────────────────────────────────────────────────────


def messages_list(
    q: Optional[str] = None,
    label_ids: Optional[str] = None,
    max_results: Optional[str] = None,
    page_token: Optional[str] = None,
    include_spam_trash: Optional[str] = None,
) -> dict:
    """List messages. q: Gmail search query (same syntax as Gmail search bar, e.g. 'from:user@example.com subject:hello is:unread after:2024/01/01'). label_ids: comma-separated label IDs to filter. max_results: 1-500 (default 100). Returns message IDs; use messages_get to fetch full content."""
    params: dict[str, Any] = {}
    if q:
        params["q"] = q
    if label_ids:
        params["labelIds"] = label_ids.split(",")
    if max_results:
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if include_spam_trash:
        params["includeSpamTrash"] = include_spam_trash
    return _gmail("GET", "messages", params=params)


def messages_get(
    message_id: str,
    format: Optional[str] = None,
    metadata_headers: Optional[str] = None,
) -> dict:
    """Get a message by ID. format: 'full' (default, parsed), 'metadata' (headers only), 'raw' (RFC 2822), or 'minimal' (IDs+labels only). metadata_headers: comma-separated header names to include when format=metadata (e.g. 'From,To,Subject,Date')."""
    params: dict[str, Any] = {}
    if format:
        params["format"] = format
    if metadata_headers:
        params["metadataHeaders"] = metadata_headers.split(",")
    return _gmail("GET", f"messages/{message_id}", params=params)


def messages_send(
    to: str,
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    from_alias: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> dict:
    """Send an email. to/cc/bcc: comma-separated addresses. Provide body_text and/or body_html. from_alias: send-as address if configured. thread_id: set to reply within a thread. in_reply_to/references: Message-ID headers for threading."""
    raw = _build_mime_message(to, subject, body_text, body_html, cc, bcc, in_reply_to, references, from_alias)
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return _gmail("POST", "messages/send", body=payload)


def messages_modify(
    message_id: str,
    add_label_ids: Optional[str] = None,
    remove_label_ids: Optional[str] = None,
) -> dict:
    """Modify labels on a message. add_label_ids/remove_label_ids: comma-separated label IDs. Common labels: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH, CATEGORY_PERSONAL, CATEGORY_SOCIAL, CATEGORY_PROMOTIONS, CATEGORY_UPDATES."""
    body: dict[str, Any] = {}
    if add_label_ids:
        body["addLabelIds"] = [lid.strip() for lid in add_label_ids.split(",")]
    if remove_label_ids:
        body["removeLabelIds"] = [lid.strip() for lid in remove_label_ids.split(",")]
    return _gmail("POST", f"messages/{message_id}/modify", body=body)


def messages_trash(message_id: str) -> dict:
    """Move a message to Trash. It will be permanently deleted after 30 days."""
    return _gmail("POST", f"messages/{message_id}/trash")


def messages_untrash(message_id: str) -> dict:
    """Remove a message from Trash back to its previous location."""
    return _gmail("POST", f"messages/{message_id}/untrash")


def messages_delete(message_id: str) -> dict:
    """Permanently delete a message (bypasses Trash). This action is irreversible."""
    return _gmail("DELETE", f"messages/{message_id}")


def messages_batch_modify(
    message_ids: str,
    add_label_ids: Optional[str] = None,
    remove_label_ids: Optional[str] = None,
) -> dict:
    """Modify labels on multiple messages at once. message_ids: comma-separated message IDs. add_label_ids/remove_label_ids: comma-separated label IDs."""
    body: dict[str, Any] = {"ids": [mid.strip() for mid in message_ids.split(",")]}
    if add_label_ids:
        body["addLabelIds"] = [lid.strip() for lid in add_label_ids.split(",")]
    if remove_label_ids:
        body["removeLabelIds"] = [lid.strip() for lid in remove_label_ids.split(",")]
    return _gmail("POST", "messages/batchModify", body=body)


def messages_batch_delete(message_ids: str) -> dict:
    """Permanently delete multiple messages. message_ids: comma-separated IDs. Irreversible."""
    body = {"ids": [mid.strip() for mid in message_ids.split(",")]}
    return _gmail("POST", "messages/batchDelete", body=body)


def messages_import(
    raw_rfc822: str,
    internal_date_source: Optional[str] = None,
    never_mark_spam: Optional[str] = None,
    process_for_calendar: Optional[str] = None,
    deleted: Optional[str] = None,
) -> dict:
    """Import a message (like IMAP APPEND). raw_rfc822: full RFC 2822 message text. Used for migration/import scenarios."""
    raw_b64 = base64.urlsafe_b64encode(raw_rfc822.encode("utf-8")).decode("ascii")
    params: dict[str, Any] = {}
    if internal_date_source:
        params["internalDateSource"] = internal_date_source
    if never_mark_spam:
        params["neverMarkSpam"] = never_mark_spam
    if process_for_calendar:
        params["processForCalendar"] = process_for_calendar
    if deleted:
        params["deleted"] = deleted
    return _gmail("POST", "messages/import", params=params, body={"raw": raw_b64})


def messages_insert(
    raw_rfc822: str,
    internal_date_source: Optional[str] = None,
    deleted: Optional[str] = None,
) -> dict:
    """Insert a message directly into the mailbox (bypasses sending). raw_rfc822: full RFC 2822 message text. Useful for archiving or creating messages without sending."""
    raw_b64 = base64.urlsafe_b64encode(raw_rfc822.encode("utf-8")).decode("ascii")
    params: dict[str, Any] = {}
    if internal_date_source:
        params["internalDateSource"] = internal_date_source
    if deleted:
        params["deleted"] = deleted
    return _gmail("POST", "messages/insert", params=params, body={"raw": raw_b64})


# ── Threads ───────────────────────────────────────────────────────────────────


def threads_list(
    q: Optional[str] = None,
    label_ids: Optional[str] = None,
    max_results: Optional[str] = None,
    page_token: Optional[str] = None,
    include_spam_trash: Optional[str] = None,
) -> dict:
    """List email threads. q: Gmail search query. label_ids: comma-separated. max_results: 1-500. Returns thread IDs with snippet previews."""
    params: dict[str, Any] = {}
    if q:
        params["q"] = q
    if label_ids:
        params["labelIds"] = label_ids.split(",")
    if max_results:
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if include_spam_trash:
        params["includeSpamTrash"] = include_spam_trash
    return _gmail("GET", "threads", params=params)


def threads_get(
    thread_id: str,
    format: Optional[str] = None,
    metadata_headers: Optional[str] = None,
) -> dict:
    """Get a thread with all its messages. format: 'full', 'metadata', 'minimal'. Returns the complete conversation."""
    params: dict[str, Any] = {}
    if format:
        params["format"] = format
    if metadata_headers:
        params["metadataHeaders"] = metadata_headers.split(",")
    return _gmail("GET", f"threads/{thread_id}", params=params)


def threads_modify(
    thread_id: str,
    add_label_ids: Optional[str] = None,
    remove_label_ids: Optional[str] = None,
) -> dict:
    """Modify labels on all messages in a thread. add_label_ids/remove_label_ids: comma-separated."""
    body: dict[str, Any] = {}
    if add_label_ids:
        body["addLabelIds"] = [lid.strip() for lid in add_label_ids.split(",")]
    if remove_label_ids:
        body["removeLabelIds"] = [lid.strip() for lid in remove_label_ids.split(",")]
    return _gmail("POST", f"threads/{thread_id}/modify", body=body)


def threads_trash(thread_id: str) -> dict:
    """Move an entire thread to Trash."""
    return _gmail("POST", f"threads/{thread_id}/trash")


def threads_untrash(thread_id: str) -> dict:
    """Remove a thread from Trash."""
    return _gmail("POST", f"threads/{thread_id}/untrash")


def threads_delete(thread_id: str) -> dict:
    """Permanently delete a thread and all its messages. Irreversible."""
    return _gmail("DELETE", f"threads/{thread_id}")


# ── Labels ────────────────────────────────────────────────────────────────────


def labels_list() -> dict:
    """List all labels (folders/categories) in the mailbox. Returns system labels (INBOX, SENT, etc.) and custom labels."""
    return _gmail("GET", "labels")


def labels_get(label_id: str) -> dict:
    """Get details of a specific label including name, type, message/thread counts, visibility settings."""
    return _gmail("GET", f"labels/{label_id}")


def labels_create(
    name: str,
    label_list_visibility: Optional[str] = None,
    message_list_visibility: Optional[str] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
) -> dict:
    """Create a new label. name: label name (use '/' for nesting, e.g. 'Work/Projects'). label_list_visibility: 'labelShow','labelShowIfUnread','labelHide'. message_list_visibility: 'show','hide'. Colors: hex like '#000000'."""
    body: dict[str, Any] = {"name": name}
    if label_list_visibility:
        body["labelListVisibility"] = label_list_visibility
    if message_list_visibility:
        body["messageListVisibility"] = message_list_visibility
    if background_color or text_color:
        body["color"] = {}
        if background_color:
            body["color"]["backgroundColor"] = background_color
        if text_color:
            body["color"]["textColor"] = text_color
    return _gmail("POST", "labels", body=body)


def labels_update(
    label_id: str,
    name: Optional[str] = None,
    label_list_visibility: Optional[str] = None,
    message_list_visibility: Optional[str] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
) -> dict:
    """Update a label. All fields are optional — only provided fields are changed."""
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if label_list_visibility:
        body["labelListVisibility"] = label_list_visibility
    if message_list_visibility:
        body["messageListVisibility"] = message_list_visibility
    if background_color or text_color:
        body["color"] = {}
        if background_color:
            body["color"]["backgroundColor"] = background_color
        if text_color:
            body["color"]["textColor"] = text_color
    return _gmail("PATCH", f"labels/{label_id}", body=body)


def labels_delete(label_id: str) -> dict:
    """Delete a label. Messages with this label are not deleted, they just lose the label."""
    return _gmail("DELETE", f"labels/{label_id}")


# ── Drafts ────────────────────────────────────────────────────────────────────


def drafts_list(
    max_results: Optional[str] = None,
    page_token: Optional[str] = None,
    q: Optional[str] = None,
    include_spam_trash: Optional[str] = None,
) -> dict:
    """List drafts. q: search query to filter. Returns draft IDs and message snippets."""
    params: dict[str, Any] = {}
    if max_results:
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if q:
        params["q"] = q
    if include_spam_trash:
        params["includeSpamTrash"] = include_spam_trash
    return _gmail("GET", "drafts", params=params)


def drafts_get(draft_id: str, format: Optional[str] = None) -> dict:
    """Get a draft by ID. format: 'full', 'metadata', 'minimal', 'raw'."""
    params: dict[str, Any] = {}
    if format:
        params["format"] = format
    return _gmail("GET", f"drafts/{draft_id}", params=params)


def drafts_create(
    to: str,
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    from_alias: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> dict:
    """Create a draft email. Same parameters as messages_send. The draft is saved but not sent."""
    raw = _build_mime_message(to, subject, body_text, body_html, cc, bcc, in_reply_to, references, from_alias)
    payload: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    return _gmail("POST", "drafts", body=payload)


def drafts_update(
    draft_id: str,
    to: str,
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    from_alias: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> dict:
    """Replace a draft's content. Provide the full new message content."""
    raw = _build_mime_message(to, subject, body_text, body_html, cc, bcc, in_reply_to, references, from_alias)
    payload: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    return _gmail("PUT", f"drafts/{draft_id}", body=payload)


def drafts_send(draft_id: str) -> dict:
    """Send an existing draft. The draft is removed and a sent message is created."""
    return _gmail("POST", "drafts/send", body={"id": draft_id})


def drafts_delete(draft_id: str) -> dict:
    """Permanently delete a draft. Does not send it."""
    return _gmail("DELETE", f"drafts/{draft_id}")


# ── History ───────────────────────────────────────────────────────────────────


def history_list(
    start_history_id: str,
    label_id: Optional[str] = None,
    max_results: Optional[str] = None,
    page_token: Optional[str] = None,
    history_types: Optional[str] = None,
) -> dict:
    """List mailbox changes since a history ID (from getProfile or a previous sync). history_types: comma-separated from 'messageAdded,messageDeleted,labelAdded,labelRemoved'. Used for incremental sync."""
    params: dict[str, Any] = {"startHistoryId": start_history_id}
    if label_id:
        params["labelId"] = label_id
    if max_results:
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if history_types:
        params["historyTypes"] = history_types.split(",")
    return _gmail("GET", "history", params=params)


# ── Settings: Vacation / Out-of-Office ────────────────────────────────────────


def settings_get_vacation() -> dict:
    """Get vacation (out-of-office) auto-reply settings: enabled, subject, body, date range, contacts-only flag."""
    return _gmail("GET", "settings/vacation")


def settings_update_vacation(
    enable_auto_reply: str,
    response_subject: Optional[str] = None,
    response_body_plain_text: Optional[str] = None,
    response_body_html: Optional[str] = None,
    restrict_to_contacts: Optional[str] = None,
    restrict_to_domain: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    """Set vacation auto-reply. enable_auto_reply: 'true'/'false'. start_time/end_time: Unix timestamp in ms. restrict_to_contacts/restrict_to_domain: 'true'/'false'."""
    body: dict[str, Any] = {"enableAutoReply": enable_auto_reply.lower() == "true"}
    if response_subject:
        body["responseSubject"] = response_subject
    if response_body_plain_text:
        body["responseBodyPlainText"] = response_body_plain_text
    if response_body_html:
        body["responseBodyHtml"] = response_body_html
    if restrict_to_contacts:
        body["restrictToContacts"] = restrict_to_contacts.lower() == "true"
    if restrict_to_domain:
        body["restrictToDomain"] = restrict_to_domain.lower() == "true"
    if start_time:
        body["startTime"] = start_time
    if end_time:
        body["endTime"] = end_time
    return _gmail("PUT", "settings/vacation", body=body)


# ── Settings: General ─────────────────────────────────────────────────────────


def settings_get_auto_forwarding() -> dict:
    """Get auto-forwarding settings."""
    return _gmail("GET", "settings/autoForwarding")


def settings_get_imap() -> dict:
    """Get IMAP settings (enabled, expunge behavior, max folder size)."""
    return _gmail("GET", "settings/imap")


def settings_update_imap(
    enabled: str,
    auto_expunge: Optional[str] = None,
    expunge_behavior: Optional[str] = None,
    max_folder_size: Optional[str] = None,
) -> dict:
    """Update IMAP settings. enabled: 'true'/'false'. expunge_behavior: 'archive','deleteForever','trash'. max_folder_size: 0-10000."""
    body: dict[str, Any] = {"enabled": enabled.lower() == "true"}
    if auto_expunge:
        body["autoExpunge"] = auto_expunge.lower() == "true"
    if expunge_behavior:
        body["expungeBehavior"] = expunge_behavior
    if max_folder_size:
        body["maxFolderSize"] = int(max_folder_size)
    return _gmail("PUT", "settings/imap", body=body)


def settings_get_pop() -> dict:
    """Get POP settings."""
    return _gmail("GET", "settings/pop")


def settings_update_pop(
    access_window: Optional[str] = None,
    disposition: Optional[str] = None,
) -> dict:
    """Update POP settings. access_window: 'disabled','fromNowOn','allMail'. disposition: 'leaveInInbox','archive','trash','markRead'."""
    body: dict[str, Any] = {}
    if access_window:
        body["accessWindow"] = access_window
    if disposition:
        body["disposition"] = disposition
    return _gmail("PUT", "settings/pop", body=body)


def settings_get_language() -> dict:
    """Get language settings for the mailbox."""
    return _gmail("GET", "settings/language")


# ── Settings: Filters ─────────────────────────────────────────────────────────


def filters_list() -> dict:
    """List all email filters (rules that automatically label, archive, delete, etc.)."""
    return _gmail("GET", "settings/filters")


def filters_get(filter_id: str) -> dict:
    """Get a specific filter by ID."""
    return _gmail("GET", f"settings/filters/{filter_id}")


def filters_create(
    from_address: Optional[str] = None,
    to_address: Optional[str] = None,
    subject: Optional[str] = None,
    query: Optional[str] = None,
    negated_query: Optional[str] = None,
    has_attachment: Optional[str] = None,
    size: Optional[str] = None,
    size_comparison: Optional[str] = None,
    add_label_ids: Optional[str] = None,
    remove_label_ids: Optional[str] = None,
    forward: Optional[str] = None,
) -> dict:
    """Create a mail filter. Criteria fields (all optional): from_address, to_address, subject, query (Gmail search), negated_query, has_attachment ('true'/'false'), size (bytes), size_comparison ('smaller'/'larger'). Actions: add_label_ids/remove_label_ids (comma-separated), forward (email address)."""
    criteria: dict[str, Any] = {}
    if from_address:
        criteria["from"] = from_address
    if to_address:
        criteria["to"] = to_address
    if subject:
        criteria["subject"] = subject
    if query:
        criteria["query"] = query
    if negated_query:
        criteria["negatedQuery"] = negated_query
    if has_attachment:
        criteria["hasAttachment"] = has_attachment.lower() == "true"
    if size:
        criteria["size"] = int(size)
    if size_comparison:
        criteria["sizeComparison"] = size_comparison

    action: dict[str, Any] = {}
    if add_label_ids:
        action["addLabelIds"] = [lid.strip() for lid in add_label_ids.split(",")]
    if remove_label_ids:
        action["removeLabelIds"] = [lid.strip() for lid in remove_label_ids.split(",")]
    if forward:
        action["forward"] = forward

    return _gmail("POST", "settings/filters", body={"criteria": criteria, "action": action})


def filters_delete(filter_id: str) -> dict:
    """Delete a mail filter."""
    return _gmail("DELETE", f"settings/filters/{filter_id}")


# ── Settings: Forwarding Addresses ────────────────────────────────────────────


def forwarding_addresses_list() -> dict:
    """List forwarding addresses and their verification status."""
    return _gmail("GET", "settings/forwardingAddresses")


def forwarding_addresses_get(forwarding_email: str) -> dict:
    """Get a specific forwarding address."""
    return _gmail("GET", f"settings/forwardingAddresses/{forwarding_email}")


def forwarding_addresses_create(forwarding_email: str) -> dict:
    """Add a new forwarding address. Gmail will send a verification email to the address."""
    return _gmail("POST", "settings/forwardingAddresses", body={"forwardingEmail": forwarding_email})


def forwarding_addresses_delete(forwarding_email: str) -> dict:
    """Remove a forwarding address."""
    return _gmail("DELETE", f"settings/forwardingAddresses/{forwarding_email}")


# ── Settings: Send-As (Aliases) ──────────────────────────────────────────────


def send_as_list() -> dict:
    """List send-as aliases (email addresses you can send from). Returns verification status, signature, and reply-to settings."""
    return _gmail("GET", "settings/sendAs")


def send_as_get(send_as_email: str) -> dict:
    """Get a specific send-as alias by email address."""
    return _gmail("GET", f"settings/sendAs/{send_as_email}")


def send_as_create(
    send_as_email: str,
    display_name: Optional[str] = None,
    reply_to_address: Optional[str] = None,
    signature: Optional[str] = None,
    is_default: Optional[str] = None,
    treat_as_alias: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[str] = None,
    smtp_username: Optional[str] = None,
    smtp_password: Optional[str] = None,
    smtp_security_mode: Optional[str] = None,
) -> dict:
    """Add a send-as alias. send_as_email: the address. For external SMTP, provide smtp_* fields. smtp_security_mode: 'none','ssl','starttls'. Gmail sends a verification email."""
    body: dict[str, Any] = {"sendAsEmail": send_as_email}
    if display_name:
        body["displayName"] = display_name
    if reply_to_address:
        body["replyToAddress"] = reply_to_address
    if signature:
        body["signature"] = signature
    if is_default:
        body["isDefault"] = is_default.lower() == "true"
    if treat_as_alias:
        body["treatAsAlias"] = treat_as_alias.lower() == "true"
    if smtp_host:
        smtp_msa: dict[str, Any] = {"host": smtp_host}
        if smtp_port:
            smtp_msa["port"] = int(smtp_port)
        if smtp_username:
            smtp_msa["username"] = smtp_username
        if smtp_password:
            smtp_msa["password"] = smtp_password
        if smtp_security_mode:
            smtp_msa["securityMode"] = smtp_security_mode
        body["smtpMsa"] = smtp_msa
    return _gmail("POST", "settings/sendAs", body=body)


def send_as_update(
    send_as_email: str,
    display_name: Optional[str] = None,
    reply_to_address: Optional[str] = None,
    signature: Optional[str] = None,
    is_default: Optional[str] = None,
) -> dict:
    """Update a send-as alias (display name, reply-to, signature, default)."""
    body: dict[str, Any] = {"sendAsEmail": send_as_email}
    if display_name:
        body["displayName"] = display_name
    if reply_to_address:
        body["replyToAddress"] = reply_to_address
    if signature is not None:
        body["signature"] = signature
    if is_default:
        body["isDefault"] = is_default.lower() == "true"
    return _gmail("PATCH", f"settings/sendAs/{send_as_email}", body=body)


def send_as_delete(send_as_email: str) -> dict:
    """Remove a send-as alias. Cannot remove the primary address."""
    return _gmail("DELETE", f"settings/sendAs/{send_as_email}")


def send_as_verify(send_as_email: str) -> dict:
    """Send a verification email to a send-as alias. Required before it can be used."""
    return _gmail("POST", f"settings/sendAs/{send_as_email}/verify")


# ── Settings: Delegates ───────────────────────────────────────────────────────


def delegates_list() -> dict:
    """List delegates who have access to this mailbox."""
    return _gmail("GET", "settings/delegates")


def delegates_get(delegate_email: str) -> dict:
    """Get a specific delegate's status (pending, accepted, etc.)."""
    return _gmail("GET", f"settings/delegates/{delegate_email}")


def delegates_create(delegate_email: str) -> dict:
    """Add a delegate to this mailbox. They'll be able to read/send on your behalf. Gmail sends a confirmation email."""
    return _gmail("POST", "settings/delegates", body={"delegateEmail": delegate_email})


def delegates_delete(delegate_email: str) -> dict:
    """Remove a delegate from this mailbox."""
    return _gmail("DELETE", f"settings/delegates/{delegate_email}")


# ── Attachments ───────────────────────────────────────────────────────────────


def attachments_get(message_id: str, attachment_id: str) -> dict:
    """Get an attachment's data by message ID and attachment ID. Returns base64-encoded data. Find attachment IDs in the message parts when fetching with messages_get."""
    return _gmail("GET", f"messages/{message_id}/attachments/{attachment_id}")


# ── Plugin class ──────────────────────────────────────────────────────────────


class GmailPlugin(MCPPlugin):
    name = "gmail"

    def __init__(self):
        self.tools = {
            # Profile
            "get_profile": ToolDef(access="read", handler=get_profile,
                description="Get Gmail profile: email, messages total, threads total, history ID."),

            # Messages
            "messages_list": ToolDef(access="read", handler=messages_list,
                description="List messages. q: Gmail search query (e.g. 'from:x is:unread after:2024/01/01'). max_results: 1-500."),
            "messages_get": ToolDef(access="read", handler=messages_get,
                description="Get a message by ID. format: full/metadata/raw/minimal."),
            "messages_send": ToolDef(access="write", handler=messages_send,
                description="Send an email. Supports text and HTML bodies, CC, BCC, threading, and send-as aliases."),
            "messages_modify": ToolDef(access="write", handler=messages_modify,
                description="Add/remove labels on a message (e.g. mark read, star, archive)."),
            "messages_trash": ToolDef(access="write", handler=messages_trash,
                description="Move a message to Trash."),
            "messages_untrash": ToolDef(access="write", handler=messages_untrash,
                description="Remove a message from Trash."),
            "messages_delete": ToolDef(access="admin", handler=messages_delete,
                description="Permanently delete a message (irreversible, bypasses Trash)."),
            "messages_batch_modify": ToolDef(access="write", handler=messages_batch_modify,
                description="Modify labels on multiple messages at once."),
            "messages_batch_delete": ToolDef(access="admin", handler=messages_batch_delete,
                description="Permanently delete multiple messages (irreversible)."),
            "messages_import": ToolDef(access="write", handler=messages_import,
                description="Import a message (like IMAP APPEND). For migration scenarios."),
            "messages_insert": ToolDef(access="write", handler=messages_insert,
                description="Insert a message into the mailbox without sending."),

            # Threads
            "threads_list": ToolDef(access="read", handler=threads_list,
                description="List email threads. q: Gmail search query. max_results: 1-500."),
            "threads_get": ToolDef(access="read", handler=threads_get,
                description="Get a thread with all its messages."),
            "threads_modify": ToolDef(access="write", handler=threads_modify,
                description="Modify labels on all messages in a thread."),
            "threads_trash": ToolDef(access="write", handler=threads_trash,
                description="Move a thread to Trash."),
            "threads_untrash": ToolDef(access="write", handler=threads_untrash,
                description="Remove a thread from Trash."),
            "threads_delete": ToolDef(access="admin", handler=threads_delete,
                description="Permanently delete a thread (irreversible)."),

            # Labels
            "labels_list": ToolDef(access="read", handler=labels_list,
                description="List all labels (system + custom)."),
            "labels_get": ToolDef(access="read", handler=labels_get,
                description="Get label details: name, type, message/thread counts."),
            "labels_create": ToolDef(access="write", handler=labels_create,
                description="Create a label. Use '/' in name for nesting (e.g. 'Work/Projects')."),
            "labels_update": ToolDef(access="write", handler=labels_update,
                description="Update a label's name, visibility, or colors."),
            "labels_delete": ToolDef(access="write", handler=labels_delete,
                description="Delete a label. Messages keep their content, just lose the label."),

            # Drafts
            "drafts_list": ToolDef(access="read", handler=drafts_list,
                description="List drafts. q: search query to filter."),
            "drafts_get": ToolDef(access="read", handler=drafts_get,
                description="Get a draft by ID."),
            "drafts_create": ToolDef(access="write", handler=drafts_create,
                description="Create a draft email (saved, not sent)."),
            "drafts_update": ToolDef(access="write", handler=drafts_update,
                description="Replace a draft's content."),
            "drafts_send": ToolDef(access="write", handler=drafts_send,
                description="Send an existing draft."),
            "drafts_delete": ToolDef(access="write", handler=drafts_delete,
                description="Delete a draft (does not send it)."),

            # History
            "history_list": ToolDef(access="read", handler=history_list,
                description="List mailbox changes since a history ID. For incremental sync."),

            # Settings: Vacation
            "settings_get_vacation": ToolDef(access="read", handler=settings_get_vacation,
                description="Get out-of-office auto-reply settings."),
            "settings_update_vacation": ToolDef(access="write", handler=settings_update_vacation,
                description="Enable/disable and configure out-of-office auto-reply."),

            # Settings: General
            "settings_get_auto_forwarding": ToolDef(access="read", handler=settings_get_auto_forwarding,
                description="Get auto-forwarding settings."),
            "settings_get_imap": ToolDef(access="read", handler=settings_get_imap,
                description="Get IMAP settings."),
            "settings_update_imap": ToolDef(access="write", handler=settings_update_imap,
                description="Update IMAP settings (enable/disable, expunge behavior)."),
            "settings_get_pop": ToolDef(access="read", handler=settings_get_pop,
                description="Get POP settings."),
            "settings_update_pop": ToolDef(access="write", handler=settings_update_pop,
                description="Update POP settings (access window, disposition)."),
            "settings_get_language": ToolDef(access="read", handler=settings_get_language,
                description="Get language settings for the mailbox."),

            # Settings: Filters
            "filters_list": ToolDef(access="read", handler=filters_list,
                description="List all email filters (auto-labeling, archiving rules)."),
            "filters_get": ToolDef(access="read", handler=filters_get,
                description="Get a specific filter by ID."),
            "filters_create": ToolDef(access="write", handler=filters_create,
                description="Create a mail filter with criteria (from, subject, query) and actions (add labels, forward)."),
            "filters_delete": ToolDef(access="write", handler=filters_delete,
                description="Delete a mail filter."),

            # Settings: Forwarding Addresses
            "forwarding_addresses_list": ToolDef(access="read", handler=forwarding_addresses_list,
                description="List forwarding addresses and their verification status."),
            "forwarding_addresses_get": ToolDef(access="read", handler=forwarding_addresses_get,
                description="Get a specific forwarding address."),
            "forwarding_addresses_create": ToolDef(access="write", handler=forwarding_addresses_create,
                description="Add a forwarding address (Gmail sends verification email)."),
            "forwarding_addresses_delete": ToolDef(access="write", handler=forwarding_addresses_delete,
                description="Remove a forwarding address."),

            # Settings: Send-As (Aliases)
            "send_as_list": ToolDef(access="read", handler=send_as_list,
                description="List send-as aliases (addresses you can send from)."),
            "send_as_get": ToolDef(access="read", handler=send_as_get,
                description="Get a send-as alias by email."),
            "send_as_create": ToolDef(access="write", handler=send_as_create,
                description="Add a send-as alias. Supports external SMTP."),
            "send_as_update": ToolDef(access="write", handler=send_as_update,
                description="Update a send-as alias (display name, reply-to, signature)."),
            "send_as_delete": ToolDef(access="write", handler=send_as_delete,
                description="Remove a send-as alias."),
            "send_as_verify": ToolDef(access="write", handler=send_as_verify,
                description="Send verification email for a send-as alias."),

            # Settings: Delegates
            "delegates_list": ToolDef(access="read", handler=delegates_list,
                description="List delegates with access to this mailbox."),
            "delegates_get": ToolDef(access="read", handler=delegates_get,
                description="Get a delegate's status."),
            "delegates_create": ToolDef(access="admin", handler=delegates_create,
                description="Add a delegate (they can read/send on your behalf)."),
            "delegates_delete": ToolDef(access="admin", handler=delegates_delete,
                description="Remove a delegate."),

            # Attachments
            "attachments_get": ToolDef(access="read", handler=attachments_get,
                description="Get attachment data (base64) by message ID and attachment ID."),
        }

    def health_check(self) -> dict:
        try:
            result = get_profile()
            if "error" in result:
                return {"status": "error", "message": result["error"]}
            return {"status": "ok", "email": result.get("emailAddress")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
