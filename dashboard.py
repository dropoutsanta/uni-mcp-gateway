"""MCP Gateway — server-rendered web dashboard.

Single-file module containing all routes, HTML templates (as Python strings),
inline CSS (light + dark mode), and minimal inline JS.  No external template
engine required.

Exported entry point: ``get_dashboard_routes()`` returns a list of Starlette
Route objects.  Call ``init_dashboard(plugin_map, tool_registry)`` once at
startup to seed plugin metadata.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

import auth
import external_mcp

# ── Configuration ─────────────────────────────────────────────────────────────

_DB_PATH = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")
_BASE_URL = os.environ.get("MCP_BASE_URL", "http://localhost:8080")

_plugin_registry: dict[str, dict] = {}
_all_plugins: list[str] = []


_ext_register_cb = None
_ext_unregister_cb = None
_tool_registry_ref: dict = {}


def init_dashboard(plugin_map: dict, tool_registry: dict, register_cb=None, unregister_cb=None) -> None:
    global _plugin_registry, _all_plugins, _ext_register_cb, _ext_unregister_cb, _tool_registry_ref
    _plugin_registry = {
        name: {
            "tool_count": sum(1 for tn in tool_registry if tn.startswith(f"{name}_")),
        }
        for name in plugin_map
    }
    _all_plugins = sorted(plugin_map.keys())
    _tool_registry_ref = tool_registry
    if register_cb:
        _ext_register_cb = register_cb
    if unregister_cb:
        _ext_unregister_cb = unregister_cb


# ── DB helper ─────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Cookie auth helpers ───────────────────────────────────────────────────────

def _get_key_from_cookie(request: Request) -> dict | None:
    token = request.cookies.get("gw_token")
    if not token:
        return None
    return auth.resolve_key(token)


# ── Pagination helper ─────────────────────────────────────────────────────────

def _paginate(request: Request, default_per_page: int = 50) -> tuple[int, int, int]:
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = min(500, max(10, int(request.query_params.get("per_page", str(default_per_page)))))
    except ValueError:
        per_page = default_per_page
    offset = (page - 1) * per_page
    return page, per_page, offset


def _pagination_html(page: int, per_page: int, total: int, base_path: str, extra_params: dict | None = None) -> str:
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total_pages <= 1:
        return ""

    params = extra_params or {}
    params["per_page"] = str(per_page)

    def page_url(p: int) -> str:
        q = {**params, "page": str(p)}
        return f"{base_path}?{urlencode(q)}"

    parts = []
    parts.append('<div class="pagination">')

    if page > 1:
        parts.append(f'<a href="{page_url(page - 1)}" class="btn btn-ghost btn-sm">&laquo; Prev</a>')
    else:
        parts.append('<span class="btn btn-ghost btn-sm" style="opacity:0.3">&laquo; Prev</span>')

    window = 2
    for p in range(1, total_pages + 1):
        if p == 1 or p == total_pages or abs(p - page) <= window:
            if p == page:
                parts.append(f'<span class="btn btn-primary btn-sm">{p}</span>')
            else:
                parts.append(f'<a href="{page_url(p)}" class="btn btn-ghost btn-sm">{p}</a>')
        elif (p == 2 and page > window + 2) or (p == total_pages - 1 and page < total_pages - window - 1):
            parts.append('<span class="btn btn-ghost btn-sm" style="opacity:0.3">…</span>')

    if page < total_pages:
        parts.append(f'<a href="{page_url(page + 1)}" class="btn btn-ghost btn-sm">Next &raquo;</a>')
    else:
        parts.append('<span class="btn btn-ghost btn-sm" style="opacity:0.3">Next &raquo;</span>')

    parts.append(f'<span class="pagination-info">{total:,} total</span>')
    parts.append('</div>')
    return "".join(parts)


# ── Stat queries ──────────────────────────────────────────────────────────────

def _get_stats(key_id: str, is_admin: bool) -> dict:
    conn = _db()
    key_clause = "" if is_admin else "WHERE key_id = ?"
    params: list = [] if is_admin else [key_id]

    total = conn.execute(
        f"SELECT COUNT(*) as c FROM audit_log {key_clause}", params
    ).fetchone()["c"]

    errors = conn.execute(
        f"SELECT COUNT(*) as c FROM audit_log {key_clause}{' AND' if key_clause else ' WHERE'} success = 0",
        params,
    ).fetchone()["c"]

    today_clause = f"{key_clause}{' AND' if key_clause else ' WHERE'} date(timestamp) = date('now')"
    today = conn.execute(
        f"SELECT COUNT(*) as c FROM audit_log {today_clause}", params
    ).fetchone()["c"]

    week_clause = f"{key_clause}{' AND' if key_clause else ' WHERE'} timestamp >= datetime('now', '-7 days')"
    week = conn.execute(
        f"SELECT COUNT(*) as c FROM audit_log {week_clause}", params
    ).fetchone()["c"]

    conn.close()
    error_rate = round((errors / total * 100), 1) if total > 0 else 0.0
    return {"total": total, "today": today, "week": week, "error_rate": error_rate}


def _get_recent_activity(key_id: str, is_admin: bool, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    conn = _db()
    if is_admin:
        total = conn.execute("SELECT COUNT(*) as c FROM audit_log").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    else:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM audit_log WHERE key_id = ?", (key_id,)
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE key_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (key_id, limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def _get_all_audit(limit: int = 50, offset: int = 0, key_filter: str = "", plugin_filter: str = "", tool_filter: str = "") -> tuple[list[dict], int]:
    conn = _db()
    clauses = []
    params: list = []
    if key_filter:
        clauses.append("key_id = ?")
        params.append(key_filter)
    if plugin_filter:
        clauses.append("plugin = ?")
        params.append(plugin_filter)
    if tool_filter:
        clauses.append("tool_name LIKE ?")
        params.append(f"%{tool_filter}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(f"SELECT COUNT(*) as c FROM audit_log {where}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def _get_distinct_audit_values() -> tuple[list[str], list[str]]:
    conn = _db()
    keys = [r[0] for r in conn.execute("SELECT DISTINCT key_id FROM audit_log WHERE key_id IS NOT NULL ORDER BY key_id").fetchall()]
    plugins = [r[0] for r in conn.execute("SELECT DISTINCT plugin FROM audit_log WHERE plugin IS NOT NULL ORDER BY plugin").fetchall()]
    conn.close()
    return keys, plugins


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _html(body: str, title: str = "MCP Gateway") -> HTMLResponse:
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
{_CSS}
</head>
<body>
{body}
{_THEME_JS}
</body>
</html>"""
    return HTMLResponse(page)


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#ffffff;--bg2:#f8f9fa;--bg3:#f0f1f3;--text:#1a1a1a;--text2:#6b7280;
  --border:#e5e7eb;--accent:#3b82f6;--accent-hover:#2563eb;
  --success:#22c55e;--error:#ef4444;--error-bg:rgba(239,68,68,0.06);
  --card-shadow:0 1px 3px rgba(0,0,0,0.1);
  --pill-read:#dbeafe;--pill-read-text:#1e40af;
  --pill-write:#fef3c7;--pill-write-text:#92400e;
  --pill-admin:#fce7f3;--pill-admin-text:#9d174d;
  --modal-bg:rgba(0,0,0,0.4);
}
[data-theme="dark"]{
  --bg:#0a0a0a;--bg2:#141414;--bg3:#1c1c1c;--text:#e5e7eb;--text2:#9ca3af;
  --border:#2a2a2a;--accent:#3b82f6;--accent-hover:#60a5fa;
  --success:#4ade80;--error:#f87171;--error-bg:rgba(239,68,68,0.1);
  --card-shadow:0 1px 3px rgba(0,0,0,0.4);
  --pill-read:#1e3a5f;--pill-read-text:#93c5fd;
  --pill-write:#422006;--pill-write-text:#fcd34d;
  --pill-admin:#4a1942;--pill-admin-text:#f9a8d4;
  --modal-bg:rgba(0,0,0,0.7);
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0a0a0a;--bg2:#141414;--bg3:#1c1c1c;--text:#e5e7eb;--text2:#9ca3af;
    --border:#2a2a2a;--accent:#3b82f6;--accent-hover:#60a5fa;
    --success:#4ade80;--error:#f87171;--error-bg:rgba(239,68,68,0.1);
    --card-shadow:0 1px 3px rgba(0,0,0,0.4);
    --pill-read:#1e3a5f;--pill-read-text:#93c5fd;
    --pill-write:#422006;--pill-write-text:#fcd34d;
    --pill-admin:#4a1942;--pill-admin-text:#f9a8d4;
    --modal-bg:rgba(0,0,0,0.7);
  }
}
html{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);line-height:1.5;
  min-height:100%;-webkit-font-smoothing:antialiased;
}
.mono{font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:0.85em}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Layout */
.container{max-width:1100px;margin:0 auto;padding:0 20px}
.header{
  border-bottom:1px solid var(--border);padding:12px 0;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;
}
.header-left{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
.header-right{display:flex;align-items:center;gap:8px}
.header h1{font-size:15px;font-weight:600;white-space:nowrap}
.header .key-id{
  background:var(--bg2);border:1px solid var(--border);
  padding:2px 8px;border-radius:4px;font-size:12px;
}
.badge-admin{
  background:var(--pill-admin);color:var(--pill-admin-text);
  padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;
  text-transform:uppercase;letter-spacing:0.5px;
}
.nav-link{
  color:var(--text2);text-decoration:none;font-size:13px;
  padding:4px 10px;border-radius:6px;transition:all 0.15s;
}
.nav-link:hover{color:var(--text);background:var(--bg2);text-decoration:none}
.nav-link.active{color:var(--accent);font-weight:500}

/* Buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;
  padding:8px 16px;border:none;border-radius:6px;cursor:pointer;
  font-size:13px;font-weight:500;font-family:inherit;
  transition:all 0.15s;text-decoration:none;
}
.btn:hover{text-decoration:none}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-hover)}
.btn-danger{background:var(--error);color:#fff}
.btn-danger:hover{opacity:0.9}
.btn-ghost{
  background:transparent;color:var(--text2);
  border:1px solid var(--border);
}
.btn-ghost:hover{color:var(--text);border-color:var(--text2)}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-icon{
  width:32px;height:32px;padding:0;border-radius:6px;
  background:transparent;border:1px solid var(--border);
  color:var(--text2);cursor:pointer;font-size:16px;
  display:inline-flex;align-items:center;justify-content:center;
  transition:all 0.15s;
}
.btn-icon:hover{color:var(--text);border-color:var(--text2)}
.btn:disabled,.btn-ghost:disabled{opacity:0.4;cursor:not-allowed}

/* Cards */
.stat-grid{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:16px;margin:24px 0;
}
.stat-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:8px;padding:20px;
}
.stat-card .label{font-size:12px;color:var(--text2);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px}
.stat-card .value{font-size:28px;font-weight:600;letter-spacing:-0.5px}
.stat-card .value.error{color:var(--error)}

