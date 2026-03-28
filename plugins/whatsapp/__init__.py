"""WhatsApp plugin for MCP Gateway.

Multi-account support: each account maps to a separate Go bridge instance
and SQLite message DB. Pass `account` to any tool to select which WhatsApp
number to use. Omit for the default account.

Uses get_data_scopes("whatsapp") for JID-based data scoping.
"""

import base64
import mimetypes
import os
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from plugin_base import MCPPlugin, ToolDef, get_data_scopes, get_context

from . import whatsapp as _wa
from .whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_send_audio_message,
    download_media as whatsapp_download_media,
    get_group_members as whatsapp_get_group_members,
    backfill_groups as whatsapp_backfill_groups,
    backfill_contacts as whatsapp_backfill_contacts,
    backfill_group_participants as whatsapp_backfill_group_participants,
    request_history_sync as whatsapp_request_history_sync,
    create_access_key as wa_create_access_key,
    list_access_keys as wa_list_access_keys,
    delete_access_key as wa_delete_access_key,
    add_key_scopes as wa_add_key_scopes,
    remove_key_scope as wa_remove_key_scope,
    get_key_scopes as wa_get_key_scopes,
)

# ─── Multi-account config ──────────────────────────────────────────────────────

_WA_ACCOUNTS: dict[str, dict[str, str]] = {
    "default": {
        "bridge_url": os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:7481"),
        "db_path": os.environ.get("WHATSAPP_DB_PATH", "/data/store/messages.db"),
    },
    "second": {
        "bridge_url": os.environ.get("WHATSAPP_BRIDGE_URL_2", "http://localhost:7482"),
        "db_path": os.environ.get("WHATSAPP_DB_PATH_2", "/data/store2/messages.db"),
    },
}

_wa_lock = threading.Lock()


