"""Slack /coldmessage slash-command via Socket Mode for SmartScout brand lookups.

Connects to Slack via WebSocket (Socket Mode) and listens for /coldmessage.
Restricted to #axisbrands (C0A5KV5QQ6S).

Requires SLACK_APP_TOKEN env var (xapp-...) with connections:write scope.

Usage in Slack:
  /coldmessage Nike          — search for a brand
  /coldmessage report 12345  — full brand dossier
  /coldmessage help          — show usage
"""

from __future__ import annotations

import logging
import os
import threading
import traceback

import httpx
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

import auth
from plugin_base import MCPPlugin, RequestContext, ToolDef, _current_context
from plugins.smartscout import SmartScoutPlugin

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS = {"C0A5KV5QQ6S"}

_ss = SmartScoutPlugin()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _set_admin_context() -> None:
    _current_context.set(
        RequestContext(
            key_id=auth.ADMIN_KEY_ID,
            is_admin=True,
            credentials={},
            data_scopes={},
        )
    )


def _money(val: float | int | None) -> str:
    if val is None:
        return "N/A"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


def _pct(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:+.1f}%"


def _format_search(brands: list[dict]) -> dict:
    if not brands:
        return {"response_type": "in_channel", "text": "No brands found."}

    lines = [f"Found *{len(brands)}* brand{'s' if len(brands) != 1 else ''}:\n"]
    for b in brands[:15]:
        rev = _money(b.get("monthlyRevenue"))
        growth = _pct(b.get("momGrowth"))
        lines.append(
            f"• *{b.get('name', '?')}* — {rev}/mo ({growth} MoM)  ·  ID `{b.get('id')}`"
        )

    if len(brands) > 15:
        lines.append(f"\n_…and {len(brands) - 15} more_")

    lines.append("\n_Use `/coldmessage report [id]` for a full dossier._")
    return {"response_type": "in_channel", "text": "\n".join(lines)}


def _format_report(report: dict) -> dict:
    brand = report.get("brand", {})
    products = report.get("products", [])
    cats = report.get("category_breakdown", [])

    name = brand.get("name", "Unknown")
    rev = _money(brand.get("monthlyRevenue"))
    units = f"{brand.get('monthlyUnitsSold', 0):,}" if brand.get("monthlyUnitsSold") else "N/A"
    growth = _pct(brand.get("momGrowth"))
    growth12 = _pct(brand.get("momGrowth12"))
    score = brand.get("brandScore", "—")
    avg_price = f"${brand.get('avgPrice', 0):.2f}" if brand.get("avgPrice") else "—"
    rating = brand.get("reviewRating", "—")
    total_prods = brand.get("totalProducts", "—")
    total_reviews = f"{brand.get('totalReviews', 0):,}" if brand.get("totalReviews") else "—"
    storefront = brand.get("storefrontUrl", "")
    ad_spend = _money(brand.get("totalAdSpend"))

    lines = [
        f"*{name}*  (ID `{brand.get('id')}`)",
        "",
        f"*Revenue:* {rev}/mo  |  *Units:* {units}/mo",
        f"*MoM:* {growth}  |  *12-mo:* {growth12}",
        f"*Brand Score:* {score}  |  *Avg Price:* {avg_price}  |  *Rating:* {rating}",
        f"*Products:* {total_prods}  |  *Reviews:* {total_reviews}  |  *Ad Spend:* {ad_spend}",
    ]

    if storefront:
        lines.append(f"*Storefront:* {storefront}")

    if products:
        lines.append("\n*Top Products:*")
        for i, p in enumerate(products[:5], 1):
            title = (p.get("title") or "")[:55]
            prev = _money(p.get("revenue"))
            rank = p.get("rank") or "—"
            sellers = p.get("num_sellers") or "—"
            lines.append(f"{i}. {title}  —  {prev}/mo  ·  Rank #{rank}  ·  {sellers} sellers")

    if cats:
        lines.append("\n*Category Breakdown:*")
        for c in cats[:5]:
            crev = _money(c.get("latest_revenue"))
            lines.append(f"• SubCat `{c.get('subcategory_id')}`: {crev}/wk")

    return {"response_type": "in_channel", "text": "\n".join(lines)}