.plugin-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:16px;margin:16px 0 32px;
}
.plugin-card{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:10px;
}
.plugin-card .plugin-header{display:flex;align-items:center;gap:8px}
.plugin-card .plugin-name{font-weight:600;font-size:14px}
.plugin-card .dot{
  width:8px;height:8px;border-radius:50%;background:var(--success);
  display:inline-block;flex-shrink:0;
}
.plugin-card .meta{font-size:12px;color:var(--text2)}
.pill{
  display:inline-block;padding:1px 7px;border-radius:4px;
  font-size:11px;font-weight:500;
}
.pill-read{background:var(--pill-read);color:var(--pill-read-text)}
.pill-write{background:var(--pill-write);color:var(--pill-write-text)}
.pill-admin{background:var(--pill-admin);color:var(--pill-admin-text)}
.pills{display:flex;gap:4px;flex-wrap:wrap}
.account-tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.account-tag{
  background:var(--bg);border:1px solid var(--border);
  padding:1px 6px;border-radius:3px;font-size:11px;
  font-family:'SF Mono','Fira Code','Consolas',monospace;
}

/* Section headings */
.section-title{
  font-size:13px;font-weight:600;text-transform:uppercase;
  letter-spacing:0.5px;color:var(--text2);margin:28px 0 12px;
}
.section-header{display:flex;align-items:center;justify-content:space-between;margin:28px 0 12px}
.section-header .section-title{margin:0}

/* Tables */
.table-wrap{overflow-x:auto;margin:12px 0 16px;border:1px solid var(--border);border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{
  text-align:left;padding:10px 14px;font-size:11px;font-weight:600;
  text-transform:uppercase;letter-spacing:0.5px;color:var(--text2);
  border-bottom:1px solid var(--border);background:var(--bg2);
}
td{padding:10px 14px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg2)}
tr.clickable-row{cursor:pointer}
tr.clickable-row:hover td{background:var(--bg3)}
tr.row-error td{background:var(--error-bg)}
.status-ok{color:var(--success)}
.status-err{color:var(--error)}

/* Pagination */
.pagination{
  display:flex;align-items:center;gap:4px;margin:12px 0 32px;flex-wrap:wrap;
}
.pagination-info{font-size:12px;color:var(--text2);margin-left:12px}

/* Tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin:24px 0 0}
.tab{
  padding:10px 20px;font-size:13px;font-weight:500;cursor:pointer;
  border-bottom:2px solid transparent;color:var(--text2);
  background:none;border-top:none;border-left:none;border-right:none;
  font-family:inherit;transition:all 0.15s;
}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{display:none;padding:20px 0}
.tab-content.active{display:block}

/* Filters */
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.filter-input,.form-input{
  padding:6px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--bg);color:var(--text);font-size:13px;
  font-family:inherit;min-width:140px;
}
.filter-input:focus,.form-input:focus{outline:none;border-color:var(--accent)}
select.filter-input,select.form-input{cursor:pointer}