def _get_allowed_accounts() -> Optional[set]:
    """Return allowed WhatsApp account names, or None for unrestricted.

    If no account scopes are configured for the key, returns None (backward compat).
    If account scopes ARE set, returns the set of allowed account names.
    """
    ctx = get_context()
    if ctx.is_admin:
        return None
    db_path = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT scope_value FROM key_plugin_scopes WHERE key_id = ? AND plugin = 'whatsapp' AND scope_type = 'account'",
        (ctx.key_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    return {r["scope_value"] for r in rows}


def _with_account(account: str, func, *args, **kwargs):
    """Call func with whatsapp.py globals pointing to the correct account."""
    acct = account or "default"
    cfg = _WA_ACCOUNTS.get(acct)
    if not cfg:
        return {"error": f"Unknown WhatsApp account '{acct}'. Available: {list(_WA_ACCOUNTS.keys())}"}

    allowed = _get_allowed_accounts()
    if allowed is not None and acct not in allowed:
        return {"error": f"Access denied: not authorized to use WhatsApp account '{acct}'."}

    with _wa_lock:
        orig_db = _wa.MESSAGES_DB_PATH
        orig_api = _wa.WHATSAPP_API_BASE_URL
        orig_bridge = _wa._BRIDGE_BASE
        orig_key = _wa._BRIDGE_API_KEY
        try:
            _wa.MESSAGES_DB_PATH = cfg["db_path"]
            _wa._BRIDGE_BASE = cfg["bridge_url"]
            _wa.WHATSAPP_API_BASE_URL = cfg["bridge_url"].rstrip("/") + "/api"
            return func(*args, **kwargs)
        finally:
            _wa.MESSAGES_DB_PATH = orig_db
            _wa.WHATSAPP_API_BASE_URL = orig_api
            _wa._BRIDGE_BASE = orig_bridge
            _wa._BRIDGE_API_KEY = orig_key


# ─── Scope helpers ─────────────────────────────────────────────────────────────

def _get_scope_jids():
    scopes = get_data_scopes("whatsapp")
    return scopes  # None = full access, set = restricted


def _check_send_scope(recipient):
    scopes = get_data_scopes("whatsapp")
    if scopes is None:
        return None
    jid = recipient if '@' in recipient else f"{recipient}@s.whatsapp.net"
    if jid not in scopes:
        return {"success": False, "message": f"Access denied: not authorized to message {recipient}"}
    return None


def _require_admin():
    ctx = get_context()
    if not ctx.is_admin:
        return {"error": "This operation requires admin access"}
    return None


def _to_dict(obj):
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    if not hasattr(obj, "__dataclass_fields__"):
        return obj
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ─── Tool handlers ─────────────────────────────────────────────────────────────

def pair_phone(phone: str, account: str = "") -> Dict[str, Any]:
    """Start phone-number pairing for an unpaired account. Returns an 8-digit code
    to enter on the phone (WhatsApp > Linked Devices > Link with phone number)."""
    import requests as req
    acct = account or "default"
    cfg = _WA_ACCOUNTS.get(acct)
    if not cfg:
        return {"error": f"Unknown account '{acct}'. Available: {list(_WA_ACCOUNTS.keys())}"}
    allowed = _get_allowed_accounts()
    if allowed is not None and acct not in allowed:
        return {"error": f"Access denied: not authorized for account '{acct}'."}
    bridge_url = cfg["bridge_url"].rstrip("/")
    bridge_key = os.environ.get("WHATSAPP_BRIDGE_API_KEY", "")
    try:
        r = req.post(
            f"{bridge_url}/api/pair-phone",
            json={"phone": phone},
            headers={"Authorization": f"Bearer {bridge_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def list_accounts() -> Dict[str, Any]:
    """List configured WhatsApp accounts and their connection status."""
    import requests as req
    allowed = _get_allowed_accounts()
    result = []
    for name, cfg in _WA_ACCOUNTS.items():
        if allowed is not None and name not in allowed:
            continue
        info: dict[str, Any] = {"account": name, "bridge_url": cfg["bridge_url"]}
        try:
            db = sqlite3.connect(cfg["db_path"], timeout=3)
            msg_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chat_count = db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            db.close()
            info["messages"] = msg_count
            info["chats"] = chat_count
            info["paired"] = msg_count > 0
        except Exception:
            info["messages"] = 0
            info["chats"] = 0
            info["paired"] = False
        try:
            r = req.get(f"{cfg['bridge_url'].rstrip('/')}/api/backfill-contacts", timeout=3,
                        headers={"Content-Type": "application/json"})
            info["bridge_status"] = "online" if r.status_code < 500 else "error"
        except Exception:
            info["bridge_status"] = "offline"
        result.append(info)
    return {"accounts": result}


def search_contacts(query: str, account: str = "") -> List[Dict[str, Any]]:
    return _with_account(account, lambda: _to_dict(
        whatsapp_search_contacts(query, allowed_jids=_get_scope_jids())))


def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    account: str = "",
) -> str:
    return _with_account(account, lambda: whatsapp_list_messages(
        after=after, before=before, sender_phone_number=sender_phone_number,
        chat_jid=chat_jid, query=query, limit=limit, page=page,
        include_context=include_context, context_before=context_before,
        context_after=context_after, allowed_jids=_get_scope_jids()))


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    account: str = "",
) -> List[Dict[str, Any]]:
    return _with_account(account, lambda: _to_dict(whatsapp_list_chats(
        query=query, limit=limit, page=page,
        include_last_message=include_last_message, sort_by=sort_by,
        allowed_jids=_get_scope_jids())))


def get_chat(chat_jid: str, include_last_message: bool = True, account: str = "") -> Dict[str, Any]:
    return _with_account(account, lambda: _to_dict(
        whatsapp_get_chat(chat_jid, include_last_message, allowed_jids=_get_scope_jids())))


def get_direct_chat_by_contact(sender_phone_number: str, account: str = "") -> Dict[str, Any]:
    return _with_account(account, lambda: _to_dict(
        whatsapp_get_direct_chat_by_contact(sender_phone_number, allowed_jids=_get_scope_jids())))


def get_contact_chats(jid: str, limit: int = 20, page: int = 0, account: str = "") -> List[Dict[str, Any]]:
    return _with_account(account, lambda: _to_dict(
        whatsapp_get_contact_chats(jid, limit, page, allowed_jids=_get_scope_jids())))


def get_last_interaction(jid: str, account: str = "") -> str:
    return _with_account(account, lambda: whatsapp_get_last_interaction(jid, allowed_jids=_get_scope_jids()))


def get_message_context(message_id: str, before: int = 5, after: int = 5, account: str = "") -> Dict[str, Any]:
    return _with_account(account, lambda: _to_dict(
        whatsapp_get_message_context(message_id, before, after, allowed_jids=_get_scope_jids())))


def send_message(recipient: str, message: str, account: str = "") -> Dict[str, Any]:
    if not recipient:
        return {"success": False, "message": "Recipient must be provided"}
    scope_err = _check_send_scope(recipient)
    if scope_err:
        return scope_err
    def _do():
        success, status_message = whatsapp_send_message(recipient, message)
        return {"success": success, "message": status_message}
    return _with_account(account, _do)


def send_file(recipient: str, media_path: str, account: str = "") -> Dict[str, Any]:
    scope_err = _check_send_scope(recipient)
    if scope_err:
        return scope_err
    def _do():
        success, status_message = whatsapp_send_file(recipient, media_path)
        return {"success": success, "message": status_message}
    return _with_account(account, _do)


def send_audio_message(recipient: str, media_path: str, account: str = "") -> Dict[str, Any]:
    scope_err = _check_send_scope(recipient)
    if scope_err:
        return scope_err
    def _do():
        success, status_message = whatsapp_send_audio_message(recipient, media_path)
        return {"success": success, "message": status_message}
    return _with_account(account, _do)


def download_media(message_id: str, chat_jid: str, account: str = "") -> Union[list, Dict[str, Any]]:
    allowed = _get_scope_jids()
    if allowed is not None and chat_jid not in allowed:
        return {"success": False, "message": "Access denied: chat not in your scope"}

    def _do():
        file_path = whatsapp_download_media(message_id, chat_jid)
        if not file_path:
            return {"success": False, "message": "Failed to download media"}
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        if content_type.startswith("image/") and os.path.isfile(file_path):
            try:
                file_size = os.path.getsize(file_path)
                if file_size < 10 * 1024 * 1024:
                    with open(file_path, "rb") as f:
                        b64_data = base64.b64encode(f.read()).decode()
                    return {
                        "success": True,
                        "image_base64": b64_data,
                        "content_type": content_type,
                        "file_path": file_path,
                    }
            except OSError:
                pass
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path,
            "content_type": content_type,
        }
    return _with_account(account, _do)


