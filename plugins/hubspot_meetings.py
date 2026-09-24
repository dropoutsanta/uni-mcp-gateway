"""HubSpot Meeting Booker plugin for the MCP Gateway.

Takes any public meetings.hubspot.com URL, decodes available time slots,
and can book meetings — all without authentication. Uses HubSpot's
public booking page API (the same endpoints their frontend calls).

For CAPTCHA-protected pages: uses Playwright with a real Chromium browser
(via Xvfb) to submit the booking form, bypassing reCAPTCHA Enterprise.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone as tz
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from plugin_base import MCPPlugin, ToolDef

log = logging.getLogger(__name__)

_TIMEOUT = 15
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_meeting_url(url: str) -> dict[str, str]:
    """Extract slug, region, and API base from a HubSpot meeting URL."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.strip("/")

    slug = ""
    location = ""
    api_base = ""

    if "meetings" in host and "hubspot" in host:
        slug = path
        location = host
        region_match = re.search(r"meetings-(\w+)\.", host)
        if region_match:
            api_base = f"https://app-{region_match.group(1)}.hubspot.com"
        else:
            api_base = "https://app.hubspot.com"
    elif "app" in host and "hubspot" in host:
        slug = re.sub(r"^meetings/?", "", path)
        region_match = re.search(r"app-(\w+)\.", host)
        if region_match:
            location = f"meetings-{region_match.group(1)}.hubspot.com"
            api_base = f"https://app-{region_match.group(1)}.hubspot.com"
        else:
            location = "meetings.hubspot.com"
            api_base = "https://app.hubspot.com"
    else:
        slug = path if "/" in path else path
        location = "meetings.hubspot.com"
        api_base = "https://app.hubspot.com"

    if not slug:
        return {"error": f"Could not extract meeting slug from URL: {url}"}

    return {"slug": slug, "location": location, "api_base": api_base}


def _fetch_meeting_data(
    slug: str, location: str, api_base: str, timezone: str,
    month_offset: int = 0,
) -> dict:
    """Fetch meeting metadata + availability from HubSpot's public API."""
    params: dict[str, Any] = {
        "slug": slug,
        "now": str(int(time.time() * 1000)),
        "timezone": timezone,
        "location": location,
        "includeInactiveLink": "true",
    }
    if month_offset > 0:
        params["monthOffset"] = month_offset

    headers = {
        **_BROWSER_HEADERS,
        "Referer": f"https://{location}/{slug}",
        "Origin": f"https://{location}",
    }

    r = httpx.get(
        f"{api_base}/api/meetings-public/v3/book",
        params=params, headers=headers, timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _format_slot(start_ms: int, end_ms: int, timezone: str) -> dict:
    """Convert UTC millis to human-readable slot."""
    tz_info = ZoneInfo(timezone)
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=tz.utc).astimezone(tz_info)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=tz.utc).astimezone(tz_info)
    return {
        "date": start_dt.strftime("%A, %B %d, %Y"),
        "start": start_dt.strftime("%I:%M %p"),
        "end": end_dt.strftime("%I:%M %p"),
        "start_utc_ms": start_ms,
        "end_utc_ms": end_ms,
        "iso": start_dt.isoformat(),
    }


_BOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "hubspot_book_browser.py")


def _book_via_subprocess(
    page_url: str,
    start_ms: int,
    organizer_tz: str,
    first_name: str,
    last_name: str,
    email: str,
    custom_fields: dict | None = None,
) -> dict[str, Any]:
    """Book via a separate Python process to avoid async loop conflicts."""
    args_json = json.dumps({
        "url": page_url,
        "start_ms": start_ms,
        "organizer_tz": organizer_tz,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "custom_fields": custom_fields or {},
    })
    result = subprocess.run(
        ["python", _BOOK_SCRIPT, args_json],
        capture_output=True, text=True, timeout=90,
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
    )
    if result.stderr:
        log.info("Booking script stderr: %s", result.stderr.strip()[-500:])
    if result.returncode != 0:
        stderr = result.stderr.strip()[-500:]
        return {"error": f"Booking subprocess failed: {stderr}"}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"error": f"Bad output from booking script: {result.stdout[:300]}"}


