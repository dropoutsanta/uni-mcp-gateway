"""Slack /coldmessage slash-command handler for SmartScout brand lookups.

Restricted to #axisbrands (C0A5KV5QQ6S). Registers a webhook route at
/webhook/coldmessage that Slack POSTs to when someone uses the command.

Usage in Slack:
  /coldmessage Nike          — search for a brand
  /coldmessage report 12345  — full brand dossier
  /coldmessage help          — show usage
"""

from __future__ import annotations

import asyncio
import logging
import traceback

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import auth
from plugin_base import MCPPlugin, RequestContext, ToolDef, _current_context
from plugins.smartscout import SmartScoutPlugin

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS = {"C0A5KV5QQ6S"}

_ss = SmartScoutPlugin()


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
        lines.append(f"• *{b.get('name', '?')}* — {rev}/mo ({growth} MoM)  ·  ID `{b.get('id')}`")

    if len(brands) > 15:
        lines.append(f"\n_…and {len(brands) - 15} more_")

    lines.append("\n_Use `/coldmessage report [id]` for a full dossier._")
    return {"response_type": "in_channel", "text": "\n".join(lines)}


def _format_report(report: dict) -> dict:
    brand = report.get("brand", {})
    products = report.get("products", [])
    history = report.get("revenue_history", {})
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

    if history and history.get("samples"):
        samples = history["samples"]
        if len(samples) >= 2:
            first = samples[0]
            last = samples[-1]
            lines.append(f"\n*Trend:* {first.get('date', '?')} → {last.get('date', '?')}")

    return {"response_type": "in_channel", "text": "\n".join(lines)}


def _do_smartscout_work(query: str) -> dict:
    """Run SmartScout lookups synchronously (called via asyncio.to_thread)."""
    _set_admin_context()

    if query.lower().startswith("report "):
        brand_id_str = query[7:].strip()
        try:
            brand_id = int(brand_id_str)
        except ValueError:
            return {"response_type": "ephemeral", "text": f"Invalid brand ID: `{brand_id_str}`. Use a numeric ID from search results."}

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


async def _post_to_slack(response_url: str, payload: dict) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(response_url, json=payload, timeout=15)


async def _process_query(query: str, response_url: str) -> None:
    try:
        payload = await asyncio.to_thread(_do_smartscout_work, query)
        await _post_to_slack(response_url, payload)
    except Exception as exc:
        logger.error("coldmessage error: %s", traceback.format_exc())
        try:
            await _post_to_slack(response_url, {
                "response_type": "ephemeral",
                "text": f"Something went wrong: {exc}",
            })
        except Exception:
            pass


async def handle_coldmessage(request: Request) -> JSONResponse:
    form = await request.form()
    channel_id = form.get("channel_id", "")
    text = (form.get("text") or "").strip()
    response_url = form.get("response_url", "")

    if channel_id not in ALLOWED_CHANNELS:
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "This command is only available in #axisbrands.",
        })

    if not text or text.lower() == "help":
        return JSONResponse({
            "response_type": "ephemeral",
            "text": (
                "*SmartScout Brand Lookup*\n\n"
                "`/coldmessage [brand name]` — search for a brand and auto-pull report if 1 match\n"
                "`/coldmessage report [brand_id]` — full brand dossier by ID\n"
                "`/coldmessage help` — show this message"
            ),
        })

    asyncio.create_task(_process_query(text, response_url))

    if text.lower().startswith("report "):
        ack = ":bar_chart: Pulling full SmartScout report…"
    else:
        ack = f":mag: Searching SmartScout for *{text}*…"

    return JSONResponse({"response_type": "in_channel", "text": ack})


class ColdMessageSlashPlugin(MCPPlugin):
    """No MCP tools — just registers the /webhook/coldmessage route."""

    name = "coldmessage_slash"
    tools: dict[str, ToolDef] = {}

    def extra_routes(self) -> list[Route]:
        return [Route("/webhook/coldmessage", endpoint=handle_coldmessage, methods=["POST"])]