def get_group_members(group_jid: str, account: str = "") -> Dict[str, Any]:
    return _with_account(account, lambda: {
        "group_jid": group_jid,
        "members": whatsapp_get_group_members(group_jid, allowed_jids=_get_scope_jids()),
        "count": len(whatsapp_get_group_members(group_jid, allowed_jids=_get_scope_jids())),
    })


def sync_data(account: str = "") -> Dict[str, Any]:
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    def _do():
        results = {}
        results["contacts"] = whatsapp_backfill_contacts()
        results["groups"] = whatsapp_backfill_groups()
        results["group_participants"] = whatsapp_backfill_group_participants()
        results["history_sync"] = whatsapp_request_history_sync()
        results["success"] = all(
            r.get("success", False) for r in [
                results["contacts"], results["groups"],
                results["group_participants"], results["history_sync"],
            ]
        )
        return results
    return _with_account(account, _do)


def manage_access_keys(
    action: str,
    key_id: Optional[str] = None,
    label: Optional[str] = None,
    scope_all: bool = False,
    chat_jids: Optional[List[str]] = None,
    account: str = "",
) -> Dict[str, Any]:
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    def _do():
        if action == "list_keys":
            keys = wa_list_access_keys()
            return {"success": True, "keys": keys}
        if not key_id:
            return {"error": "key_id is required for this action"}
        if action == "create_key":
            return wa_create_access_key(key_id, label or "", scope_all)
        if action == "delete_key":
            return wa_delete_access_key(key_id)
        if action == "list_scopes":
            scopes = wa_get_key_scopes(key_id)
            return {"success": True, "key_id": key_id, "scopes": list(scopes)}
        if action == "add_scopes":
            if not chat_jids:
                return {"error": "chat_jids list is required for add_scopes"}
            return wa_add_key_scopes(key_id, chat_jids)
        if action == "remove_scope":
            if not chat_jids or len(chat_jids) == 0:
                return {"error": "chat_jids with one JID is required for remove_scope"}
            return wa_remove_key_scope(key_id, chat_jids[0])
        return {"error": f"Unknown action: {action}"}
    return _with_account(account, _do)


