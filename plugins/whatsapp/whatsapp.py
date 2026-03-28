"""WhatsApp MCP plugin — SQLite + Go bridge layer.

Access key management functions (seed_access_key, resolve_api_key, etc.) are
retained for backward compatibility but are NOT used for gateway auth —
the MCP Gateway handles authentication and scoping itself.
"""

import json
import os.path
import sqlite3
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set, Tuple

import requests

from . import audio

MESSAGES_DB_PATH = os.environ.get(
    "WHATSAPP_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store", "messages.db"),
)
_BRIDGE_BASE = os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:7481")
WHATSAPP_API_BASE_URL = os.environ.get("WHATSAPP_API_BASE_URL") or (
    _BRIDGE_BASE.rstrip("/") + "/api" if not _BRIDGE_BASE.rstrip("/").endswith("/api") else _BRIDGE_BASE.rstrip("/")
)
_BRIDGE_API_KEY = os.environ.get("WHATSAPP_BRIDGE_API_KEY", "")


def _bridge_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if _BRIDGE_API_KEY:
        headers["Authorization"] = f"Bearer {_BRIDGE_API_KEY}"
    return headers


# ---------------------------------------------------------------------------
# Access key management (backward compatibility only — NOT used for gateway auth)
# ---------------------------------------------------------------------------

def ensure_access_tables():
    """Create access key tables if they don't exist."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS access_keys (
                id TEXT PRIMARY KEY,
                api_key TEXT UNIQUE NOT NULL,
                label TEXT DEFAULT '',
                scope_all BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS access_key_scopes (
                key_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                PRIMARY KEY (key_id, chat_jid),
                FOREIGN KEY (key_id) REFERENCES access_keys(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        if "conn" in locals():
            conn.close()


def seed_access_key(key_id: str, api_key: str, label: str, scope_all: bool):
    """Insert an access key if it doesn't already exist."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO access_keys (id, api_key, label, scope_all) VALUES (?, ?, ?, ?)",
            (key_id, api_key, label, scope_all),
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"Failed to seed access key {key_id}: {e}")
    finally:
        if "conn" in locals():
            conn.close()


def resolve_api_key(api_key: str) -> Optional[dict]:
    """Look up an access key by its api_key value. Returns {"id": ..., "scope_all": bool} or None."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        row = conn.execute("SELECT id, scope_all FROM access_keys WHERE api_key = ?", (api_key,)).fetchone()
        if row:
            return {"id": row[0], "scope_all": bool(row[1])}
        return None
    except sqlite3.Error:
        return None
    finally:
        if "conn" in locals():
            conn.close()


def resolve_key_by_id_and_token(key_id: str, token: str) -> Optional[dict]:
    """Look up an access key by id and verify the token matches."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        row = conn.execute(
            "SELECT id, scope_all FROM access_keys WHERE id = ? AND api_key = ?", (key_id, token)
        ).fetchone()
        if row:
            return {"id": row[0], "scope_all": bool(row[1])}
        return None
    except sqlite3.Error:
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_key_scopes(key_id: str) -> Set[str]:
    """Get the set of allowed chat JIDs for an access key."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        rows = conn.execute("SELECT chat_jid FROM access_key_scopes WHERE key_id = ?", (key_id,)).fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        if "conn" in locals():
            conn.close()


def get_allowed_jids(key_id: str, scope_all: bool) -> Optional[Set[str]]:
    """Return the set of allowed JIDs for a key, or None if scope_all (no filtering)."""
    if scope_all:
        return None
    return get_key_scopes(key_id)


def create_access_key(key_id: str, label: str, scope_all: bool = False, api_key: str = None) -> dict:
    """Create a new access key. Returns the key info including the generated api_key."""
    if api_key is None:
        api_key = secrets.token_urlsafe(32)
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        conn.execute(
            "INSERT INTO access_keys (id, api_key, label, scope_all) VALUES (?, ?, ?, ?)",
            (key_id, api_key, label, scope_all),
        )
        conn.commit()
        return {"success": True, "id": key_id, "api_key": api_key, "label": label, "scope_all": scope_all}
    except sqlite3.IntegrityError:
        return {"success": False, "message": f"Key '{key_id}' already exists"}
    except sqlite3.Error as e:
        return {"success": False, "message": str(e)}
    finally:
        if "conn" in locals():
            conn.close()


def list_access_keys() -> List[dict]:
    """List all access keys (without exposing api_key values)."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        rows = conn.execute(
            "SELECT id, label, scope_all, created_at FROM access_keys ORDER BY created_at"
        ).fetchall()
        return [{"id": r[0], "label": r[1], "scope_all": bool(r[2]), "created_at": r[3]} for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if "conn" in locals():
            conn.close()


def delete_access_key(key_id: str) -> dict:
    """Delete an access key and its scopes."""
    if key_id == "g0d":
        return {"success": False, "message": "Cannot delete g0d key"}
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        conn.execute("DELETE FROM access_key_scopes WHERE key_id = ?", (key_id,))
        conn.execute("DELETE FROM access_keys WHERE id = ?", (key_id,))
        conn.commit()
        return {"success": True}
    except sqlite3.Error as e:
        return {"success": False, "message": str(e)}
    finally:
        if "conn" in locals():
            conn.close()


def add_key_scopes(key_id: str, chat_jids: List[str]) -> dict:
    """Add chat JIDs to an access key's scope."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        added = 0
        for jid in chat_jids:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO access_key_scopes (key_id, chat_jid) VALUES (?, ?)",
                    (key_id, jid),
                )
                added += 1
            except sqlite3.Error:
                pass
        conn.commit()
        return {"success": True, "added": added}
    except sqlite3.Error as e:
        return {"success": False, "message": str(e)}
    finally:
        if "conn" in locals():
            conn.close()


def remove_key_scope(key_id: str, chat_jid: str) -> dict:
    """Remove a chat JID from an access key's scope."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        conn.execute(
            "DELETE FROM access_key_scopes WHERE key_id = ? AND chat_jid = ?",
            (key_id, chat_jid),
        )
        conn.commit()
        return {"success": True}
    except sqlite3.Error as e:
        return {"success": False, "message": str(e)}
    finally:
        if "conn" in locals():
            conn.close()