# ── SmartScout work (runs in a background thread) ───────────────────────────


def _do_smartscout_work(query: str) -> dict:
    _set_admin_context()

    if query.lower().startswith("report "):
        brand_id_str = query[7:].strip()
        try:
            brand_id = int(brand_id_str)
        except ValueError:
            return {
                "response_type": "ephemeral",
                "text": f"Invalid brand ID: `{brand_id_str}`. Use a numeric ID from search results.",
            }
        report = _ss.brand_report(brand_id=brand_id)
        if isinstance(report, dict) and "error" in report:
            return {"response_type": "in_channel", "text": f"SmartScout error: {report['error']}"}
        return _format_report(report)

    result = _ss.search_brands(name=query)
    if isinstance(result, dict) and "error" in result:
        return {"response_type": "in_channel", "text": f"SmartScout error: {result['error']}"}

    brands = result.get("brands", [])
    if len(brands) == 1:
        bid = brands[0].get("id")
        if bid:
            report = _ss.brand_report(brand_id=bid)
            if isinstance(report, dict) and "error" not in report:
                return _format_report(report)

    return _format_search(brands)


# ── Socket Mode handler ─────────────────────────────────────────────────────

_HELP_TEXT = (
    "*SmartScout Brand Lookup*\n\n"
    "`/coldmessage [brand name]` — search for a brand and auto-pull report if 1 match\n"
    "`/coldmessage report [brand_id]` — full brand dossier by ID\n"
    "`/coldmessage help` — show this message"
)


def _handle_socket_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    if req.type != "slash_commands":
        return

    payload = req.payload or {}
    if payload.get("command") != "/coldmessage":
        return

    channel_id = payload.get("channel_id", "")
    text = (payload.get("text") or "").strip()
    response_url = payload.get("response_url", "")

    if channel_id not in ALLOWED_CHANNELS:
        client.send_socket_mode_response(
            SocketModeResponse(
                envelope_id=req.envelope_id,
                payload={"response_type": "ephemeral", "text": "This command is only available in #axisbrands."},
            )
        )
        return

    if not text or text.lower() == "help":
        client.send_socket_mode_response(
            SocketModeResponse(
                envelope_id=req.envelope_id,
                payload={"response_type": "ephemeral", "text": _HELP_TEXT},
            )
        )
        return

    ack = f":mag: Searching SmartScout for *{text}*…"
    if text.lower().startswith("report "):
        ack = ":bar_chart: Pulling full SmartScout report…"

    client.send_socket_mode_response(
        SocketModeResponse(
            envelope_id=req.envelope_id,
            payload={"response_type": "in_channel", "text": ack},
        )
    )

    def _bg() -> None:
        try:
            result = _do_smartscout_work(text)
            httpx.post(response_url, json=result, timeout=30)
        except Exception:
            logger.error("coldmessage bg error:\n%s", traceback.format_exc())
            try:
                httpx.post(
                    response_url,
                    json={"response_type": "ephemeral", "text": "Something went wrong. Try again."},
                    timeout=10,
                )
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()


def _start_socket_mode() -> None:
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not app_token:
        logger.warning("[coldmessage] SLACK_APP_TOKEN not set — Socket Mode disabled")
        return

    try:
        sm_client = SocketModeClient(app_token=app_token)
        sm_client.socket_mode_request_listeners.append(_handle_socket_event)
        sm_client.connect()
        logger.info("[coldmessage] Socket Mode connected")
    except Exception:
        logger.error("[coldmessage] Socket Mode failed:\n%s", traceback.format_exc())


# ── Plugin definition ────────────────────────────────────────────────────────


class ColdMessageSlashPlugin(MCPPlugin):
    """No MCP tools — starts a Socket Mode listener for /coldmessage."""

    name = "coldmessage_slash"
    tools: dict[str, ToolDef] = {}

    def __init__(self) -> None:
        super().__init__()
        threading.Thread(target=_start_socket_mode, daemon=True, name="coldmessage-socket").start()