/* Forms / Modal */
.modal-backdrop{
  display:none;position:fixed;inset:0;background:var(--modal-bg);
  z-index:1000;align-items:center;justify-content:center;padding:20px;
}
.modal-backdrop.open{display:flex}
.modal{
  background:var(--bg);border:1px solid var(--border);border-radius:8px;
  padding:28px;width:100%;max-width:560px;max-height:90vh;overflow-y:auto;
  box-shadow:0 8px 30px rgba(0,0,0,0.2);
}
.modal h2{font-size:18px;font-weight:600;margin-bottom:20px}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:12px;font-weight:500;margin-bottom:6px;color:var(--text2)}
.form-group input[type="text"],.form-group input[type="number"],.form-group select,.form-group textarea{
  width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--bg2);color:var(--text);font-size:13px;font-family:inherit;
}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{outline:none;border-color:var(--accent)}
.form-group textarea{resize:vertical;min-height:60px}
.form-group .help{font-size:11px;color:var(--text2);margin-top:4px}
.form-row{display:flex;gap:12px}
.form-row .form-group{flex:1}
.form-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.checkbox-group{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.checkbox-label{
  display:inline-flex;align-items:center;gap:4px;font-size:12px;
  padding:4px 10px;border:1px solid var(--border);border-radius:4px;
  cursor:pointer;transition:all 0.15s;
}
.checkbox-label:hover{border-color:var(--accent)}
.checkbox-label input{margin:0}
.checkbox-label.checked{background:var(--pill-read);border-color:var(--pill-read-text)}
.toggle-row{display:flex;align-items:center;gap:8px}
.toggle-row input[type="checkbox"]{width:16px;height:16px;cursor:pointer}

/* Flash messages */
.flash{padding:10px 16px;border-radius:6px;font-size:13px;margin:16px 0}
.flash-success{background:rgba(34,197,94,0.1);color:var(--success);border:1px solid rgba(34,197,94,0.2)}
.flash-error{background:var(--error-bg);color:var(--error);border:1px solid rgba(239,68,68,0.2)}
.flash .mono{font-size:12px;word-break:break-all}

/* Login page */
.login-wrap{
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:20px;
}
.login-card{
  width:100%;max-width:380px;background:var(--bg2);
  border:1px solid var(--border);border-radius:8px;padding:40px 32px;
}
.login-card h1{font-size:22px;font-weight:600;margin-bottom:4px;text-align:center}
.login-card .subtitle{
  font-size:13px;color:var(--text2);text-align:center;margin-bottom:28px;
}
.login-card .error-msg{
  background:var(--error-bg);color:var(--error);
  padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:16px;
}
.login-card label{font-size:12px;font-weight:500;display:block;margin-bottom:6px}
.login-card input[type="password"]{
  width:100%;padding:10px 12px;border:1px solid var(--border);
  border-radius:6px;background:var(--bg);color:var(--text);
  font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:14px;
}
.login-card input[type="password"]:focus{outline:none;border-color:var(--accent)}
.login-card .btn{width:100%;margin-top:16px;padding:10px}
.login-logo{text-align:center;font-size:32px;margin-bottom:16px}

/* Detail page */
.detail-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin:20px 0}
.detail-item{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px}
.detail-item .detail-label{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px}
.detail-item .detail-value{font-size:14px;font-weight:500;word-break:break-all}
.json-block{
  background:var(--bg2);border:1px solid var(--border);border-radius:8px;
  padding:16px;overflow-x:auto;margin:8px 0 24px;
  font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:12px;
  line-height:1.6;white-space:pre-wrap;word-break:break-word;
  max-height:500px;overflow-y:auto;
}
.json-block .json-key{color:var(--accent)}
.json-block .json-str{color:var(--success)}
.json-block .json-num{color:#e879f9}
.json-block .json-bool{color:#f97316}
.json-block .json-null{color:var(--text2)}
.back-link{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text2);margin:20px 0 8px}
.back-link:hover{color:var(--text);text-decoration:none}

/* Footer */
.footer{
  text-align:center;padding:24px 0;font-size:12px;color:var(--text2);
  border-top:1px solid var(--border);margin-top:40px;
}
.footer .mono{font-size:11px}

/* Responsive */
@media(max-width:640px){
  .stat-grid{grid-template-columns:1fr 1fr}
  .plugin-grid{grid-template-columns:1fr}
  .header{flex-direction:column;align-items:flex-start}
  .header-right{width:100%;justify-content:flex-end}
  td,th{padding:8px 10px;font-size:12px}
  .modal{padding:20px}
  .form-row{flex-direction:column;gap:0}
}
</style>"""


# ── Theme toggle JS ───────────────────────────────────────────────────────────

_THEME_JS = """<script>
(function(){
  var s=localStorage.getItem('gw_theme');
  if(s)document.documentElement.setAttribute('data-theme',s);
})();
function toggleTheme(){
  var h=document.documentElement;
  var cur=h.getAttribute('data-theme');
  var isDark=cur==='dark'||(cur!=='light'&&window.matchMedia('(prefers-color-scheme:dark)').matches);
  var next=isDark?'light':'dark';
  h.setAttribute('data-theme',next);
  localStorage.setItem('gw_theme',next);
  updateToggleIcon();
}
function updateToggleIcon(){
  var btn=document.getElementById('theme-toggle');
  if(!btn)return;
  var h=document.documentElement;
  var cur=h.getAttribute('data-theme');
  var isDark=cur==='dark'||(cur!=='light'&&window.matchMedia('(prefers-color-scheme:dark)').matches);
  btn.innerHTML=isDark
    ?'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
    :'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
}
document.addEventListener('DOMContentLoaded',updateToggleIcon);
</script>"""


# ── Relative time JS ──────────────────────────────────────────────────────────

_RELATIVE_TIME_JS = """<script>
function relTime(iso){
  if(!iso)return'\\u2014';
  var d=new Date(iso.replace(' ','T')+(iso.indexOf('Z')<0&&iso.indexOf('+')< 0?'Z':''));
  var s=Math.floor((Date.now()-d.getTime())/1000);
  if(s<60)return s+'s ago';
  var m=Math.floor(s/60);if(m<60)return m+'m ago';
  var h=Math.floor(m/60);if(h<24)return h+'h ago';
  var dy=Math.floor(h/24);return dy+'d ago';
}
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('[data-ts]').forEach(function(el){
    el.textContent=relTime(el.getAttribute('data-ts'));
    el.title=el.getAttribute('data-ts');
  });
});
</script>"""


# ── Template fragments ────────────────────────────────────────────────────────

def _header_html(key_info: dict, active: str = "dashboard") -> str:
    label = escape(key_info.get("label") or "\u2014")
    key_id = escape(key_info["id"])
    admin_badge = '<span class="badge-admin">Admin</span>' if key_info.get("is_admin") else ""
    dash_active = ' active' if active == "dashboard" else ""
    admin_link = ""
    if key_info.get("is_admin"):
        admin_active = ' active' if active == "admin" else ""
        admin_link = f'<a href="/dash/admin" class="nav-link{admin_active}">Admin</a>'

    return f"""<div class="container">
  <div class="header">
    <div class="header-left">
      <h1>{label}</h1>
      <span class="key-id mono">{key_id}</span>
      {admin_badge}
    </div>
    <div class="header-right">
      <a href="/dash" class="nav-link{dash_active}">Dashboard</a>
      {admin_link}
      <button id="theme-toggle" class="btn-icon" onclick="toggleTheme()" title="Toggle theme"></button>
      <a href="/dash/logout" class="btn btn-ghost btn-sm">Logout</a>
    </div>
  </div>
</div>"""


def _footer_html() -> str:
    base = escape(_BASE_URL)
    return f"""<div class="container">
  <div class="footer">MCP Gateway &middot; <span class="mono">{base}</span></div>
</div>"""


def _activity_table(entries: list[dict], show_key: bool = False) -> str:
    rows = []
    for entry in entries:
        eid = entry.get("id", "")
        ts = entry.get("timestamp", "")
        tool = escape(entry.get("tool_name", "") or "\u2014")
        plugin = escape(entry.get("plugin", "") or "\u2014")
        ok = entry.get("success", 1)
        dur = entry.get("duration_ms", 0) or 0
        status_icon = '<span class="status-ok">&#10003;</span>' if ok else '<span class="status-err">&#10007;</span>'
        row_cls = ' row-error' if not ok else ""
        key_col = f'<td><span class="mono">{escape(entry.get("key_id", "") or "")}</span></td>' if show_key else ""
        click = f' onclick="window.location=\'/dash/audit/{eid}\'" style="cursor:pointer"' if eid else ""
        rows.append(f"""<tr class="clickable-row{row_cls}"{click}>
  <td><span class="mono" data-ts="{escape(str(ts))}">{escape(str(ts))}</span></td>
  {key_col}
  <td><span class="mono">{tool}</span></td>
  <td>{plugin}</td>
  <td>{status_icon}</td>
  <td class="mono">{dur}ms</td>
</tr>""")

    key_th = "<th>Key</th>" if show_key else ""
    empty_cols = 6 if show_key else 5
    return f"""<div class="table-wrap">