def _jid_filter(column: str, allowed_jids: Optional[Set[str]]) -> Tuple[str, list]:
    """Build a SQL fragment + params for JID-based scope filtering.
    Returns ("", []) if allowed_jids is None (no filtering).
    Returns ("AND 1=0", []) if allowed_jids is empty (deny all)."""
    if allowed_jids is None:
        return ("", [])
    if not allowed_jids:
        return ("AND 1=0", [])
    placeholders = ",".join("?" * len(allowed_jids))
    return (f"AND {column} IN ({placeholders})", list(allowed_jids))


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None


@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        return self.jid.endswith("@g.us")


@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str


@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]


def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (sender_jid,))
        result = cursor.fetchone()
        if not result and "@" in sender_jid:
            phone_part = sender_jid.split("@")[0]
            cursor.execute("SELECT name FROM chats WHERE jid LIKE ? LIMIT 1", (f"%{phone_part}%",))
            result = cursor.fetchone()
        return result[0] if result and result[0] else sender_jid
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if "conn" in locals():
            conn.close()


def format_message(message: Message, show_chat_info: bool = True) -> str:
    output = ""
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
    content_prefix = ""
    if hasattr(message, "media_type") and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output


def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> str:
    if not messages:
        return "No messages to display."
    return "".join(format_message(m, show_chat_info) for m in messages)


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
    allowed_jids: Optional[Set[str]] = None,
) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        query_parts = [
            "SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"
        ]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        scope_sql, scope_params = _jid_filter("messages.chat_jid", allowed_jids)
        if scope_sql:
            where_clauses.append(scope_sql.lstrip("AND "))
            params.extend(scope_params)
        if after:
            try:
                after_dt = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            where_clauses.append("messages.timestamp > ?")
            params.append(after_dt)
        if before:
            try:
                before_dt = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            where_clauses.append("messages.timestamp < ?")
            params.append(before_dt)
        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        cursor.execute(" ".join(query_parts), tuple(params))
        rows = cursor.fetchall()
        result = []
        for msg in rows:
            result.append(
                Message(
                    timestamp=datetime.fromisoformat(msg[0]),
                    sender=msg[1],
                    chat_name=msg[2],
                    content=msg[3],
                    is_from_me=msg[4],
                    chat_jid=msg[5],
                    id=msg[6],
                    media_type=msg[7],
                )
            )
        if include_context and result:
            messages_with_context = []
            for msg in result:
                ctx = get_message_context(msg.id, context_before, context_after, allowed_jids=allowed_jids)
                messages_with_context.extend(ctx.before)
                messages_with_context.append(ctx.message)
                messages_with_context.extend(ctx.after)
            return format_messages_list(messages_with_context, show_chat_info=True)
        return format_messages_list(result, show_chat_info=True)
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return ""
    finally:
        if "conn" in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    allowed_jids: Optional[Set[str]] = None,
) -> MessageContext:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages JOIN chats ON messages.chat_jid = chats.jid WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
        if allowed_jids is not None and msg_data[5] not in allowed_jids:
            raise ValueError(f"Message with ID {message_id} not found")
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8],
        )
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ? ORDER BY messages.timestamp DESC LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        before_messages = [
            Message(
                timestamp=datetime.fromisoformat(m[0]),
                sender=m[1],
                chat_name=m[2],
                content=m[3],
                is_from_me=m[4],
                chat_jid=m[5],
                id=m[6],
                media_type=m[7],
            )
            for m in cursor.fetchall()
        ]
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ? ORDER BY messages.timestamp ASC LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        after_messages = [
            Message(
                timestamp=datetime.fromisoformat(m[0]),
                sender=m[1],
                chat_name=m[2],
                content=m[3],
                is_from_me=m[4],
                chat_jid=m[5],
                id=m[6],
                media_type=m[7],
            )
            for m in cursor.fetchall()
        ]
        return MessageContext(message=target_message, before=before_messages, after=after_messages)
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if "conn" in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    allowed_jids: Optional[Set[str]] = None,
) -> List[Chat]:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        query_parts = ["""
            SELECT chats.jid, chats.name, chats.last_message_time, messages.content as last_message, messages.sender as last_sender, messages.is_from_me as last_is_from_me FROM chats
        """]
        if include_last_message:
            query_parts.append("LEFT JOIN messages ON chats.jid = messages.chat_jid AND chats.last_message_time = messages.timestamp")
        where_clauses = []
        params = []
        scope_sql, scope_params = _jid_filter("chats.jid", allowed_jids)
        if scope_sql:
            where_clauses.append(scope_sql.lstrip("AND "))
            params.extend(scope_params)
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, page * limit])
        cursor.execute(" ".join(query_parts), tuple(params))
        return [
            Chat(
                jid=r[0],
                name=r[1],
                last_message_time=datetime.fromisoformat(r[2]) if r[2] else None,
                last_message=r[3],
                last_sender=r[4],
                last_is_from_me=r[5],
            )
            for r in cursor.fetchall()
        ]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def search_contacts(query: str, allowed_jids: Optional[Set[str]] = None) -> List[Contact]:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        search_pattern = "%" + query + "%"
        scope_sql, scope_params = _jid_filter("jid", allowed_jids)
        cursor.execute(f"""
            SELECT DISTINCT jid, name FROM chats
            WHERE (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?)) AND jid NOT LIKE '%@g.us' {scope_sql}
            ORDER BY name, jid LIMIT 50
        """, (search_pattern, search_pattern, *scope_params))
        return [
            Contact(phone_number=r[0].split("@")[0], name=r[1], jid=r[0])
            for r in cursor.fetchall()
        ]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0, allowed_jids: Optional[Set[str]] = None) -> List[Chat]:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        scope_sql, scope_params = _jid_filter("c.jid", allowed_jids)
        cursor.execute(f"""
            SELECT DISTINCT c.jid, c.name, c.last_message_time, m.content as last_message, m.sender as last_sender, m.is_from_me as last_is_from_me
            FROM chats c JOIN messages m ON c.jid = m.chat_jid
            WHERE (m.sender = ? OR c.jid = ?) {scope_sql}
            ORDER BY c.last_message_time DESC LIMIT ? OFFSET ?
        """, (jid, jid, *scope_params, limit, page * limit))
        return [
            Chat(
                jid=r[0],
                name=r[1],
                last_message_time=datetime.fromisoformat(r[2]) if r[2] else None,
                last_message=r[3],
                last_sender=r[4],
                last_is_from_me=r[5],
            )
            for r in cursor.fetchall()
        ]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def get_last_interaction(jid: str, allowed_jids: Optional[Set[str]] = None) -> Optional[str]:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        scope_sql, scope_params = _jid_filter("c.jid", allowed_jids)
        cursor.execute(f"""
            SELECT m.timestamp, m.sender, c.name, m.content, m.is_from_me, c.jid, m.id, m.media_type
            FROM messages m JOIN chats c ON m.chat_jid = c.jid
            WHERE (m.sender = ? OR c.jid = ?) {scope_sql}
            ORDER BY m.timestamp DESC LIMIT 1
        """, (jid, jid, *scope_params))
        msg_data = cursor.fetchone()
        if not msg_data:
            return None
        return format_message(
            Message(
                timestamp=datetime.fromisoformat(msg_data[0]),
                sender=msg_data[1],
                chat_name=msg_data[2],
                content=msg_data[3],
                is_from_me=msg_data[4],
                chat_jid=msg_data[5],
                id=msg_data[6],
                media_type=msg_data[7],
            )
        )
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True, allowed_jids: Optional[Set[str]] = None) -> Optional[Chat]:
    if allowed_jids is not None and chat_jid not in allowed_jids:
        return None
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        query = "SELECT c.jid, c.name, c.last_message_time, m.content as last_message, m.sender as last_sender, m.is_from_me as last_is_from_me FROM chats c"
        if include_last_message:
            query += " LEFT JOIN messages m ON c.jid = m.chat_jid AND c.last_message_time = m.timestamp"
        query += " WHERE c.jid = ?"
        cursor.execute(query, (chat_jid,))
        r = cursor.fetchone()
        if not r:
            return None
        return Chat(
            jid=r[0],
            name=r[1],
            last_message_time=datetime.fromisoformat(r[2]) if r[2] else None,
            last_message=r[3],
            last_sender=r[4],
            last_is_from_me=r[5],
        )
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str, allowed_jids: Optional[Set[str]] = None) -> Optional[Chat]:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        scope_sql, scope_params = _jid_filter("c.jid", allowed_jids)
        cursor.execute(f"""
            SELECT c.jid, c.name, c.last_message_time, m.content as last_message, m.sender as last_sender, m.is_from_me as last_is_from_me
            FROM chats c LEFT JOIN messages m ON c.jid = m.chat_jid AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us' {scope_sql} LIMIT 1
        """, (f"%{sender_phone_number}%", *scope_params))
        r = cursor.fetchone()
        if not r:
            return None
        return Chat(
            jid=r[0],
            name=r[1],
            last_message_time=datetime.fromisoformat(r[2]) if r[2] else None,
            last_message=r[3],
            last_sender=r[4],
            last_is_from_me=r[5],
        )
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "message": message}
        response = requests.post(url, json=payload, headers=_bridge_headers())
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        if not media_path:
            return False, "Media path must be provided"
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "media_path": media_path}
        response = requests.post(url, json=payload, headers=_bridge_headers())
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        if not media_path:
            return False, "Media path must be provided"
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "media_path": media_path}
        response = requests.post(url, json=payload, headers=_bridge_headers())
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {"message_id": message_id, "chat_jid": chat_jid}
        response = requests.post(url, json=payload, headers=_bridge_headers())
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                return result.get("path")
        return None
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"Download error: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error: {e}")
        return None


def backfill_groups() -> dict:
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/backfill-groups", headers=_bridge_headers())
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def backfill_contacts() -> dict:
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/backfill-contacts", headers=_bridge_headers())
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def backfill_group_participants() -> dict:
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/backfill-group-participants", headers=_bridge_headers())
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def request_history_sync() -> dict:
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/request-history-sync", headers=_bridge_headers())
        return resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}


def get_group_members(group_jid: str, allowed_jids: Optional[Set[str]] = None) -> List[dict]:
    if allowed_jids is not None and group_jid not in allowed_jids:
        return []
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT member_jid, display_name, is_admin, is_super_admin FROM group_members WHERE group_jid = ? ORDER BY display_name",
            (group_jid,),
        )
        return [
            {
                "jid": r[0],
                "phone": r[0].split("@")[0] if "@" in r[0] else r[0],
                "display_name": r[1] or "",
                "is_admin": bool(r[2]),
                "is_super_admin": bool(r[3]),
            }
            for r in cursor.fetchall()
        ]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()
