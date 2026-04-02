"""AI Ark plugin for MCP Gateway.

Exposes the full AI Ark API as MCP tools: company search, people search,
reverse lookup, phone finder, personality analysis, email export, and more.
"""

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from plugin_base import MCPPlugin, ToolDef, get_credentials


_ARK_BASE = "https://api.ai-ark.com/api/developer-portal"
_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/data/results"))


def _build_any_include(values: list[str]) -> dict:
    return {"any": {"include": values}}


def _build_any_include_smart(values: list[str]) -> dict:
    return {"any": {"include": {"mode": "SMART", "content": values}}}


def _parse_json_or_csv(value: str) -> list[str]:
    """Parse a JSON array string or comma-separated string into a list."""
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
    return [v.strip() for v in value.split(",") if v.strip()]


def _coerce_filters(raw) -> dict | None:
    """Accept filters_json as a JSON string or an already-parsed dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    return None


def _parse_range_pairs(value: str) -> list[dict]:
    """Parse range specs like '1-10,51-200' into [{start:1,end:10},{start:51,end:200}]."""
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                ranges.append({"start": int(lo.strip()), "end": int(hi.strip())})
            except ValueError:
                pass
    return ranges


def _save_receipt_mapping(track_id: str, receipt_id: str) -> None:
    """Map a trackId to a receipt ID so get_export_results can find webhook data."""
    mapping_path = _RESULTS_DIR / "_mappings.json"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    mappings = {}
    if mapping_path.exists():
        try:
            mappings = json.loads(mapping_path.read_text())
        except Exception:
            pass
    mappings[track_id] = receipt_id
    mapping_path.write_text(json.dumps(mappings))


def _load_receipt_id(track_id: str) -> str | None:
    """Look up the receipt ID for a given trackId."""
    mapping_path = _RESULTS_DIR / "_mappings.json"
    if not mapping_path.exists():
        return None
    try:
        mappings = json.loads(mapping_path.read_text())
        return mappings.get(track_id)
    except Exception:
        return None


def _webhook_url_for(receipt_id: str) -> str:
    """Generate the MCP gateway webhook URL for a given receipt ID."""
    base = os.environ.get("MCP_BASE_URL", "https://shitty-agent-gateway-27.fly.dev").rstrip("/")
    return f"{base}/webhook/{receipt_id}"


class AiArkPlugin(MCPPlugin):
    name = "ai_ark"

    def __init__(self):
        self.tools = {
            "how_it_works": ToolDef(
                access="read",
                handler=self.how_it_works,
                description="Best-practices guide for using AI Ark tools. Call this first if you're unfamiliar with AI Ark.",
            ),
            "test_search_people": ToolDef(
                access="read",
                handler=self.test_search_people,
                description=(
                    "Count how many leads match your search criteria. Use this to estimate\n"
                    "market size, validate filters, or check counts BEFORE exporting.\n\n"
                    "Returns: totalElements (total matching leads) and 1 sample profile.\n"
                    "Costs 0.5 credits per call. Same filters as search_people.\n\n"
                    "Example: 'How many VPs of Sales are at SaaS companies in New York?'\n"
                    "→ test_search_people(seniority_levels=\"vp\", departments=\"sales\",\n"
                    "    industries=\"software development\", locations=\"new york\")\n"
                ),
            ),
            "search_companies": ToolDef(
                access="read",
                handler=self.search_companies,
                description="0.5 CREDITS PER RESULT. Search 69M+ enriched company profiles. Returns company data instantly (no polling needed). Results include: name, domain, industry, location, employee count, revenue, technologies, LinkedIn URL, logo, and description.",
            ),
            "search_people": ToolDef(
                access="read",
                handler=self.search_people,
                description=(
                    "0.5 CREDITS PER RESULT. Search 400M+ people profiles. Returns up to 100 profiles per call\n"
                    "(no polling). Does NOT include email addresses — use\n"
                    "export_people_with_email to get emails.\n\n"
                    "For larger result sets, parallelize with page offsets (page=0,\n"
                    "page=1, ...). API supports 5 requests/second.\n"
                ),
            ),
            "export_people_with_email": ToolDef(
                access="write",
                handler=self.export_people_with_email,
                description=(
                    "Find people AND their verified email addresses in one step.\n\n"
                    "IMPORTANT: Always call test_search_people first to check how many\n"
                    "leads match your filters before exporting.\n\n"
                    "This is a two-step async process:\n"
                    "  1. Call this tool → returns a trackId immediately.\n"
                    "  2. Call get_export_results(track_id=trackId) to poll for results.\n\n"
                    "Max 100 results per call. Each person exported costs 1 email credit.\n"
                    "For larger exports, parallelize with page offsets (page=0, page=1,\n"
                    "...). API supports 5 requests/second.\n\n"
                    "Same filters as search_people. Use flat params (domains,\n"
                    "seniority_levels, etc.) — do NOT use filters_json unless you know\n"
                    "the exact AI Ark nested schema.\n"
                ),
            ),
            "reverse_people_lookup": ToolDef(
                access="write",
                handler=self.reverse_people_lookup,
                description="1 CREDIT. Look up a person by email or phone number. Returns full profile instantly.",
            ),
            "find_mobile_phone": ToolDef(
                access="write",
                handler=self.find_mobile_phone,
                description="1 CREDIT. Find mobile phone numbers for a person by LinkedIn URL or company domain + name.",
            ),
            "analyze_personality": ToolDef(
                access="write",
                handler=self.analyze_personality,
                description="1 CREDIT. Analyze a person's personality from their LinkedIn. Returns DISC, OCEAN, archetype, selling tips.",
            ),
            "find_emails_by_track_id": ToolDef(
                access="write",
                handler=self.find_emails_by_track_id,
                description="1 CREDIT PER PERSON. Find verified emails for people from a previous search_people trackId. Poll get_export_results for results.",
            ),
            "get_email_statistics": ToolDef(
                access="read",
                handler=self.get_email_statistics,
                description="Check email-finding progress. Returns counts only, not the actual data.",
            ),
            "get_export_results": ToolDef(
                access="read",
                handler=self.get_export_results,
                description="Get the results of an email export. Poll this after export_people_with_email or find_emails_by_track_id.",
            ),
            "list_previous_exports": ToolDef(
                access="read",
                handler=self.list_previous_exports,
                description="List all previously completed email exports stored on this server.",
            ),
            "get_credits": ToolDef(
                access="read",
                handler=self.get_credits,
                description="Check remaining AI Ark API credits.",
            ),
        }

    def _ark_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        creds = get_credentials("ai_ark")
        api_key = creds.get("api_key")
        if not api_key:
            return {"error": "Not authenticated. Please configure AI Ark API key via gateway_set_credentials."}

        url = f"{_ARK_BASE}{path}"
        headers = {
            "X-TOKEN": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        kwargs: dict[str, Any] = {"method": method, "url": url, "headers": headers, "timeout": timeout}
        if body is not None:
            kwargs["json"] = body

        try:
            resp = httpx.request(**kwargs)
        except httpx.TimeoutException:
            return {"error": f"Request timed out after {timeout}s"}
        except httpx.RequestError as exc:
            return {"error": f"Request failed: {exc}"}

        if resp.status_code >= 400:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            return {"error": f"HTTP {resp.status_code}", "details": err}

        text = resp.text
        if not text:
            return {"success": True}
        try:
            return resp.json()
        except Exception:
            return {"data": text}

    def search_companies(
        self,
        filters_json: Any = None,
        industries: Optional[str] = None,
        locations: Optional[str] = None,
        employee_size: Optional[str] = None,
        company_names: Optional[str] = None,
        domains: Optional[str] = None,
        technologies: Optional[str] = None,
        company_types: Optional[str] = None,
        founded_year_start: Optional[int] = None,
        founded_year_end: Optional[int] = None,
        revenue_start: Optional[int] = None,
        revenue_end: Optional[int] = None,
        keywords: Optional[str] = None,
        lookalike_domains: Optional[str] = None,
        page: int = 0,
        size: int = 25,
    ) -> dict:
        if filters_json:
            try:
                body = _coerce_filters(filters_json)
            except (json.JSONDecodeError, TypeError):
                return {"error": "Invalid JSON in filters_json"}
            body.setdefault("page", page)
            body.setdefault("size", min(size, 100))
            return self._ark_request("POST", "/v1/companies", body=body)

        account: dict[str, Any] = {}

        if industries:
            account["industry"] = _build_any_include([i.lower() for i in _parse_json_or_csv(industries)])
        if locations:
            account["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(locations)])
        if employee_size:
            ranges = _parse_range_pairs(employee_size)
            if ranges:
                account["employeeSize"] = {"type": "RANGE", "range": ranges}
        if company_names:
            account["name"] = _build_any_include_smart(_parse_json_or_csv(company_names))
        if domains:
            account["domain"] = _build_any_include(_parse_json_or_csv(domains))
        if technologies:
            account["technology"] = _build_any_include(_parse_json_or_csv(technologies))
        if company_types:
            account["type"] = _build_any_include(_parse_json_or_csv(company_types))
        if founded_year_start is not None or founded_year_end is not None:
            r: dict[str, int] = {}
            if founded_year_start is not None:
                r["start"] = founded_year_start
            if founded_year_end is not None:
                r["end"] = founded_year_end
            account["foundedYear"] = {"type": "RANGE", "range": r}
        if revenue_start is not None or revenue_end is not None:
            rr: dict[str, int] = {}
            if revenue_start is not None:
                rr["start"] = revenue_start
            if revenue_end is not None:
                rr["end"] = revenue_end
            account["revenue"] = {"type": "RANGE", "range": [rr]}
        if keywords:
            kw_list = _parse_json_or_csv(keywords)
            account["keyword"] = {
                "any": {
                    "include": {
                        "sources": [
                            {"mode": "SMART", "source": "NAME"},
                            {"mode": "SMART", "source": "KEYWORD"},
                            {"mode": "SMART", "source": "SEO"},
                            {"mode": "SMART", "source": "DESCRIPTION"},
                        ],
                        "content": kw_list,
                    }
                }
            }

        body: dict[str, Any] = {"page": page, "size": min(size, 100)}
        if account:
            body["account"] = account
        if lookalike_domains:
            body["lookalikeDomains"] = _parse_json_or_csv(lookalike_domains)[:5]

        return self._ark_request("POST", "/v1/companies", body=body)

    def _build_people_body(
        self,
        filters_json: Any = None,
        job_titles: Optional[str] = None,
        locations: Optional[str] = None,
        seniority_levels: Optional[str] = None,
        departments: Optional[str] = None,
        skills: Optional[str] = None,
        languages: Optional[str] = None,
        profile_keywords: Optional[str] = None,
        linkedin_urls: Optional[str] = None,
        domains: Optional[str] = None,
        company_names: Optional[str] = None,
        industries: Optional[str] = None,
        company_hq_locations: Optional[str] = None,
        employee_size: Optional[str] = None,
        company_types: Optional[str] = None,
        technologies: Optional[str] = None,
        company_keywords: Optional[str] = None,
        founded_year_start: Optional[int] = None,
        founded_year_end: Optional[int] = None,
        revenue_start: Optional[int] = None,
        revenue_end: Optional[int] = None,
        page: int = 0,
        size: int = 25,
        max_size: int = 100,
    ) -> dict:
        """Build the /v1/people request body from flat params or filters_json."""
        if filters_json:
            try:
                body = _coerce_filters(filters_json)
            except (json.JSONDecodeError, TypeError):
                return {"error": "Invalid JSON in filters_json"}
            body.setdefault("page", page)
            body.setdefault("size", min(size, max_size))
            return body

        contact: dict[str, Any] = {}
        account: dict[str, Any] = {}

        if job_titles:
            titles = _parse_json_or_csv(job_titles)
            contact["experience"] = {"current": {"title": _build_any_include_smart(titles)}}
        if locations:
            contact["location"] = _build_any_include([loc.lower() for loc in _parse_json_or_csv(locations)])
        if seniority_levels:
            contact["seniority"] = _build_any_include(_parse_json_or_csv(seniority_levels))
        if departments:
            contact["departmentAndFunction"] = _build_any_include(_parse_json_or_csv(departments))
        if skills:
            contact["skill"] = _build_any_include_smart(_parse_json_or_csv(skills))
        if languages:
            contact["language"] = _build_any_include_smart(_parse_json_or_csv(languages))
        if profile_keywords:
            kw = _parse_json_or_csv(profile_keywords)
            contact["keyword"] = {
                "any": {
                    "include": {
                        "sources": [
                            {"mode": "SMART", "source": "HEADLINE"},
                            {"mode": "SMART", "source": "SUMMARY"},
                            {"mode": "SMART", "source": "SKILL"},
                            {"mode": "SMART", "source": "WORK_HISTORY_DESCRIPTION"},
                        ],
                        "content": kw,
                    }
                }
            }
        if linkedin_urls:
            contact["linkedin"] = _build_any_include(_parse_json_or_csv(linkedin_urls))

        if domains:
            account["domain"] = _build_any_include(_parse_json_or_csv(domains))
        if company_names:
            account["name"] = _build_any_include_smart(_parse_json_or_csv(company_names))
        if industries:
            account["industry"] = _build_any_include([i.lower() for i in _parse_json_or_csv(industries)])
        if company_hq_locations:
            account["location"] = _build_any_include(
                [loc.lower() for loc in _parse_json_or_csv(company_hq_locations)]
            )
        if employee_size:
            ranges = _parse_range_pairs(employee_size)
            if ranges:
                account["employeeSize"] = {"type": "RANGE", "range": ranges}
        if company_types:
            account["type"] = _build_any_include(_parse_json_or_csv(company_types))
        if technologies:
            account["technology"] = _build_any_include(_parse_json_or_csv(technologies))
        if company_keywords:
            kw2 = _parse_json_or_csv(company_keywords)
            account["keyword"] = {
                "any": {
                    "include": {
                        "sources": [
                            {"mode": "SMART", "source": "NAME"},
                            {"mode": "SMART", "source": "KEYWORD"},
                            {"mode": "SMART", "source": "SEO"},
                            {"mode": "SMART", "source": "DESCRIPTION"},
                        ],
                        "content": kw2,
                    }
                }
            }
        if founded_year_start is not None or founded_year_end is not None:
            r: dict[str, int] = {}
            if founded_year_start is not None:
                r["start"] = founded_year_start
            if founded_year_end is not None:
                r["end"] = founded_year_end
            account["foundedYear"] = {"type": "RANGE", "range": r}
        if revenue_start is not None or revenue_end is not None:
            rr: dict[str, int] = {}
            if revenue_start is not None:
                rr["start"] = revenue_start
            if revenue_end is not None:
                rr["end"] = revenue_end
            account["revenue"] = {"type": "RANGE", "range": [rr]}

        body: dict[str, Any] = {"page": page, "size": min(size, max_size)}
        if contact:
            body["contact"] = contact
        if account:
            body["account"] = account

        return body

    def how_it_works(self) -> dict:
        return {
            "guide": (
                "AI Ark Best Practices\n"
                "=====================\n\n"
                "1. COUNT BEFORE YOU EXPORT\n"
                "   Always call test_search_people first to see how many leads match\n"
                "   your filters. It returns totalElements (the total count) and 1\n"
                "   sample profile. This is FREE — no email credits consumed.\n\n"
                "2. CREDIT COSTS\n"
                "   - search_people: 0.5 CREDITS PER RESULT (profiles only, no emails)\n"
                "   - search_companies: 0.5 CREDITS PER RESULT\n"
                "   - test_search_people: 0.5 CREDITS (returns count + 1 sample)\n"
                "   - export_people_with_email: 1 CREDIT PER PERSON exported\n"
                "   - find_emails_by_track_id: 1 CREDIT PER PERSON\n"
                "   - reverse_people_lookup: 1 CREDIT per lookup\n"
                "   - find_mobile_phone: 1 CREDIT per lookup\n"
                "   - analyze_personality: 1 CREDIT per analysis\n"
                "   Check balance with get_credits.\n\n"
                "3. SIZE LIMITS\n"
                "   All tools are capped at 100 results per call. For larger sets,\n"
                "   parallelize with page offsets: page=0, page=1, page=2, etc.\n"
                "   The API supports 5 requests/second, 300/minute, 18,000/hour.\n\n"
                "4. USE FLAT PARAMS, NOT filters_json\n"
                "   Use the named parameters (domains, seniority_levels, industries,\n"
                "   job_titles, etc.) instead of filters_json. The flat params are\n"
                "   automatically converted to the correct nested AI Ark API format.\n"
                "   Only use filters_json if you know the exact AI Ark schema.\n\n"
                "5. EXPORT WORKFLOW\n"
                "   a) test_search_people(...) → check totalElements\n"
                "   b) export_people_with_email(...) → returns trackId\n"
                "   c) get_export_results(track_id=...) → poll until results arrive\n"
                "      (typically 15-120 seconds)\n\n"
                "6. KEY FILTERS\n"
                "   People: job_titles, seniority_levels, departments, locations,\n"
                "           skills, languages, profile_keywords, linkedin_urls\n"
                "   Company: domains, company_names, industries, company_hq_locations,\n"
                "            employee_size, company_types, technologies,\n"
                "            company_keywords, founded_year_start/end,\n"
                "            revenue_start/end\n"
            )
        }

    def test_search_people(
        self,
        filters_json: Any = None,
        job_titles: Optional[str] = None,
        locations: Optional[str] = None,
        seniority_levels: Optional[str] = None,
        departments: Optional[str] = None,
        skills: Optional[str] = None,
        languages: Optional[str] = None,
        profile_keywords: Optional[str] = None,
        linkedin_urls: Optional[str] = None,
        domains: Optional[str] = None,
        company_names: Optional[str] = None,
        industries: Optional[str] = None,
        company_hq_locations: Optional[str] = None,
        employee_size: Optional[str] = None,
        company_types: Optional[str] = None,
        technologies: Optional[str] = None,
        company_keywords: Optional[str] = None,
        founded_year_start: Optional[int] = None,
        founded_year_end: Optional[int] = None,
        revenue_start: Optional[int] = None,
        revenue_end: Optional[int] = None,
    ) -> dict:
        body = self._build_people_body(
            filters_json=filters_json, job_titles=job_titles, locations=locations,
            seniority_levels=seniority_levels, departments=departments, skills=skills,
            languages=languages, profile_keywords=profile_keywords,
            linkedin_urls=linkedin_urls, domains=domains, company_names=company_names,
            industries=industries, company_hq_locations=company_hq_locations,
            employee_size=employee_size, company_types=company_types,
            technologies=technologies, company_keywords=company_keywords,
            founded_year_start=founded_year_start, founded_year_end=founded_year_end,
            revenue_start=revenue_start, revenue_end=revenue_end,
            page=0, size=1, max_size=1,
        )
        if "error" in body:
            return body

        result = self._ark_request("POST", "/v1/people", body=body)
        if "error" in result:
            return result

        total = result.get("totalElements", 0)
        data = result.get("data", [])
        sample = None
        if data:
            p = data[0]
            profile = p.get("profile", {})
            company = p.get("company", {}).get("summary", {})
            dept = p.get("department", {})
            sample = {
                "name": profile.get("full_name"),
                "title": profile.get("title"),
                "company": company.get("name"),
                "industry": company.get("industry"),
                "location": p.get("location", {}).get("short"),
                "seniority": dept.get("seniority"),
            }

        return {
            "total_matching_leads": total,
            "estimated_export_credits": total,
            "sample": sample,
            "next_step": (
                f"Found {total} matching leads. To export with verified emails, "
                f"call export_people_with_email with the same filters (max 100 per call, "
                f"use page offsets to parallelize)."
            ),
        }

    def search_people(
        self,
        filters_json: Any = None,
        job_titles: Optional[str] = None,
        locations: Optional[str] = None,
        seniority_levels: Optional[str] = None,
        departments: Optional[str] = None,
        skills: Optional[str] = None,
        languages: Optional[str] = None,
        profile_keywords: Optional[str] = None,
        linkedin_urls: Optional[str] = None,
        domains: Optional[str] = None,
        company_names: Optional[str] = None,
        industries: Optional[str] = None,
        company_hq_locations: Optional[str] = None,
        employee_size: Optional[str] = None,
        company_types: Optional[str] = None,
        technologies: Optional[str] = None,
        company_keywords: Optional[str] = None,
        founded_year_start: Optional[int] = None,
        founded_year_end: Optional[int] = None,
        revenue_start: Optional[int] = None,
        revenue_end: Optional[int] = None,
        page: int = 0,
        size: int = 25,
    ) -> dict:
        body = self._build_people_body(
            filters_json=filters_json, job_titles=job_titles, locations=locations,
            seniority_levels=seniority_levels, departments=departments, skills=skills,
            languages=languages, profile_keywords=profile_keywords,
            linkedin_urls=linkedin_urls, domains=domains, company_names=company_names,
            industries=industries, company_hq_locations=company_hq_locations,
            employee_size=employee_size, company_types=company_types,
            technologies=technologies, company_keywords=company_keywords,
            founded_year_start=founded_year_start, founded_year_end=founded_year_end,
            revenue_start=revenue_start, revenue_end=revenue_end,
            page=page, size=size, max_size=100,
        )
        if "error" in body:
            return body

        return self._ark_request("POST", "/v1/people", body=body)

    def export_people_with_email(
        self,
        filters_json: Any = None,
        job_titles: Optional[str] = None,
        locations: Optional[str] = None,
        seniority_levels: Optional[str] = None,
        departments: Optional[str] = None,
        skills: Optional[str] = None,
        languages: Optional[str] = None,
        profile_keywords: Optional[str] = None,
        linkedin_urls: Optional[str] = None,
        domains: Optional[str] = None,
        company_names: Optional[str] = None,
        industries: Optional[str] = None,
        company_hq_locations: Optional[str] = None,
        employee_size: Optional[str] = None,
        company_types: Optional[str] = None,
        technologies: Optional[str] = None,
        company_keywords: Optional[str] = None,
        founded_year_start: Optional[int] = None,
        founded_year_end: Optional[int] = None,
        revenue_start: Optional[int] = None,
        revenue_end: Optional[int] = None,
        page: int = 0,
        size: int = 25,
    ) -> dict:
        body = self._build_people_body(
            filters_json=filters_json, job_titles=job_titles, locations=locations,
            seniority_levels=seniority_levels, departments=departments, skills=skills,
            languages=languages, profile_keywords=profile_keywords,
            linkedin_urls=linkedin_urls, domains=domains, company_names=company_names,
            industries=industries, company_hq_locations=company_hq_locations,
            employee_size=employee_size, company_types=company_types,
            technologies=technologies, company_keywords=company_keywords,
            founded_year_start=founded_year_start, founded_year_end=founded_year_end,
            revenue_start=revenue_start, revenue_end=revenue_end,
            page=page, size=size, max_size=100,
        )
        if "error" in body:
            return body

        receipt_id = secrets.token_urlsafe(16)
        body["webhook"] = _webhook_url_for(receipt_id)

        result = self._ark_request("POST", "/v1/people/export", body=body)

        track_id = result.get("trackId")
        if track_id and "error" not in result:
            _save_receipt_mapping(track_id, receipt_id)
            result["_hint"] = f"Use get_export_results(track_id='{track_id}') to poll for results."

        return result

    def reverse_people_lookup(self, search: str) -> dict:
        return self._ark_request("POST", "/v1/people/reverse-lookup", body={"search": search})

    def find_mobile_phone(
        self,
        linkedin: Optional[str] = None,
        domain: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if linkedin:
            body["linkedin"] = linkedin
        if domain:
            body["domain"] = domain
        if name:
            body["name"] = name
        return self._ark_request("POST", "/v1/people/mobile-phone-finder", body=body)

    def analyze_personality(self, url: str) -> dict:
        return self._ark_request("POST", "/v1/people/analysis", body={"url": url})

    def find_emails_by_track_id(self, track_id: str) -> dict:
        receipt_id = secrets.token_urlsafe(16)
        body: dict[str, Any] = {
            "trackId": track_id,
            "webhook": _webhook_url_for(receipt_id),
        }
        result = self._ark_request("POST", "/v1/people/email-finder", body=body)

        if "error" not in result:
            result_track = result.get("trackId", track_id)
            _save_receipt_mapping(result_track, receipt_id)
            result["_hint"] = f"Use get_export_results(track_id='{result_track}') to poll for results."

        return result

    def get_email_statistics(self, track_id: str) -> dict:
        stats = self._ark_request("GET", f"/v1/people/statistics/{track_id}")
        if "error" in stats and "401" in str(stats.get("error", "")):
            receipt_id = _load_receipt_id(track_id)
            if receipt_id:
                result_path = _RESULTS_DIR / f"{receipt_id}.json"
                if result_path.exists():
                    try:
                        data = json.loads(result_path.read_text())
                        people = data.get("data", [])
                        valid = sum(
                            1 for p in people
                            for e in p.get("email", {}).get("output", [])
                            if e.get("status") == "VALID"
                        )
                        return {
                            "state": "DONE",
                            "statistics": {"total": len(people), "found": valid},
                            "_source": "webhook_cache",
                        }
                    except Exception:
                        pass
            return {
                "status": "awaiting_webhook",
                "message": "Statistics endpoint unavailable (upstream auth change). Results will arrive via webhook — poll get_export_results in 15-30s.",
                "track_id": track_id,
            }
        return stats

    def get_export_results(self, track_id: str) -> dict:
        receipt_id = _load_receipt_id(track_id)
        if receipt_id:
            result_path = _RESULTS_DIR / f"{receipt_id}.json"
            if result_path.exists():
                try:
                    return json.loads(result_path.read_text())
                except Exception as exc:
                    return {"error": f"Failed to read results: {exc}"}

        stats = self._ark_request("GET", f"/v1/people/statistics/{track_id}")

        if "error" in stats:
            if "401" in str(stats.get("error", "")):
                return {
                    "status": "awaiting_webhook",
                    "message": "Statistics endpoint unavailable (upstream auth change). Results arrive via webhook — poll this tool again in 15-30 seconds.",
                    "track_id": track_id,
                }
            return stats

        state = stats.get("state", "UNKNOWN")
        total = stats.get("statistics", {}).get("total", 0)
        found = stats.get("statistics", {}).get("found", 0)

        if state == "DONE":
            if receipt_id:
                result_path = _RESULTS_DIR / f"{receipt_id}.json"
                if result_path.exists():
                    return json.loads(result_path.read_text())
            return {
                "status": "completed_awaiting_delivery",
                "message": f"Email finding is DONE ({found}/{total} found). Results arriving shortly — try again in a few seconds.",
                "statistics": stats.get("statistics"),
            }

        return {
            "status": "processing",
            "message": f"Still processing: {found}/{total} emails found so far. State: {state}. Poll again in 10-30 seconds.",
            "statistics": stats.get("statistics"),
            "state": state,
        }

    def list_previous_exports(self) -> dict:
        mapping_path = _RESULTS_DIR / "_mappings.json"
        mappings: dict[str, str] = {}
        if mapping_path.exists():
            try:
                mappings = json.loads(mapping_path.read_text())
            except Exception:
                pass

        reverse_map = {v: k for k, v in mappings.items()}
        exports = []

        if not _RESULTS_DIR.exists():
            return {"exports": [], "total": 0}

        for f in sorted(_RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.name.startswith("_"):
                continue
            receipt_id = f.stem
            track_id = reverse_map.get(receipt_id, None)
            stat = f.stat()
            size_kb = round(stat.st_size / 1024, 1)

            summary: dict[str, Any] = {
                "receipt_id": receipt_id,
                "track_id": track_id,
                "size_kb": size_kb,
                "stored_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(stat.st_mtime)),
            }

            try:
                data = json.loads(f.read_text())
                people = data.get("data", [])
                if isinstance(people, list):
                    summary["people_count"] = len(people)
                    emails_found = 0
                    for p in people:
                        email_out = p.get("email", {}).get("output", [])
                        emails_found += sum(1 for e in email_out if e.get("status") == "VALID")
                    summary["valid_emails"] = emails_found
                    if people:
                        first = people[0]
                        summary["sample"] = {
                            "name": first.get("identifier", "?"),
                            "company": first.get("company", {}).get("summary", {}).get("name", "?"),
                        }
            except Exception:
                pass

            exports.append(summary)

        return {"exports": exports, "total": len(exports)}

    def get_credits(self) -> dict:
        return self._ark_request("GET", "/v1/payments/credits")

    async def webhook_receiver(self, request: Request):
        """Receives webhook POSTs from AI Ark and stores the payload."""
        track_id = request.path_params.get("track_id", "")
        if not track_id:
            return JSONResponse({"error": "missing track_id"}, status_code=400)

        try:
            payload = await request.json()
        except Exception:
            body = await request.body()
            payload = {"raw": body.decode(errors="replace")}

        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_path = _RESULTS_DIR / f"{track_id}.json"
        result_path.write_text(json.dumps(payload))
        print(f"[webhook] stored results for trackId={track_id} ({len(json.dumps(payload))} bytes)", flush=True)

        return JSONResponse({"received": True})

    def extra_routes(self) -> list:
        return [Route("/webhook/{track_id}", self.webhook_receiver, methods=["POST"])]
