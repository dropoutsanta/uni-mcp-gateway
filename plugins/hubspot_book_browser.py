#!/usr/bin/env python3
"""Standalone Playwright booking script for HubSpot meetings.

Called as a subprocess by the hubspot_meetings plugin to avoid
Playwright sync/async event loop conflicts with the gateway.

Usage:
  python hubspot_book_browser.py '<json_args>'

Input JSON: {url, start_ms, organizer_tz, first_name, last_name, email}
Output JSON: {status, body} or {error}
"""

import calendar
import json
import re
import sys
from datetime import datetime, timezone as tz
from zoneinfo import ZoneInfo


def main(args: dict) -> dict:
    from playwright.sync_api import sync_playwright

    page_url = args["url"]
    start_ms = int(args["start_ms"])
    organizer_tz = args["organizer_tz"]
    first_name = args["first_name"]
    last_name = args["last_name"]
    email = args["email"]
    custom_fields = args.get("custom_fields", {})

    tz_info = ZoneInfo(organizer_tz)
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=tz.utc).astimezone(tz_info)
    target_day = str(start_dt.day)
    target_month_year = start_dt.strftime("%B %Y").lower()
    target_time = start_dt.strftime("%I:%M %p").lstrip("0").lower()

    booking_result: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            timezone_id=organizer_tz,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        def on_response(response):
            if response.request.method == "POST" and "book" in response.url:
                booking_result["status"] = response.status
                try:
                    booking_result["body"] = response.json()
                except Exception:
                    booking_result["body_text"] = response.text()

        page.on("response", on_response)

        page.goto(page_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3000)

        def _get_displayed_month() -> str:
            body = page.locator("body").text_content() or ""
            for m in range(1, 13):
                name = calendar.month_name[m]
                for yr in range(2025, 2029):
                    label = f"{name} {yr}"
                    if label in body:
                        return label.lower()
            return ""

        displayed = _get_displayed_month()
        for _ in range(12):
            if target_month_year in displayed:
                break
            next_btn = page.locator("[data-test-id='date-picker-header-next-btn']")
            if next_btn.count() and next_btn.is_enabled():
                next_btn.click(force=True)
                page.wait_for_timeout(600)
                displayed = _get_displayed_month()
            else:
                break

        # click the target day
        for btn in page.locator("button[data-test-id]").all():
            txt = (btn.text_content() or "").strip()
            test_id = btn.get_attribute("data-test-id") or ""
            if txt == target_day and "unavailable" not in test_id:
                btn.click()
                break
        else:
            browser.close()
            return {"error": f"Day {target_day} not available on calendar"}

        page.wait_for_timeout(2500)

        # click the target time slot (not necessarily a <button>)
        time_variants = [
            target_time,
            target_time.replace("am", "AM").replace("pm", "PM"),
        ]
        time_clicked = False
        for label in time_variants:
            loc = page.get_by_text(label, exact=True)
            if loc.count() > 0:
                loc.first.click()
                time_clicked = True
                break

        if not time_clicked:
            browser.close()
            return {"error": f"Time slot '{target_time}' not found on page"}

        page.wait_for_timeout(2000)

        page.locator("input[name='firstName']").fill(first_name)
        page.locator("input[name='lastName']").fill(last_name)
        page.locator("input[name='email'], input[type='email']").first.fill(email)

        for label_text, value in custom_fields.items():
            field = page.get_by_label(label_text, exact=False)
            if field.count() > 0:
                tag = field.first.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    field.first.select_option(label=value)
                elif tag == "textarea":
                    field.first.fill(value)
                else:
                    field.first.fill(value)

        page.wait_for_timeout(500)

        page.get_by_role(
            "button", name=re.compile(r"confirm", re.IGNORECASE)
        ).click()

        page.wait_for_timeout(12_000)
        browser.close()

    return booking_result


if __name__ == "__main__":
    args = json.loads(sys.argv[1])
    try:
        result = main(args)
    except Exception as e:
        result = {"error": str(e)}
    print(json.dumps(result), flush=True)