<table>
<thead><tr><th>Time</th>{key_th}<th>Tool</th><th>Plugin</th><th>Status</th><th>Duration</th></tr></thead>
<tbody>{''.join(rows) if rows else f'<tr><td colspan="{empty_cols}" style="text-align:center;color:var(--text2);padding:24px">No activity yet</td></tr>'}</tbody>
</table>
</div>"""


# ── Route handlers ────────────────────────────────────────────────────────────

async def root_redirect(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if key:
        return RedirectResponse("/dash", status_code=302)
    return RedirectResponse("/dash/login", status_code=302)


async def login_page(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if key:
        return RedirectResponse("/dash", status_code=302)
    return _render_login()


async def login_post(request: Request) -> Response:
    form = await request.form()
    token = form.get("api_key", "").strip()
    if not token:
        return _render_login(error="Please enter an API key.")

    key_info = auth.resolve_key(token)
    if not key_info:
        return _render_login(error="Invalid API key.")

    if auth.is_key_expired(key_info):
        return _render_login(error="This API key has expired.")

    response = RedirectResponse("/dash", status_code=302)
    response.set_cookie(
        "gw_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


async def dashboard_page(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key:
        return RedirectResponse("/dash/login", status_code=302)
    return _render_dashboard(key, request)


async def admin_page(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key:
        return RedirectResponse("/dash/login", status_code=302)
    if not key.get("is_admin"):
        return RedirectResponse("/dash", status_code=302)
    return _render_admin(key, request)


async def admin_create_key(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)

    form = await request.form()
    key_id = form.get("key_id", "").strip()
    label = form.get("label", "").strip()
    is_admin = form.get("is_admin") == "on"
    rate_limit = int(form.get("rate_limit", "100") or "100")
    expires_at = form.get("expires_at", "").strip() or None
    allowed_ips = form.get("allowed_ips", "").strip() or None

    if not key_id:
        return RedirectResponse("/dash/admin?flash=error&msg=Key+ID+is+required", status_code=302)

    result = auth.create_key(key_id, label=label, is_admin=is_admin, rate_limit=rate_limit,
                             expires_at=expires_at, allowed_ips=allowed_ips)
    if "error" in result:
        msg = result["error"].replace(" ", "+")
        return RedirectResponse(f"/dash/admin?flash=error&msg={msg}", status_code=302)

    # Set plugin permissions
    for plugin in _all_plugins:
        level = form.get(f"perm_{plugin}", "")
        if level:
            auth.set_permissions(key_id, plugin, [level, *(["read"] if level in ("write", "admin") else []), *(["write"] if level == "admin" else [])])

    api_key = result.get("api_key", "")
    return RedirectResponse(f"/dash/admin?flash=success&msg=Key+created&new_key={api_key}", status_code=302)


async def admin_edit_key(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)

    form = await request.form()
    target_key_id = form.get("target_key_id", "").strip()
    if not target_key_id:
        return RedirectResponse("/dash/admin?flash=error&msg=Missing+key+ID", status_code=302)

    label = form.get("label", "").strip()
    rate_limit = int(form.get("rate_limit", "100") or "100")
    expires_at = form.get("expires_at", "").strip() or None
    allowed_ips = form.get("allowed_ips", "").strip() or None
    can_audit = 1 if form.get("can_audit") else 0

    conn = _db()
    conn.execute(
        "UPDATE keys SET label=?, rate_limit=?, expires_at=?, allowed_ips=?, can_audit=? WHERE id=?",
        (label, rate_limit, expires_at, allowed_ips, can_audit, target_key_id),
    )
    conn.commit()
    conn.close()

    # Update permissions
    for plugin in _all_plugins:
        level = form.get(f"perm_{plugin}", "")
        if level == "none":
            conn2 = _db()
            conn2.execute("DELETE FROM key_permissions WHERE key_id=? AND plugin=?", (target_key_id, plugin))
            conn2.commit()
            conn2.close()
        elif level:
            levels = [level]
            if level in ("write", "admin"):
                levels.append("read")
            if level == "admin":
                levels.append("write")
            auth.set_permissions(target_key_id, plugin, levels)

    # Update granular rate limits
    existing_limits = auth.get_rate_limits(target_key_id)
    new_limits: dict[str, int] = {}
    for field_name in form.keys():
        if field_name.startswith("rl_scope_"):
            raw = form.get(field_name, "")
            if "|" in raw:
                scope, val_str = raw.rsplit("|", 1)
                try:
                    val = int(val_str)
                    new_limits[scope] = val
                except ValueError:
                    pass

    for scope in existing_limits:
        if scope not in new_limits:
            auth.delete_rate_limit(target_key_id, scope)
    for scope, val in new_limits.items():
        if val > 0:
            auth.set_rate_limit(target_key_id, scope, val)
        else:
            auth.delete_rate_limit(target_key_id, scope)

    return RedirectResponse(f"/dash/admin?flash=success&msg=Key+updated", status_code=302)


async def admin_delete_key(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)

    form = await request.form()
    target_key_id = form.get("target_key_id", "").strip()
    if not target_key_id:
        return RedirectResponse("/dash/admin?flash=error&msg=Missing+key+ID", status_code=302)

    result = auth.delete_key(target_key_id)
    if "error" in result:
        msg = result["error"].replace(" ", "+")
        return RedirectResponse(f"/dash/admin?flash=error&msg={msg}", status_code=302)

    return RedirectResponse(f"/dash/admin?flash=success&msg=Key+deleted", status_code=302)


async def audit_detail_page(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key:
        return RedirectResponse("/dash/login", status_code=302)

    entry_id = request.path_params.get("entry_id", "")
    try:
        entry_id_int = int(entry_id)
    except (ValueError, TypeError):
        return RedirectResponse("/dash", status_code=302)

    import audit as audit_mod
    entry = audit_mod.get_audit_entry(entry_id_int)
    if not entry:
        return RedirectResponse("/dash", status_code=302)

    is_admin = bool(key.get("is_admin"))
    if not is_admin and entry.get("key_id") != key["id"]:
        return RedirectResponse("/dash", status_code=302)

    return _render_audit_detail(key, entry)


async def logout_page(request: Request) -> Response:
    response = RedirectResponse("/dash/login", status_code=302)
    response.delete_cookie("gw_token", path="/")
    return response


# ── Login renderer ────────────────────────────────────────────────────────────

def _render_login(error: str = "") -> HTMLResponse:
    error_html = f'<div class="error-msg">{escape(error)}</div>' if error else ""
    body = f"""<div class="login-wrap">
  <div class="login-card">
    <div class="login-logo">\u26a1</div>
    <h1>MCP Gateway</h1>
    <p class="subtitle">Enter your API key to continue</p>
    {error_html}
    <form method="POST" action="/dash/login" autocomplete="off">
      <label for="api_key">API Key</label>
      <input type="password" id="api_key" name="api_key" placeholder="gw_..." autofocus>
      <button type="submit" class="btn btn-primary">Connect</button>
    </form>
  </div>
</div>"""
    return _html(body, title="Login \u2014 MCP Gateway")


# ── Dashboard renderer ────────────────────────────────────────────────────────

def _render_dashboard(key_info: dict, request: Request) -> HTMLResponse:
    is_admin = bool(key_info.get("is_admin"))
    can_audit = bool(key_info.get("can_audit", 1))
    key_id = key_info["id"]
    stats = _get_stats(key_id, is_admin) if (is_admin or can_audit) else {"total": 0, "today": 0, "week": 0, "error_rate": 0}
    page, per_page, offset = _paginate(request, default_per_page=50)
    activity, total_activity = (
        _get_recent_activity(key_id, is_admin, limit=per_page, offset=offset)
        if (is_admin or can_audit)
        else ([], 0)
    )
    perms = auth.get_key_permissions(key_id)

    stats_html = f"""<div class="stat-grid">
  <div class="stat-card"><div class="label">Total Calls</div><div class="value">{stats['total']:,}</div></div>
  <div class="stat-card"><div class="label">Today</div><div class="value">{stats['today']:,}</div></div>
  <div class="stat-card"><div class="label">This Week</div><div class="value">{stats['week']:,}</div></div>
  <div class="stat-card"><div class="label">Error Rate</div><div class="value{' error' if stats['error_rate'] > 5 else ''}">{stats['error_rate']}%</div></div>
</div>"""

    # Plugins
    plugin_cards = []
    creds_all = auth.get_key_credentials(key_id)
    for name, info in sorted(_plugin_registry.items()):
        tool_count = info.get("tool_count", 0)
        key_perms = perms.get(name, set())

        pills = ""
        for level in ("read", "write", "admin"):
            if is_admin or level in key_perms:
                pills += f'<span class="pill pill-{level}">{level}</span>'

        account_tags = ""
        if name in creds_all:
            accounts = set()
            for k in creds_all[name]:
                if "." in k:
                    accounts.add(k.split(".")[0])
            if not accounts and "api_key" in creds_all[name]:
                accounts.add("default")
            for a in sorted(accounts)[:5]:
                account_tags += f'<span class="account-tag">{escape(a)}</span>'

        plugin_cards.append(f"""<div class="plugin-card">
  <div class="plugin-header"><span class="dot"></span><span class="plugin-name">{escape(name)}</span></div>
  <div class="pills">{pills}</div>
  <div class="meta">{tool_count} tool{'s' if tool_count != 1 else ''}{f' &middot; {len(set(a.split(".")[0] for a in creds_all.get(name, {}) if "." in a) or {"default"} if creds_all.get(name) else set())} account{"s" if len(set(a.split(".")[0] for a in creds_all.get(name, {}) if "." in a) or {"default"} if creds_all.get(name) else set()) != 1 else ""}' if creds_all.get(name) else ''}</div>
  {f'<div class="account-tags">{account_tags}</div>' if account_tags else ''}
