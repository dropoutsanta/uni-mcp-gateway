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

ALLOWED_CHANNELS = {"C0A5KV5QQ6S", "C0A5RG2HJVA"}

_ss = SmartScoutPlugin()


# ── Helpers ──────────────────────────────────────────────────────────────────


_CRED_KEY_ID = "g0d"


def _set_admin_context() -> None:
    _current_context.set(
        RequestContext(
            key_id=_CRED_KEY_ID,
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


def _brand_header(brand: dict) -> list[str]:
    """Common brand header lines used by both summary and full report."""
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
    ad_spend = _money(brand.get("totalAdSpend"))
    storefront = brand.get("storefrontUrl", "")

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
    return lines


def _format_summary(report: dict) -> dict:
    """Quick summary — brand basics + top products list."""
    brand = report.get("brand", {})
    products = report.get("products", [])

    lines = _brand_header(brand)

    if products:
        lines.append("\n*Top Products:*")
        for i, p in enumerate(products[:5], 1):
            title = (p.get("title") or "")[:55]
            prev = _money(p.get("revenue"))
            rank = p.get("rank") or "—"
            lines.append(f"{i}. {title}  —  {prev}/mo  ·  Rank #{rank}")

    lines.append(f"\n_Use `/coldmessage report {brand.get('name', '')}` for the full dossier._")
    return {"response_type": "in_channel", "text": "\n".join(lines)}


def _format_full_report(report: dict) -> dict:
    """Full dossier — everything SmartScout has."""
    brand = report.get("brand", {})
    products = report.get("products", [])
    product_details = report.get("product_details", [])
    sellers = report.get("sellers", [])
    competitors = report.get("competitors", {})
    landscape = report.get("subcategory_landscape", {})
    history = report.get("revenue_history", {})

    lines = _brand_header(brand)

    # Revenue trend
    if history and history.get("samples"):
        samples = history["samples"]
        lines.append(f"\n*Revenue Trend* ({history.get('total_weeks', '?')} weeks of data):")
        for s in samples[-6:]:
            lines.append(f"  {s.get('date', '?')}  —  {_money(s.get('revenue'))}/wk  ·  {s.get('asins', '?')} ASINs")

    # Top products with detail
    if products:
        lines.append("\n*Top Products:*")
        for i, p in enumerate(products[:5], 1):
            title = (p.get("title") or "")[:55]
            prev = _money(p.get("revenue"))
            rank = p.get("rank") or "—"
            sellers_ct = p.get("num_sellers") or "—"
            growth = _pct(p.get("mom_growth"))
            bbp = f"${p.get('buybox_price', 0):.2f}" if p.get("buybox_price") else "—"
            lines.append(f"{i}. *{title}*")
            lines.append(f"    {prev}/mo  ·  Rank #{rank}  ·  {sellers_ct} sellers  ·  BB ${bbp}  ·  {growth} MoM")

    # Per-product organic ranks & search terms
    if product_details:
        lines.append("\n*Product Deep-Dive:*")
        for det in product_details[:3]:
            asin = det.get("asin", "?")
            lines.append(f"\n  _ASIN {asin}_")

            ranks = det.get("organic_ranks", [])
            if ranks:
                total = det.get("total_ranked_terms", len(ranks))
                lines.append(f"  Organic ranks ({total} terms total, top 10):")
                for r in ranks[:10]:
                    term = r.get("term", "?")
                    pos = r.get("rank", "?")
                    vol = r.get("volume")
                    vol_str = f"  ·  {vol:,} vol" if vol else ""
                    lines.append(f"    #{pos} — _{term}_{vol_str}")

            bb = det.get("buybox_sellers", [])
            if bb:
                lines.append(f"  Buy Box sellers:")
                for s in bb[:5]:
                    sname = s.get("seller") or "?"
                    pct = f"{s.get('buybox_pct', 0):.0f}%" if s.get("buybox_pct") else "—"
                    fba = " (FBA)" if s.get("is_fba") else ""
                    lines.append(f"    • {sname} — {pct} BB{fba}")

    # Seller coverage
    if sellers:
        lines.append("\n*Seller Coverage:*")
        for s in sellers[:7]:
            sname = s.get("name") or "?"
            srev = _money(s.get("revenue"))
            offers = s.get("offers") or "?"
            pct = f"{s.get('brand_pct', 0):.0f}%" if s.get("brand_pct") else "—"
            lines.append(f"• {sname}  —  {srev}/mo  ·  {offers} offers  ·  {pct} brand share")

    # Competitors
    if competitors and competitors.get("top_15"):
        lines.append(f"\n*Competitors* (vs ASIN {competitors.get('for_asin', '?')}):")
        for c in competitors["top_15"][:8]:
            cname = c.get("brand") or "?"
            ctitle = (c.get("title") or "")[:40]
            crev = _money(c.get("revenue"))
            rel = f"{c.get('relevancy', 0):.0f}%" if c.get("relevancy") else ""
            lines.append(f"• {cname} — {ctitle}  ·  {crev}/mo  {rel}")

    # Subcategory landscape
    if landscape and landscape.get("top_brands"):
        lines.append(f"\n*Category Landscape* (SubCat `{landscape.get('subcategory_id')}`):")
        for lb in landscape["top_brands"][:8]:
            lname = lb.get("brand") or "?"
            lrev = _money(lb.get("revenue"))
            share = f"{lb.get('market_share', 0):.1f}%" if lb.get("market_share") else ""
            lines.append(f"• {lname}  —  {lrev}/wk  {share}")

    return {"response_type": "in_channel", "text": "\n".join(lines)}


# ── SmartScout work (runs in a background thread) ───────────────────────────


def _do_smartscout_work(query: str) -> dict:
    _set_admin_context()

    is_full = query.lower().startswith("report ")
    search_term = query[7:].strip() if is_full else query
    fmt = _format_full_report if is_full else _format_summary

    # If it's a numeric ID, go straight to report
    try:
        brand_id = int(search_term)
        report = _ss.brand_report(brand_id=brand_id)
        if isinstance(report, dict) and "error" in report:
            return {"response_type": "in_channel", "text": f"SmartScout error: {report['error']}"}
        return fmt(report)
    except ValueError:
        pass

    result = _ss.search_brands(name=search_term)
    if isinstance(result, dict) and "error" in result:
        return {"response_type": "in_channel", "text": f"SmartScout error: {result['error']}"}

    brands = result.get("brands", [])
    if len(brands) == 1:
        bid = brands[0].get("id")
        if bid:
            report = _ss.brand_report(brand_id=bid)
            if isinstance(report, dict) and "error" not in report:
                return fmt(report)

    return _format_search(brands)


# ── Socket Mode handler ─────────────────────────────────────────────────────

_HELP_TEXT = (
    "*SmartScout Brand Lookup*\n\n"
    "`/coldmessage [brand]` — quick summary (revenue, top products)\n"
    "`/coldmessage report [brand]` — full dossier (organic ranks, search terms, sellers, competitors, category landscape)\n"
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
            print(f"[coldmessage] starting work: {text!r}", flush=True)
            result = _do_smartscout_work(text)
            print(f"[coldmessage] work done, posting to response_url", flush=True)
            resp = httpx.post(response_url, json=result, timeout=30)
            print(f"[coldmessage] posted to Slack: {resp.status_code}", flush=True)
        except Exception:
            print(f"[coldmessage] BG ERROR:\n{traceback.format_exc()}", flush=True)
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
        print("[coldmessage] SLACK_APP_TOKEN not set — Socket Mode disabled", flush=True)
        return

    try:
        sm_client = SocketModeClient(app_token=app_token)
        sm_client.socket_mode_request_listeners.append(_handle_socket_event)
        sm_client.connect()
        print("[coldmessage] Socket Mode connected successfully", flush=True)
    except Exception:
        print(f"[coldmessage] Socket Mode FAILED:\n{traceback.format_exc()}", flush=True)


# ── Plugin definition ────────────────────────────────────────────────────────


class ColdMessageSlashPlugin(MCPPlugin):
    """No MCP tools — starts a Socket Mode listener for /coldmessage."""

    name = "coldmessage_slash"
    tools: dict[str, ToolDef] = {}

    def __init__(self) -> None:
        super().__init__()
        threading.Thread(target=_start_socket_mode, daemon=True, name="coldmessage-socket").start()