class HubSpotMeetingsPlugin(MCPPlugin):
    name = "hubspot_meetings"
    tools: dict[str, ToolDef] = {}

    def __init__(self):
        self.tools = {
            "how_to_use_me": ToolDef(
                access="read", handler=self.how_to_use_me,
                description=(
                    "START HERE. Explains how to use the HubSpot Meeting Booker — "
                    "check availability and book meetings on ANY public HubSpot "
                    "meeting link. No authentication needed."
                ),
            ),
            "get_availability": ToolDef(
                access="read", handler=self.get_availability,
                description=(
                    "Get available time slots from any HubSpot meeting link. Takes "
                    "a meetings.hubspot.com URL and returns all open slots with "
                    "dates and times.\n\n"
                    "Params: url (required — full HubSpot meeting URL, e.g. "
                    "'https://meetings.hubspot.com/jane-doe/30min'), timezone (default "
                    "America/New_York), month_offset (0 = current month, 1 = next "
                    "month, etc.).\n\n"
                    "Returns: meeting info (title, duration, location) + list of "
                    "available time slots with human-readable dates."
                ),
            ),
            "book_meeting": ToolDef(
                access="write", handler=self.book_meeting,
                description=(
                    "Book a meeting slot on a HubSpot meeting link. First call "
                    "get_availability to see open slots, then use a slot's "
                    "start_utc_ms to book.\n\n"
                    "Params: url (required), start_time (required — start_utc_ms "
                    "from get_availability), first_name (required), last_name "
                    "(required), email (required), timezone (default "
                    "America/New_York), custom_fields (optional dict — for pages "
                    "with extra form fields like Company, Phone, etc. Keys are "
                    "field labels as shown on the form, values are strings).\n\n"
                    "Handles reCAPTCHA automatically via browser automation."
                ),
            ),
        }

    def how_to_use_me(self, **kwargs) -> Any:
        return {
            "overview": (
                "HubSpot Meeting Booker lets you check availability and book "
                "meetings on ANY public HubSpot meeting link — no HubSpot account "
                "or authentication needed. Works for your own links and prospect "
                "links alike."
            ),
            "workflow": [
                "STEP 1: get_availability(url='https://meetings.hubspot.com/person/meeting-type') "
                "— returns meeting details + all available time slots.",
                "STEP 2: Pick a slot from the results. Note its start_utc_ms value.",
                "STEP 3: book_meeting(url=same_url, start_time=start_utc_ms, "
                "first_name='...', last_name='...', email='...') — books the slot.",
            ],
            "supported_url_formats": [
                "meetings.hubspot.com/person/meeting-type",
                "meetings-eu1.hubspot.com/person/meeting-type",
                "app.hubspot.com/meetings/person/meeting-type",
            ],
            "tips": [
                "get_availability returns slots for the current ~2-week window by "
                "default. Use month_offset=1 to see next month's availability.",
                "The response includes meeting metadata: title, duration, location "
                "(Zoom/Google Meet/etc.), and whether CAPTCHA is required.",
                "Booking automatically handles reCAPTCHA — no extra config needed.",
                "Some meeting pages have extra form fields (Company, Phone, etc.). "
                "Pass these via custom_fields={'Company': 'Acme', 'Phone': '555-1234'}.",
                "Always present slots in the user's local timezone.",
            ],
        }

    def get_availability(self, **kwargs) -> Any:
        url = kwargs.get("url", "")
        if not url:
            return {"error": "url is required — provide a HubSpot meeting link"}

        parsed = _parse_meeting_url(url)
        if "error" in parsed:
            return parsed

        timezone = kwargs.get("timezone", "America/New_York")
        month_offset = int(kwargs.get("month_offset", 0))

        try:
            data = _fetch_meeting_data(
                parsed["slug"], parsed["location"], parsed["api_base"],
                timezone, month_offset,
            )
        except httpx.HTTPStatusError as e:
            return {"error": f"HubSpot API error: {e.response.status_code}", "detail": e.response.text[:300]}
        except Exception as e:
            return {"error": str(e)}

        custom = data.get("customParams", {})
        display = custom.get("displayInfo", {})
        durations = custom.get("durations", [])
        duration_mins = durations[0] // 60000 if durations else None

        meeting_info = {
            "title": display.get("headline", ""),
            "duration_minutes": duration_mins,
            "location_type": custom.get("location", ""),
            "organizer_timezone": custom.get("timezone", ""),
            "captcha_required": custom.get("recaptchaEnabled", False),
            "slug": parsed["slug"],
        }

        link_avail = data.get("linkAvailability", {})
        has_more = link_avail.get("hasMore", False)
        by_duration = link_avail.get("linkAvailabilityByDuration", {})

        slots = []
        for dur_key, dur_data in by_duration.items():
            for avail in dur_data.get("availabilities", []):
                slots.append(_format_slot(
                    avail["startMillisUtc"], avail["endMillisUtc"], timezone,
                ))

        slots_by_date: dict[str, list] = {}
        for s in slots:
            date = s["date"]
            slots_by_date.setdefault(date, []).append({
                "time": f"{s['start']} - {s['end']}",
                "start_utc_ms": s["start_utc_ms"],
                "end_utc_ms": s["end_utc_ms"],
            })

        return {
            "meeting": meeting_info,
            "timezone": timezone,
            "total_slots": len(slots),
            "has_more_months": has_more,
            "availability": slots_by_date,
        }

    def book_meeting(self, **kwargs) -> Any:
        url = kwargs.get("url", "")
        start_time = kwargs.get("start_time")
        first_name = kwargs.get("first_name", "")
        last_name = kwargs.get("last_name", "")
        email = kwargs.get("email", "")

        if not url:
            return {"error": "url is required"}
        if not start_time:
            return {"error": "start_time is required (use start_utc_ms from get_availability)"}
        if not email:
            return {"error": "email is required"}
        if not first_name:
            return {"error": "first_name is required"}

        parsed = _parse_meeting_url(url)
        if "error" in parsed:
            return parsed

        timezone = kwargs.get("timezone", "America/New_York")
        start_time = int(start_time)

        try:
            avail_data = _fetch_meeting_data(
                parsed["slug"], parsed["location"], parsed["api_base"], timezone,
            )
        except Exception as e:
            return {"error": f"Failed to fetch meeting data: {e}"}

        custom = avail_data.get("customParams", {})
        durations = custom.get("durations", [1800000])
        duration = durations[0]
        captcha_required = custom.get("recaptchaEnabled", False)
        organizer_tz = custom.get("timezone", timezone)

        custom_fields = kwargs.get("custom_fields", {})
        if isinstance(custom_fields, str):
            try:
                custom_fields = json.loads(custom_fields)
            except json.JSONDecodeError:
                custom_fields = {}

        if captcha_required:
            return self._book_with_browser(
                parsed, organizer_tz, start_time, first_name, last_name, email,
                custom_fields,
            )

        return self._book_with_http(
            parsed, timezone, start_time, duration, first_name, last_name, email,
            kwargs.get("guest_emails", []), custom_fields,
        )

    def _book_with_http(
        self, parsed: dict, timezone: str, start_time: int, duration: int,
        first_name: str, last_name: str, email: str, guest_emails: Any,
        custom_fields: dict | None = None,
    ) -> dict:
        """Fast path: direct HTTP POST for non-CAPTCHA pages."""
        if isinstance(guest_emails, str):
            guest_emails = [e.strip() for e in guest_emails.split(",") if e.strip()]

        book_body: dict[str, Any] = {
            "slug": parsed["slug"],
            "firstName": first_name,
            "lastName": last_name or "",
            "email": email,
            "startTime": start_time,
            "duration": duration,
            "timezone": timezone,
            "locale": "en-us",
            "guestEmails": guest_emails or [],
            "likelyAvailableUserIds": [],
        }
        if custom_fields:
            book_body["customFormFields"] = custom_fields

        headers = {
            **_BROWSER_HEADERS,
            "Content-Type": "application/json",
            "Referer": f"https://{parsed['location']}/{parsed['slug']}",
            "Origin": f"https://{parsed['location']}",
        }

        book_base = parsed["api_base"].replace("://app-", "://api-").replace("://app.", "://api.")
        book_url = f"{book_base}/meetings-public/v1/book"

        try:
            r = httpx.post(
                book_url,
                params={"slug": parsed["slug"], "timezone": timezone},
                headers=headers, json=book_body, timeout=30,
            )
            r.raise_for_status()
            result = r.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"Booking failed: {e.response.status_code}", "detail": e.response.text[:500]}
        except Exception as e:
            return {"error": f"Booking failed: {str(e)}"}

        return self._format_booking_result(result)

    def _book_with_browser(
        self, parsed: dict, organizer_tz: str, start_time: int,
        first_name: str, last_name: str, email: str,
        custom_fields: dict | None = None,
    ) -> dict:
        """Browser path: Playwright automation for CAPTCHA-protected pages."""
        page_url = f"https://{parsed['location']}/{parsed['slug']}"
        log.info("Booking via Playwright subprocess: %s", page_url)

        try:
            result = _book_via_subprocess(
                page_url, start_time, organizer_tz,
                first_name, last_name, email, custom_fields,
            )
        except Exception as e:
            log.exception("Playwright booking failed")
            return {"error": f"Browser booking failed: {str(e)}"}

        if "error" in result:
            return result

        status = result.get("status")
        body = result.get("body", {})

        if status == 200:
            return self._format_booking_result(body)

        return {
            "error": f"Booking returned status {status}",
            "detail": str(body)[:500],
        }

    @staticmethod
    def _format_booking_result(result: dict) -> dict:
        contact = result.get("contact", {})
        organizer = result.get("organizer", {})
        return {
            "success": True,
            "subject": result.get("subject"),
            "start": result.get("start"),
            "end": result.get("end"),
            "contact": {
                "name": contact.get("name"),
                "email": contact.get("email"),
            },
            "organizer": {
                "name": organizer.get("name"),
            },
            "conference_link": result.get("location"),
        }