</div>""")

    plugins_html = f"""<div class="section-title">Plugins</div>
<div class="plugin-grid">{''.join(plugin_cards)}</div>""" if plugin_cards else ""

    # My Accounts section (non-admin users can manage their own credentials)
    accounts_html = ""
    if not is_admin:
        flash_qs = request.query_params.get("flash", "")
        flash_msg = request.query_params.get("msg", "")
        flash_html = ""
        if flash_qs and flash_msg:
            flash_cls = "flash-success" if flash_qs == "success" else "flash-error"
            flash_html = f'<div class="{flash_cls}">{escape(flash_msg)}</div>'

        accessible_plugins = sorted(perms.keys())
        account_rows = []
        for pname in accessible_plugins:
            pcreds = creds_all.get(pname, {})
            cred_keys = sorted(pcreds.keys())
            accounts_list: list[str] = []
            seen_accts: set[str] = set()
            bare_keys: list[str] = []
            for k in cred_keys:
                if "." in k:
                    acct = k.split(".")[0]
                    if acct not in seen_accts:
                        accounts_list.append(acct)
                        seen_accts.add(acct)
                else:
                    bare_keys.append(k)

            status = '<span class="status-ok">configured</span>' if cred_keys else '<span style="color:var(--text2)">not configured</span>'
            acct_tags = ""
            if accounts_list:
                acct_tags = " ".join(f'<span class="account-tag">{escape(a)}</span>' for a in accounts_list[:8])
            elif bare_keys:
                acct_tags = '<span class="account-tag">default</span>'

            remove_btns = ""
            for acct_name in accounts_list:
                remove_btns += f'<form method="POST" action="/dash/credentials/remove" style="display:inline" onsubmit="return confirm(\\x27Remove {escape(acct_name)}?\\x27)"><input type="hidden" name="plugin" value="{escape(pname)}"><input type="hidden" name="account" value="{escape(acct_name)}"><button type="submit" class="btn btn-ghost btn-sm" style="color:var(--error);font-size:11px">x {escape(acct_name)}</button></form>'

            account_rows.append(f"""<tr>
  <td class="mono">{escape(pname)}</td>
  <td>{status}</td>
  <td>{acct_tags}</td>
  <td style="white-space:nowrap">
    <button class="btn btn-ghost btn-sm" onclick="openCredsModal('{escape(pname)}')">+ Add Account</button>
    {remove_btns}
  </td>
</tr>""")

        creds_modal = f"""<div class="modal-backdrop" id="creds-modal">
  <div class="modal">
    <h2>Configure: <span id="creds-plugin-title" class="mono"></span></h2>
    <form method="POST" action="/dash/credentials/set">
      <input type="hidden" name="plugin" id="creds-plugin-name">
      <div class="form-group">
        <label>Account Name <span style="font-weight:400;color:var(--text2)">(required)</span></label>
        <input type="text" name="account" class="form-input" placeholder="e.g. work, personal" required>
      </div>
      <div id="creds-fields">
        <div class="form-row creds-field-row">
          <div class="form-group" style="flex:1"><label>Key</label><input type="text" name="cred_key_0" class="form-input" placeholder="api_key"></div>
          <div class="form-group" style="flex:2"><label>Value</label><input type="text" name="cred_val_0" class="form-input" placeholder="sk-..."></div>
        </div>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" onclick="addCredField()" style="margin-bottom:12px">+ Add field</button>
      <div class="help">Common keys: api_key, client_id, client_secret, refresh_token, base_url, bot_token</div>
      <div class="form-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal('creds-modal')">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  </div>
</div>"""

        creds_js = """<script>
var _credFieldCount=1;
function openCredsModal(plugin){
  document.getElementById('creds-plugin-title').textContent=plugin;
  document.getElementById('creds-plugin-name').value=plugin;
  _credFieldCount=1;
  document.getElementById('creds-fields').innerHTML='<div class="form-row creds-field-row"><div class="form-group" style="flex:1"><label>Key</label><input type="text" name="cred_key_0" class="form-input" placeholder="api_key"></div><div class="form-group" style="flex:2"><label>Value</label><input type="text" name="cred_val_0" class="form-input" placeholder="sk-..."></div></div>';
  document.getElementById('creds-modal').classList.add('open');
}
function addCredField(){
  var i=_credFieldCount++;
  var html='<div class="form-row creds-field-row"><div class="form-group" style="flex:1"><input type="text" name="cred_key_'+i+'" class="form-input" placeholder="key"></div><div class="form-group" style="flex:2"><input type="text" name="cred_val_'+i+'" class="form-input" placeholder="value"></div></div>';
  document.getElementById('creds-fields').insertAdjacentHTML('beforeend',html);
}
</script>"""

        accounts_html = f"""{flash_html}
<div class="section-title">My Accounts</div>
<div class="table-wrap">
<table>
<thead><tr><th>Plugin</th><th>Status</th><th>Accounts</th><th></th></tr></thead>
<tbody>{''.join(account_rows)}</tbody>
</table>
</div>
{creds_modal}
{creds_js}"""

    if is_admin or can_audit:
        activity_html = f"""<div class="section-title">Recent Activity</div>
{_activity_table(activity, show_key=is_admin)}
{_pagination_html(page, per_page, total_activity, "/dash")}"""
    else:
        activity_html = ""

    body = f"""{_header_html(key_info, 'dashboard')}
<div class="container">
{stats_html}
{plugins_html}
{accounts_html}
{activity_html}
</div>
{_footer_html()}
{_RELATIVE_TIME_JS}"""
    return _html(body, title="Dashboard \u2014 MCP Gateway")


# ── Admin renderer ────────────────────────────────────────────────────────────

def _render_admin(key_info: dict, request: Request) -> HTMLResponse:
    all_keys = auth.list_keys()
    tab = request.query_params.get("tab", "keys")

    # Flash messages
    flash = request.query_params.get("flash", "")
    flash_msg = request.query_params.get("msg", "")
    new_key = request.query_params.get("new_key", "")
    flash_html = ""
    if flash == "success":
        extra = f'<br><span class="mono">{escape(new_key)}</span><br><small>Copy this now \u2014 it won\'t be shown again.</small>' if new_key else ""
        flash_html = f'<div class="flash flash-success">{escape(flash_msg)}{extra}</div>'
    elif flash == "error":
        flash_html = f'<div class="flash flash-error">{escape(flash_msg)}</div>'

    # Keys table
    key_rows = []
    for k in all_keys:
        kid = escape(k.get("id", ""))
        klabel = escape(k.get("label", "") or "\u2014")
        kadmin = "Yes" if k.get("is_admin") else "No"
        kperms_dict = k.get("permissions", {})
        kplugins = ", ".join(sorted(kperms_dict.keys())) if kperms_dict else "\u2014"
        krate = k.get("rate_limit", 100)
        kcreated = k.get("created_at", "")
        kexpiry = k.get("expires_at") or "\u2014"

        # Build permission levels summary
        perm_levels = {}
        for pname, plevels in kperms_dict.items():
            max_level = "admin" if "admin" in plevels else ("write" if "write" in plevels else "read")
            perm_levels[pname] = max_level

        granular_limits = auth.get_rate_limits(k.get("id", ""))

        # JSON-safe data for the edit modal
        edit_data = json.dumps({
            "id": k.get("id", ""),
            "label": k.get("label", ""),
            "rate_limit": krate,
            "expires_at": k.get("expires_at") or "",
            "allowed_ips": k.get("allowed_ips") or "",
            "can_audit": bool(k.get("can_audit", 1)),
            "permissions": perm_levels,
            "granular_limits": granular_limits,
        })

        edit_btn = f"""<button class="btn btn-ghost btn-sm" onclick='openEditModal({escape(edit_data)})'>Edit</button>"""
        del_btn = ""
        if k.get("id") != auth.ADMIN_KEY_ID:
            del_btn = f"""<form method="POST" action="/dash/admin/keys/delete" style="display:inline" onsubmit="return confirm('Delete key {kid}?')">