# ─── Plugin definition ─────────────────────────────────────────────────────────

class WhatsAppPlugin(MCPPlugin):
    name = "whatsapp"
    tools = {
        "pair_phone": ToolDef(
            access="admin",
            handler=pair_phone,
            description="Pair a new WhatsApp account via phone number. Returns an 8-digit code to enter on the phone.",
        ),
        "list_accounts": ToolDef(
            access="read",
            handler=list_accounts,
            description="List configured WhatsApp accounts, their pairing status, and message counts.",
        ),
        "search_contacts": ToolDef(
            access="read",
            handler=search_contacts,
            description="Search for individual WhatsApp contacts (not groups) by name or phone number. Pass account for multi-account.",
        ),
        "list_messages": ToolDef(
            access="read",
            handler=list_messages,
            description="Search and retrieve WhatsApp messages across all chats or within a specific chat. Pass account for multi-account.",
        ),
        "list_chats": ToolDef(
            access="read",
            handler=list_chats,
            description="List WhatsApp chats — both individual conversations AND group chats. Pass account for multi-account.",
        ),
        "get_chat": ToolDef(
            access="read",
            handler=get_chat,
            description="Get metadata for a specific WhatsApp chat by its JID. Pass account for multi-account.",
        ),
        "get_direct_chat_by_contact": ToolDef(
            access="read",
            handler=get_direct_chat_by_contact,
            description="Look up the 1-on-1 (direct) WhatsApp chat with a specific person by their phone number. Pass account for multi-account.",
        ),
        "get_contact_chats": ToolDef(
            access="read",
            handler=get_contact_chats,
            description="Find all chats (including group chats) where a specific contact has sent messages. Pass account for multi-account.",
        ),
        "get_last_interaction": ToolDef(
            access="read",
            handler=get_last_interaction,
            description="Get the single most recent message exchanged with a contact (sent or received). Pass account for multi-account.",
        ),
        "get_message_context": ToolDef(
            access="read",
            handler=get_message_context,
            description="Get the messages surrounding a specific message by its ID. Pass account for multi-account.",
        ),
        "download_media": ToolDef(
            access="read",
            handler=download_media,
            description="Download a media attachment (image, video, audio, document) from a WhatsApp message. Pass account for multi-account.",
        ),
        "get_group_members": ToolDef(
            access="read",
            handler=get_group_members,
            description="Get the list of members in a WhatsApp group, including their roles (admin/member). Pass account for multi-account.",
        ),
        "send_message": ToolDef(
            access="write",
            handler=send_message,
            description="Send a text message via WhatsApp to a person or group. Pass account for multi-account.",
        ),
        "send_file": ToolDef(
            access="write",
            handler=send_file,
            description="Send an image, video, or document file via WhatsApp. Pass account for multi-account.",
        ),
        "send_audio_message": ToolDef(
            access="write",
            handler=send_audio_message,
            description="Send an audio file as a playable voice message bubble in WhatsApp. Pass account for multi-account.",
        ),
        "sync_data": ToolDef(
            access="admin",
            handler=sync_data,
            description="Sync all WhatsApp data from the server: contacts, groups, group members, and message history. Pass account for multi-account.",
        ),
        "manage_access_keys": ToolDef(
            access="admin",
            handler=manage_access_keys,
            description="Manage access keys that control who can see which WhatsApp chats (legacy/local keys). Pass account for multi-account.",
        ),
    }

    def health_check(self) -> dict:
        import requests as req
        results = {}
        for name, cfg in _WA_ACCOUNTS.items():
            try:
                r = req.get(f"{cfg['bridge_url'].rstrip('/')}/api/backfill-contacts",
                            timeout=3, headers={"Content-Type": "application/json"})
                results[name] = "online" if r.status_code < 500 else f"HTTP {r.status_code}"
            except Exception as e:
                results[name] = f"offline: {e}"
        return {"status": "ok" if any(v == "online" for v in results.values()) else "error", "bridges": results}