<input type="hidden" name="target_key_id" value="{kid}">
<button type="submit" class="btn btn-ghost btn-sm" style="color:var(--error)">Delete</button>
</form>"""

        key_rows.append(f"""<tr>
  <td>{klabel}</td>
  <td><span class="mono">{kid}</span></td>
  <td>{kadmin}</td>
  <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{escape(kplugins)}">{escape(kplugins)}</td>
  <td class="mono">{krate}/min</td>
  <td><span class="mono" data-ts="{escape(str(kcreated))}">{escape(str(kcreated))}</span></td>
  <td class="mono">{escape(str(kexpiry))}</td>
  <td style="white-space:nowrap">{edit_btn} {del_btn}</td>
</tr>""")

    keys_html = f"""{flash_html}
<div class="section-header">
  <div class="section-title">API Keys</div>
  <button class="btn btn-primary btn-sm" onclick="openCreateModal()">+ Create Key</button>
</div>
<div class="table-wrap">
<table>
<thead><tr><th>Label</th><th>ID</th><th>Admin</th><th>Plugins</th><th>Rate Limit</th><th>Created</th><th>Expiry</th><th></th></tr></thead>
<tbody>{''.join(key_rows)}</tbody>
</table>
</div>"""

    # Plugin permission selects (used in modals)
    plugin_options_tmpl = "\n".join(
        f'<div class="form-group" style="margin-bottom:8px"><label style="display:flex;align-items:center;justify-content:space-between">'
        f'<span>{escape(p)}</span>'
        f'<select class="form-input" name="perm_{escape(p)}" id="{{prefix}}_perm_{escape(p)}" style="width:120px;min-width:120px">'
        f'<option value="none">None</option><option value="read">Read</option><option value="write">Write</option><option value="admin">Admin</option>'
        f'</select></label></div>'
        for p in _all_plugins
    )

    create_plugins = plugin_options_tmpl.replace("{prefix}", "create")
    edit_plugins = plugin_options_tmpl.replace("{prefix}", "edit")

    # Create key modal
    create_modal = f"""<div class="modal-backdrop" id="create-modal">
  <div class="modal">
    <h2>Create API Key</h2>
    <form method="POST" action="/dash/admin/keys/create">
      <div class="form-row">
        <div class="form-group"><label>Key ID</label><input type="text" name="key_id" required placeholder="my-agent"></div>
        <div class="form-group"><label>Label</label><input type="text" name="label" placeholder="My Agent Key"></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>Rate Limit (req/min)</label><input type="number" name="rate_limit" value="100" min="0"></div>
        <div class="form-group"><label>Expiry (ISO date)</label><input type="text" name="expires_at" placeholder="2026-12-31T23:59:59"></div>
      </div>
      <div class="form-group"><label>Allowed IPs (comma-separated CIDRs, blank = all)</label><input type="text" name="allowed_ips" placeholder="1.2.3.4,10.0.0.0/8"></div>
      <div class="form-group">
        <div class="toggle-row"><input type="checkbox" name="is_admin" id="create-is-admin"><label for="create-is-admin">Admin key</label></div>
      </div>
      <div class="form-group"><label>Plugin Permissions</label>{create_plugins}</div>
      <div class="form-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal('create-modal')">Cancel</button>
        <button type="submit" class="btn btn-primary">Create</button>
      </div>
    </form>
  </div>
</div>"""

    # Edit key modal
    edit_modal = f"""<div class="modal-backdrop" id="edit-modal">
  <div class="modal">
    <h2>Edit Key: <span id="edit-key-title" class="mono"></span></h2>
    <form method="POST" action="/dash/admin/keys/edit">
      <input type="hidden" name="target_key_id" id="edit-key-id">
      <div class="form-group"><label>Label</label><input type="text" name="label" id="edit-label"></div>
      <div class="form-row">
        <div class="form-group"><label>Global Rate Limit (req/min)</label><input type="number" name="rate_limit" id="edit-rate-limit" min="0"></div>
        <div class="form-group"><label>Expiry (ISO date)</label><input type="text" name="expires_at" id="edit-expires"></div>
      </div>
      <div class="form-group"><label>Allowed IPs</label><input type="text" name="allowed_ips" id="edit-ips"></div>
      <div class="form-group">
        <div class="toggle-row"><input type="checkbox" name="can_audit" id="edit-can-audit" value="1" checked><label for="edit-can-audit">Can audit own activity</label></div>
      </div>
      <div class="form-group"><label>Plugin Permissions</label>{edit_plugins}</div>
      <div class="form-group" style="border-top:1px solid var(--border);padding-top:16px;margin-top:8px">
        <label>Granular Rate Limits <span style="font-weight:400;color:var(--text2)">(per plugin / account / tool)</span></label>
        <div id="edit-rate-limits-list" style="margin:8px 0"></div>
        <div style="display:flex;gap:8px;align-items:flex-end;margin-top:8px">
          <div style="flex:1">
            <select id="add-rl-type" class="form-input" style="width:100%" onchange="updateRlPlaceholder()">
              <option value="plugin">Plugin</option>
              <option value="account">Account</option>
              <option value="tool">Tool</option>
            </select>
          </div>
          <div style="flex:2"><input type="text" id="add-rl-scope" class="form-input" style="width:100%" placeholder="myplugin"></div>
          <div style="flex:1"><input type="number" id="add-rl-value" class="form-input" style="width:100%" placeholder="req/min" min="1"></div>
          <button type="button" class="btn btn-ghost btn-sm" onclick="addRateLimit()" style="white-space:nowrap">+ Add</button>
        </div>
        <div class="help" style="margin-top:4px">Plugin: "myplugin". Account: "myplugin:prod". Tool: "myplugin_do_thing".</div>
      </div>
      <div class="form-actions">
        <button type="button" class="btn btn-ghost" onclick="closeModal('edit-modal')">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  </div>
</div>"""

    # Audit log with server-side filtering + pagination
    audit_page, audit_per_page, audit_offset = 1, 50, 0
    key_filter = ""
    plugin_filter = ""
    tool_filter = ""
    if tab == "audit":
        try:
            audit_page = max(1, int(request.query_params.get("audit_page", "1")))
        except ValueError:
            audit_page = 1
        audit_offset = (audit_page - 1) * audit_per_page
        key_filter = request.query_params.get("flt_key", "")
        plugin_filter = request.query_params.get("flt_plugin", "")
        tool_filter = request.query_params.get("flt_tool", "")

    audit_entries, audit_total = _get_all_audit(
        limit=audit_per_page, offset=audit_offset,
        key_filter=key_filter, plugin_filter=plugin_filter, tool_filter=tool_filter,
    )
    all_audit_keys, all_audit_plugins = _get_distinct_audit_values()

    key_options = '<option value="">All keys</option>' + "".join(
        f'<option value="{escape(k)}"{" selected" if k == key_filter else ""}>{escape(k)}</option>' for k in all_audit_keys
    )
    plugin_options = '<option value="">All plugins</option>' + "".join(
        f'<option value="{escape(p)}"{" selected" if p == plugin_filter else ""}>{escape(p)}</option>' for p in all_audit_plugins
    )

    audit_extra_params = {"tab": "audit"}
    if key_filter:
        audit_extra_params["flt_key"] = key_filter
    if plugin_filter:
        audit_extra_params["flt_plugin"] = plugin_filter
    if tool_filter:
        audit_extra_params["flt_tool"] = tool_filter

    audit_html = f"""<form method="GET" action="/dash/admin" class="filters">
  <input type="hidden" name="tab" value="audit">
  <select class="filter-input" name="flt_key">{key_options}</select>
  <select class="filter-input" name="flt_plugin">{plugin_options}</select>
  <input class="filter-input" name="flt_tool" type="text" placeholder="Search tool name\u2026" value="{escape(tool_filter)}">
  <button type="submit" class="btn btn-ghost btn-sm">Filter</button>
</form>
{_activity_table(audit_entries, show_key=True)}
{_pagination_html(audit_page, audit_per_page, audit_total, "/dash/admin", audit_extra_params)}"""

    # External MCPs section
    ext_configs = external_mcp.list_external_mcps()
    ext_rows = []
    for ec in ext_configs:
        ename = escape(ec.get("name", ""))
        eurl = escape(ec.get("url", ""))
        eenabled = "Yes" if ec.get("enabled") else "No"
        elastr = ec.get("last_refreshed") or "\u2014"
        eerr = ec.get("last_error") or ""
        eerr_badge = f' <span class="status-err" title="{escape(eerr)}">\u26a0</span>' if eerr else ""
        etool_count = len([k for k in _tool_registry_ref if k.startswith(f"{ec.get('name', '')}_")])
        ext_rows.append(f"""<tr>
  <td class="mono">{ename}</td>
  <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis" title="{eurl}">{eurl}</td>
  <td>{eenabled}{eerr_badge}</td>
  <td class="mono">{etool_count}</td>
  <td><span class="mono" data-ts="{escape(str(elastr))}">{escape(str(elastr))}</span></td>
  <td style="white-space:nowrap">
    <form method="POST" action="/dash/admin/external/refresh" style="display:inline"><input type="hidden" name="name" value="{ename}"><button type="submit" class="btn btn-ghost btn-sm">Refresh</button></form>
    <form method="POST" action="/dash/admin/external/remove" style="display:inline" onsubmit="return confirm('Remove {ename}?')"><input type="hidden" name="name" value="{ename}"><button type="submit" class="btn btn-ghost btn-sm" style="color:var(--error)">Remove</button></form>
  </td>
</tr>""")

    ext_html = f"""
<div class="section-header">
  <div class="section-title">External MCP Bridges</div>
</div>
<div class="table-wrap">
<table>
<thead><tr><th>Name</th><th>URL</th><th>Active</th><th>Tools</th><th>Last Refreshed</th><th></th></tr></thead>
<tbody>{''.join(ext_rows) if ext_rows else '<tr><td colspan="6" style="text-align:center;color:var(--text2)">No external MCPs configured</td></tr>'}</tbody>
</table>
</div>
<div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
  <div class="section-title" style="margin-bottom:8px">Add External MCP</div>
  <form method="POST" action="/dash/admin/external/add">
    <div class="form-row">
      <div class="form-group"><label>Name (plugin namespace)</label><input type="text" name="name" class="form-input" required placeholder="myservice"></div>
      <div class="form-group" style="flex:2"><label>MCP URL</label><input type="text" name="url" class="form-input" required placeholder="https://example.com/mcp"></div>
    </div>
    <div class="form-row">
      <div class="form-group" style="flex:2"><label>Auth Header</label><input type="text" name="auth_header" class="form-input" placeholder="Bearer token123 or x-api-key: secret"></div>
      <div class="form-group"><button type="submit" class="btn btn-primary" style="margin-top:22px">Connect</button></div>
    </div>
  </form>
</div>"""

    keys_active = " active" if tab == "keys" else ""
    ext_active = " active" if tab == "external" else ""
    audit_active = " active" if tab == "audit" else ""

    modal_js = """<script>
var _currentRateLimits={};
function openCreateModal(){document.getElementById('create-modal').classList.add('open')}
function openEditModal(data){
  document.getElementById('edit-key-id').value=data.id;
  document.getElementById('edit-key-title').textContent=data.id;
  document.getElementById('edit-label').value=data.label||'';
  document.getElementById('edit-rate-limit').value=data.rate_limit||100;
  document.getElementById('edit-expires').value=data.expires_at||'';
  document.getElementById('edit-ips').value=data.allowed_ips||'';
  document.getElementById('edit-can-audit').checked=data.can_audit!==false;
  var allPlugins=""" + json.dumps(_all_plugins) + """;
  allPlugins.forEach(function(p){
    var sel=document.getElementById('edit_perm_'+p);
    if(sel)sel.value=data.permissions[p]||'none';
  });
  _currentRateLimits=Object.assign({},data.granular_limits||{});
  renderRateLimits();
  document.getElementById('edit-modal').classList.add('open');
}
function renderRateLimits(){
  var container=document.getElementById('edit-rate-limits-list');
  if(!container)return;
  var activeScopes=Object.keys(_currentRateLimits).filter(function(s){return _currentRateLimits[s]>0}).sort();
  if(!activeScopes.length){container.innerHTML='<div style="color:var(--text2);font-size:12px">No granular limits set</div>';return}
  var html='';
  activeScopes.forEach(function(scope){
    var val=_currentRateLimits[scope];
    var parts=scope.split(':');
    var typeLabel=parts[0];
    var scopeVal=parts.slice(1).join(':');
    html+='<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
      +'<span class="pill pill-'+({'plugin':'read','account':'write','tool':'admin'}[typeLabel]||'read')+'">'+typeLabel+'</span>'
      +'<span class="mono" style="flex:1;font-size:12px">'+scopeVal+'</span>'
      +'<span class="mono" style="font-size:12px">'+val+'/min</span>'
      +'<button type="button" class="btn btn-ghost btn-sm" style="color:var(--error);padding:2px 6px" onclick="removeRateLimit(\''+scope+'\')">×</button>'
      +'<input type="hidden" name="rl_scope_'+scope.replace(/[^a-zA-Z0-9_:]/g,'_')+'" value="'+scope+'|'+val+'">'
      +'</div>';
  });
  container.innerHTML=html;
}
function addRateLimit(){
  var type=document.getElementById('add-rl-type').value;
  var scope=document.getElementById('add-rl-scope').value.trim();
  var val=parseInt(document.getElementById('add-rl-value').value);
  if(!scope||!val||val<1)return;
  var fullScope=type+':'+scope;
  _currentRateLimits[fullScope]=val;
  renderRateLimits();
  document.getElementById('add-rl-scope').value='';
  document.getElementById('add-rl-value').value='';
}
function removeRateLimit(scope){
  _currentRateLimits[scope]=-1;
  renderRateLimits();
}
function updateRlPlaceholder(){
  var type=document.getElementById('add-rl-type').value;
  var ph={'plugin':'myplugin','account':'myplugin:prod','tool':'myplugin_do_thing'};
  document.getElementById('add-rl-scope').placeholder=ph[type]||'';
}
function closeModal(id){document.getElementById(id).classList.remove('open')}
document.querySelectorAll('.modal-backdrop').forEach(function(m){
  m.addEventListener('click',function(e){if(e.target===m)m.classList.remove('open')});
});
</script>"""

    tab_js = """<script>
function switchTab(name){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.getAttribute('data-tab')===name)});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.toggle('active',c.id==='tab-'+name)});
  var url=new URL(window.location);url.searchParams.set('tab',name);
  window.history.replaceState({},'',url);
}
</script>"""

    body = f"""{_header_html(key_info, 'admin')}
<div class="container">
  <div class="tabs">
    <button class="tab{keys_active}" data-tab="keys" onclick="switchTab('keys')">Keys</button>
    <button class="tab{ext_active}" data-tab="external" onclick="switchTab('external')">External MCPs</button>
    <button class="tab{audit_active}" data-tab="audit" onclick="switchTab('audit')">Audit Log</button>
  </div>
  <div class="tab-content{keys_active}" id="tab-keys">{keys_html}</div>
  <div class="tab-content{ext_active}" id="tab-external">{ext_html}</div>
  <div class="tab-content{audit_active}" id="tab-audit">{audit_html}</div>
</div>
{create_modal}
{edit_modal}
{_footer_html()}
{_RELATIVE_TIME_JS}
{modal_js}
{tab_js}"""
    return _html(body, title="Admin \u2014 MCP Gateway")


# ── Audit detail renderer ─────────────────────────────────────────────────────

_JSON_HIGHLIGHT_JS = """<script>
function highlightJson(el){
  var raw=el.textContent;
  try{var obj=JSON.parse(raw);raw=JSON.stringify(obj,null,2)}catch(e){}
  var html=raw
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"([^"]+)"\\s*:/g,'<span class="json-key">"$1"</span>:')
    .replace(/:\\s*"([^"]*?)"/g,': <span class="json-str">"$1"</span>')
    .replace(/:\\s*(-?\\d+\\.?\\d*)/g,': <span class="json-num">$1</span>')
    .replace(/:\\s*(true|false)/g,': <span class="json-bool">$1</span>')
    .replace(/:\\s*(null)/g,': <span class="json-null">$1</span>');
  el.innerHTML=html;
}
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('.json-block').forEach(highlightJson);
});
</script>"""


def _render_audit_detail(key_info: dict, entry: dict) -> HTMLResponse:
    ts = entry.get("timestamp", "")
    tool = escape(entry.get("tool_name", "") or "\u2014")
    plugin = escape(entry.get("plugin", "") or "\u2014")
    ek = escape(entry.get("key_id", "") or "\u2014")
    ok = entry.get("success", 1)
    dur = entry.get("duration_ms", 0) or 0
    error = entry.get("error") or ""
    status_text = '<span class="status-ok">Success</span>' if ok else f'<span class="status-err">Failed</span>'

    args_raw = entry.get("args_json") or ""
    result_raw = entry.get("result_json") or ""

    try:
        args_pretty = json.dumps(json.loads(args_raw), indent=2) if args_raw else "No arguments"
    except (json.JSONDecodeError, TypeError):
        args_pretty = args_raw or "No arguments"

    try:
        result_pretty = json.dumps(json.loads(result_raw), indent=2) if result_raw else "No response recorded"
    except (json.JSONDecodeError, TypeError):
        result_pretty = result_raw or "No response recorded"

    back_href = "/dash/admin?tab=audit" if key_info.get("is_admin") else "/dash"

    error_section = ""
    if error:
        error_section = f"""<div class="section-title">Error</div>
<div class="json-block" style="border-color:var(--error);color:var(--error)">{escape(error)}</div>"""

    body = f"""{_header_html(key_info, 'admin' if key_info.get('is_admin') else 'dashboard')}
<div class="container">
  <a href="{back_href}" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
    Back to {('Audit Log' if key_info.get('is_admin') else 'Activity')}
  </a>

  <div class="detail-grid">
    <div class="detail-item">
      <div class="detail-label">Tool</div>
      <div class="detail-value mono">{tool}</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Plugin</div>
      <div class="detail-value">{plugin}</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Key</div>
      <div class="detail-value mono">{ek}</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Status</div>
      <div class="detail-value">{status_text}</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Duration</div>
      <div class="detail-value mono">{dur}ms</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Time</div>
      <div class="detail-value mono" data-ts="{escape(str(ts))}">{escape(str(ts))}</div>
    </div>
  </div>

  {error_section}

  <div class="section-title">Request (Arguments)</div>
  <div class="json-block">{escape(args_pretty)}</div>

  <div class="section-title">Response</div>
  <div class="json-block">{escape(result_pretty)}</div>
</div>
{_footer_html()}
{_RELATIVE_TIME_JS}
{_JSON_HIGHLIGHT_JS}"""
    return _html(body, title=f"{tool} \u2014 Audit Detail")


# ── Route list ────────────────────────────────────────────────────────────────

async def user_set_credentials(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key:
        return RedirectResponse("/dash/login", status_code=302)
    key_id = key["id"]
    form = await request.form()
    plugin = form.get("plugin", "").strip()
    account = form.get("account", "").strip()

    if not plugin:
        return RedirectResponse("/dash?flash=error&msg=Missing+plugin", status_code=302)
    if not account:
        return RedirectResponse("/dash?flash=error&msg=Account+name+is+required", status_code=302)

    if not key.get("is_admin"):
        perms = auth.get_key_permissions(key_id)
        if plugin not in perms:
            return RedirectResponse(f"/dash?flash=error&msg=No+access+to+{plugin}", status_code=302)

    creds: dict[str, str] = {}
    for i in range(20):
        k = form.get(f"cred_key_{i}", "").strip()
        v = form.get(f"cred_val_{i}", "").strip()
        if k and v:
            creds[f"{account}.{k}"] = v

    if not creds:
        return RedirectResponse("/dash?flash=error&msg=No+credentials+provided", status_code=302)

    auth.upsert_credentials(key_id, plugin, creds)
    return RedirectResponse(f"/dash?flash=success&msg=Added+{account}+account+for+{plugin}", status_code=302)


async def user_remove_credentials(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key:
        return RedirectResponse("/dash/login", status_code=302)
    key_id = key["id"]
    form = await request.form()
    plugin = form.get("plugin", "").strip()
    account = form.get("account", "").strip()
    if not plugin or not account:
        return RedirectResponse("/dash?flash=error&msg=Missing+plugin+or+account", status_code=302)

    conn = _db()
    conn.execute(
        "DELETE FROM key_credentials WHERE key_id = ? AND plugin = ? AND credential_key LIKE ?",
        (key_id, plugin, f"{account}.%"),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/dash?flash=success&msg=Removed+{account}+from+{plugin}", status_code=302)


async def admin_add_external(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)
    form = await request.form()
    name = form.get("name", "").strip()
    url = form.get("url", "").strip()
    auth_header = form.get("auth_header", "").strip()
    if not name or not url:
        return RedirectResponse("/dash/admin?tab=external&flash=error&msg=Name+and+URL+required", status_code=302)
    result = external_mcp.add_external_mcp(name, url, auth_header)
    if result.get("error"):
        return RedirectResponse(f"/dash/admin?tab=external&flash=error&msg={result['error']}", status_code=302)
    plugin, err = external_mcp.refresh_external_plugin(name)
    if err:
        return RedirectResponse(f"/dash/admin?tab=external&flash=error&msg=Saved+but+connect+failed:+{err[:80]}", status_code=302)
    if _ext_register_cb:
        _ext_register_cb(plugin)
    return RedirectResponse(f"/dash/admin?tab=external&flash=success&msg=Connected+{name}+({len(plugin.tools)}+tools)", status_code=302)


async def admin_remove_external(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse("/dash/admin?tab=external&flash=error&msg=Missing+name", status_code=302)
    external_mcp.remove_external_mcp(name)
    if _ext_unregister_cb:
        _ext_unregister_cb(name)
    return RedirectResponse("/dash/admin?tab=external&flash=success&msg=Removed+" + name, status_code=302)


async def admin_refresh_external(request: Request) -> Response:
    key = _get_key_from_cookie(request)
    if not key or not key.get("is_admin"):
        return RedirectResponse("/dash/login", status_code=302)
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return RedirectResponse("/dash/admin?tab=external&flash=error&msg=Missing+name", status_code=302)
    if _ext_unregister_cb:
        _ext_unregister_cb(name)
    plugin, err = external_mcp.refresh_external_plugin(name)
    if err:
        return RedirectResponse(f"/dash/admin?tab=external&flash=error&msg=Refresh+failed:+{err[:80]}", status_code=302)
    if _ext_register_cb:
        _ext_register_cb(plugin)
    return RedirectResponse(f"/dash/admin?tab=external&flash=success&msg=Refreshed+{name}+({len(plugin.tools)}+tools)", status_code=302)


def get_dashboard_routes() -> list[Route]:
    return [
        Route("/", root_redirect, methods=["GET"]),
        Route("/dash", dashboard_page, methods=["GET"]),
        Route("/dash/login", login_page, methods=["GET"]),
        Route("/dash/login", login_post, methods=["POST"]),
        Route("/dash/admin", admin_page, methods=["GET"]),
        Route("/dash/admin/keys/create", admin_create_key, methods=["POST"]),
        Route("/dash/admin/keys/edit", admin_edit_key, methods=["POST"]),
        Route("/dash/admin/keys/delete", admin_delete_key, methods=["POST"]),
        Route("/dash/credentials/set", user_set_credentials, methods=["POST"]),
        Route("/dash/credentials/remove", user_remove_credentials, methods=["POST"]),
        Route("/dash/admin/external/add", admin_add_external, methods=["POST"]),
        Route("/dash/admin/external/remove", admin_remove_external, methods=["POST"]),
        Route("/dash/admin/external/refresh", admin_refresh_external, methods=["POST"]),
        Route("/dash/audit/{entry_id:int}", audit_detail_page, methods=["GET"]),
        Route("/dash/logout", logout_page, methods=["GET"]),
    ]
