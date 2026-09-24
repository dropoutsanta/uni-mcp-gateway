"""ContactOut plugin for the MCP Gateway.

Find emails, phone numbers, and enrich LinkedIn profiles. Covers LinkedIn
enrichment, people search, company search, decision makers, email verification,
and bulk contact info. Multi-account via {account}.api_key.

Auth: token header (not Bearer).
Base URL: https://api.contactout.com
Rate limits: People Search 60/min, Contact Checker 150/min, Others 1000/min.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_BASE = "https://api.contactout.com"
_TIMEOUT = 30


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("contactout")
    except RuntimeError:
        return []
    accounts = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "api_key" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str = "") -> dict:
    try:
        creds = get_credentials("contactout")
    except RuntimeError:
        return {"error": "No request context available."}

    selected = account
    if not selected:
        available = _list_accounts()
        if len(available) == 1:
            selected = available[0]
        elif len(available) > 1:
            return {
                "error": "Multiple ContactOut accounts configured. Specify `account`.",
                "available_accounts": available,
            }
        else:
            return {"error": "No ContactOut credentials configured for this key."}

    if selected == "default":
        api_key = creds.get("api_key", "")
    else:
        api_key = creds.get(f"{selected}.api_key", "")

    if not api_key:
        return {
            "error": f"No ContactOut credentials for account '{selected}'.",
            "available_accounts": _list_accounts(),
        }
    return {"account": selected, "api_key": api_key}


def _req(method: str, path: str, api_key: str, *,
         params: dict | None = None,
         body: dict | None = None,
         timeout: float = _TIMEOUT) -> dict:
    headers = {
        "token": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_BASE}{path}"
    try:
        resp = httpx.request(method, url, headers=headers,
                             params=params, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return {"error": f"Timed out ({timeout}s)."}
    except httpx.RequestError as exc:
        return {"error": f"Request failed: {exc}"}

    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "?")
        return {"error": f"Rate limited. Retry after {retry}s."}
    if resp.status_code >= 400:
        try:
            return {"error": f"HTTP {resp.status_code}", "details": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}", "details": resp.text}
    try:
        return resp.json()
    except Exception:
        return {"data": resp.text}


def _parse_csv(val: str) -> list[str]:
    if not val:
        return []
    return [v.strip() for v in val.split(",") if v.strip()]


class ContactOutPlugin(MCPPlugin):
    name = "contactout"

    def __init__(self):
        self.tools: dict[str, ToolDef] = {
            # -- META --
            "how_to_use_me": ToolDef(
                access="read", handler=self.how_to_use_me,
                description=(
                    "START HERE. Returns a guide explaining every ContactOut tool, "
                    "credit costs, and example workflows."
                ),
            ),
            "stats": ToolDef(
                access="read", handler=self.stats,
                description=(
                    "Get API usage stats and remaining credits.\n"
                    "Params: period (YYYY-MM, default current month). Optional: account."
                ),
            ),

            # -- ENRICHMENT --
            "enrich_linkedin": ToolDef(
                access="read", handler=self.enrich_linkedin,
                description=(
                    "Enrich a LinkedIn profile — returns emails, phones, experience, "
                    "education, skills, company info, and more.\n"
                    "Params: profile_url (required — full LinkedIn URL), "
                    "profile_only (bool, default false — if true, no contact info, "
                    "uses search credit instead of email/phone credits). "
                    "Optional: account.\n"
                    "Cost: 1 email credit if email found, 1 phone credit if phone found."
                ),
            ),
            "enrich_email": ToolDef(
                access="read", handler=self.enrich_email,
                description=(
                    "Reverse-enrich from an email address — returns LinkedIn profile, "
                    "company, experience, etc.\n"
                    "Params: email (required), include (optional — 'work_email' to "
                    "also return work email). Optional: account.\n"
                    "Cost: 1 email credit + 1 phone credit if found."
                ),
            ),
            "enrich_person": ToolDef(
                access="write", handler=self.enrich_person,
                description=(
                    "Flexible person enrichment using multiple data points. Pass any "
                    "combination of identifiers to find and enrich a profile.\n"
                    "Params: linkedin_url, email, phone, full_name, first_name, "
                    "last_name, company (comma-sep), company_domain (comma-sep), "
                    "job_title, location, education (comma-sep), "
                    "include (comma-sep: work_email,personal_email,phone).\n"
                    "Optional: account.\n"
                    "Cost: 1 search credit + 1 email/phone credit per type found."
                ),
            ),

            # -- CONTACT INFO --
            "contact_info": ToolDef(
                access="read", handler=self.contact_info,
                description=(
                    "Get contact details (emails + phones) for a LinkedIn profile.\n"
                    "Params: profile_url (required), include_phone (bool, default "
                    "true), email_type (personal|work|personal,work|none, default "
                    "returns both). Optional: account.\n"
                    "Cost: 1 email credit if found, 1 phone credit if found."
                ),
            ),
            "contact_info_bulk": ToolDef(
                access="write", handler=self.contact_info_bulk,
                description=(
                    "Bulk contact info for up to 1000 LinkedIn profiles (async). "
                    "Returns a job_id — poll with contact_info_bulk_results.\n"
                    "Params: profiles_json (required — JSON array of LinkedIn URLs, "
                    "max 1000), include_phone (bool, default true). Optional: account.\n"
                    "Cost: 1 email credit per profile found, 1 phone if include_phone."
                ),
            ),
            "contact_info_bulk_results": ToolDef(
                access="read", handler=self.contact_info_bulk_results,
                description=(
                    "Poll results for a bulk contact info job.\n"
                    "Params: job_id (required). Optional: account.\n"
                    "Returns status (QUEUED/PROCESSING/SENT/DONE) and results when ready."
                ),
            ),

            # -- SEARCH --
            "search_people": ToolDef(
                access="read", handler=self.search_people,
                description=(
                    "Search for people matching criteria. Returns 25 profiles per page.\n"
                    "Params: page (default 1), name, job_title (comma-sep), "
                    "seniority (comma-sep), job_function (comma-sep), skills (comma-sep), "
                    "company (comma-sep), domain (comma-sep), industry (comma-sep), "
                    "location (comma-sep), company_size (comma-sep, e.g. '1_10,11_50'), "
                    "keyword, years_of_experience (comma-sep, e.g. '6_10'), "
                    "match_experience (current|past|both), "
                    "data_types (comma-sep: personal_email,work_email,phone), "
                    "reveal_info (bool — if true, returns emails/phones, costs credits), "
                    "search_json (raw JSON body for advanced filters). Optional: account.\n"
                    "Cost: 1 search credit per profile. +1 email/phone if reveal_info=true."
                ),
            ),
            "count_people": ToolDef(
                access="read", handler=self.count_people,
                description=(
                    "Count people matching search criteria (FREE — no credits).\n"
                    "Same filters as search_people except page/reveal_info/data_types. "
                    "Returns total_results count only. Use before search to gauge volume.\n"
                    "Params: same as search_people. Optional: account."
                ),
            ),
            "decision_makers": ToolDef(
                access="read", handler=self.decision_makers,
                description=(
                    "Find key decision makers at a company.\n"
                    "Params: At least one of: linkedin_url (company LinkedIn URL), "
                    "domain, name. Also: page (default 1), reveal_info (bool). "
                    "Optional: account.\n"
                    "Cost: 1 search credit per profile. +1 email/phone if reveal_info."
                ),
            ),

            # -- COMPANY --
            "search_companies": ToolDef(
                access="read", handler=self.search_companies,
                description=(
                    "Search for companies matching criteria.\n"
                    "Params: page (default 1), name (comma-sep), domain (comma-sep), "
                    "size (comma-sep, e.g. '1_10,51_200'), location (comma-sep), "
                    "industry (comma-sep), min_revenue, max_revenue, "
                    "year_founded_from, year_founded_to, linkedin_url (comma-sep). "
                    "Optional: account.\n"
                    "Cost: 1 search credit per company returned."
                ),
            ),
            "enrich_domains": ToolDef(
                access="read", handler=self.enrich_domains,
                description=(
                    "Get company info from domain names (max 30).\n"
                    "Params: domains (required — comma-sep domain names). Optional: account.\n"
                    "Cost: 1 search credit per company found."
                ),
            ),

            # -- REVERSE LOOKUP --
            "email_to_linkedin": ToolDef(
                access="read", handler=self.email_to_linkedin,
                description=(
                    "Get LinkedIn profile URL from an email address.\n"
                    "Params: email (required). Optional: account.\n"
                    "Cost: 1 email credit if found."
                ),
            ),

            # -- CHECKERS (FREE) --
            "check_personal_email": ToolDef(
                access="read", handler=self.check_personal_email,
                description=(
                    "Check if a personal email exists for a LinkedIn profile (FREE).\n"
                    "Params: profile_url (required). Optional: account."
                ),
            ),
            "check_work_email": ToolDef(
                access="read", handler=self.check_work_email,
                description=(
                    "Check if a work email exists for a LinkedIn profile (FREE).\n"
                    "Params: profile_url (required). Optional: account."
                ),
            ),
            "check_phone": ToolDef(
                access="read", handler=self.check_phone,
                description=(
                    "Check if a phone number exists for a LinkedIn profile (FREE).\n"
                    "Params: profile_url (required). Optional: account."
                ),
            ),

            # -- EMAIL VERIFICATION --
            "verify_email": ToolDef(
                access="read", handler=self.verify_email,
                description=(
                    "Verify deliverability of a single email address.\n"
                    "Params: email (required). Optional: account.\n"
                    "Returns: valid, invalid, accept_all, disposable, or unknown.\n"
                    "Cost: 1 verifier credit if valid/invalid/accept_all."
                ),
            ),
            "verify_emails_bulk": ToolDef(
                access="write", handler=self.verify_emails_bulk,
                description=(
                    "Bulk verify up to 1000 emails (async). Returns job_id.\n"
                    "Params: emails_json (required — JSON array of emails, max 1000). "
                    "Optional: account.\n"
                    "Poll with verify_emails_bulk_results."
                ),
            ),
            "verify_emails_bulk_results": ToolDef(
                access="read", handler=self.verify_emails_bulk_results,
                description=(
                    "Poll results for a bulk email verification job.\n"
                    "Params: job_id (required). Optional: account."
                ),
            ),
        }

    # =================================================================
    # META
    # =================================================================

    def how_to_use_me(self, **kwargs) -> Any:
        return {
            "overview": (
                "ContactOut finds emails, phone numbers, and enriches profiles "
                "from LinkedIn URLs, email addresses, or search criteria. 800M+ "
                "profiles, 350M+ emails, 100M+ direct dials."
            ),
            "credit_costs": {
                "FREE (no credits)": [
                    "count_people — count matching profiles before searching",
                    "check_personal_email / check_work_email / check_phone — "
                    "check if contact data exists before revealing",
                ],
                "1 search credit": [
                    "search_people — per profile returned",
                    "decision_makers — per profile returned",
                    "search_companies — per company returned",
                    "enrich_domains — per company found",
                    "enrich_person — per match (if no email/phone requested)",
                ],
                "1 email credit": [
                    "enrich_linkedin — if email found",
                    "contact_info — if email found",
                    "contact_info_bulk — per profile with email",
                    "search_people (reveal_info=true) — per profile with email",
                    "email_to_linkedin — if match found",
                ],
                "1 phone credit": [
                    "enrich_linkedin — if phone found",
                    "contact_info (include_phone=true) — if phone found",
                    "contact_info_bulk (include_phone=true) — per phone",
                ],
                "1 verifier credit": [
                    "verify_email — if valid/invalid/accept_all",
                    "verify_emails_bulk — per email verified",
                ],
            },
            "workflows": {
                "Find emails for a list of LinkedIn URLs": [
                    "1. contact_info_bulk(profiles_json='[\"https://linkedin.com/in/...\", ...]')",
                    "2. contact_info_bulk_results(job_id='...') — poll until DONE",
                ],
                "Search for people at a company and get emails": [
                    "1. count_people(company='Stripe', job_title='VP,Director') — check volume (FREE)",
                    "2. search_people(company='Stripe', job_title='VP,Director', reveal_info=true) — get contacts",
                ],
                "Find decision makers with contact info": [
                    "1. decision_makers(domain='stripe.com', reveal_info=true)",
                ],
                "Check before you pay": [
                    "1. check_personal_email(profile_url='...') — FREE check",
                    "2. If true → contact_info(profile_url='...') — pay only when data exists",
                ],
                "Enrich from AI Ark / HeyReach LinkedIn URLs": [
                    "When you have LinkedIn URLs from ai_ark_search_people or heyreach_leads_list:",
                    "1. Batch the URLs into groups of 1000",
                    "2. contact_info_bulk(profiles_json='[...]', include_phone=true)",
                    "3. Poll contact_info_bulk_results until DONE",
                    "4. Merge emails/phones back into your lead data",
                ],
            },
            "tips": [
                "ALWAYS use count_people before search_people to check volume.",
                "Use check_* endpoints (FREE) before contact_info to avoid wasting credits.",
                "contact_info_bulk v2 supports up to 1000 profiles — much cheaper than single calls.",
                "search_people returns 25 per page. Use page param to paginate.",
                "verify_email before sending — saves deliverability reputation.",
                "Rate limits: People Search 60/min, Contact Checker 150/min, Others 1000/min.",
            ],
        }

    def stats(self, period: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        params: dict[str, Any] = {}
        if period:
            params["period"] = period
        return _req("GET", "/v1/stats", r["api_key"], params=params)

    # =================================================================
    # ENRICHMENT
    # =================================================================

    def enrich_linkedin(self, profile_url: str = "", profile_only: bool = False,
                        account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        params: dict[str, Any] = {"profile": profile_url}
        if profile_only:
            params["profile_only"] = "true"
        return _req("GET", "/v1/linkedin/enrich", r["api_key"], params=params)

    def enrich_email(self, email: str = "", include: str = "",
                     account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not email:
            return {"error": "email is required"}
        params: dict[str, Any] = {"email": email}
        if include:
            params["include"] = include
        return _req("GET", "/v1/email/enrich", r["api_key"], params=params)

    def enrich_person(self, linkedin_url: str = "", email: str = "",
                      phone: str = "", full_name: str = "",
                      first_name: str = "", last_name: str = "",
                      company: str = "", company_domain: str = "",
                      job_title: str = "", location: str = "",
                      education: str = "", include: str = "",
                      account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {}
        if linkedin_url:
            body["linkedin_url"] = linkedin_url
        if email:
            body["email"] = email
        if phone:
            body["phone"] = phone
        if full_name:
            body["full_name"] = full_name
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name
        if company:
            body["company"] = _parse_csv(company)
        if company_domain:
            body["company_domain"] = _parse_csv(company_domain)
        if job_title:
            body["job_title"] = job_title
        if location:
            body["location"] = location
        if education:
            body["education"] = _parse_csv(education)
        if include:
            body["include"] = _parse_csv(include)
        if not body:
            return {"error": "At least one identifier is required (linkedin_url, email, phone, full_name, etc.)"}
        return _req("POST", "/v1/people/enrich", r["api_key"], body=body)

    # =================================================================
    # CONTACT INFO
    # =================================================================

    def contact_info(self, profile_url: str = "", include_phone: bool = True,
                     email_type: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        params: dict[str, Any] = {
            "profile": profile_url,
            "include_phone": "true" if include_phone else "false",
        }
        if email_type:
            params["email_type"] = email_type
        return _req("GET", "/v1/people/linkedin", r["api_key"], params=params)

    def contact_info_bulk(self, profiles_json: str = "",
                          include_phone: bool = True,
                          account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profiles_json:
            return {"error": "profiles_json is required (JSON array of LinkedIn URLs)"}
        try:
            profiles = json.loads(profiles_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid profiles_json: {e}"}
        if not isinstance(profiles, list) or len(profiles) > 1000:
            return {"error": "profiles_json must be a JSON array of max 1000 URLs"}
        body: dict[str, Any] = {"profiles": profiles}
        if include_phone:
            body["include_phone"] = True
        return _req("POST", "/v2/people/linkedin/batch", r["api_key"], body=body)

    def contact_info_bulk_results(self, job_id: str = "",
                                  account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not job_id:
            return {"error": "job_id is required"}
        return _req("GET", f"/v2/people/linkedin/batch/{job_id}", r["api_key"])

    # =================================================================
    # SEARCH
    # =================================================================

    def search_people(self, page: int = 1, name: str = "",
                      job_title: str = "", seniority: str = "",
                      job_function: str = "", skills: str = "",
                      company: str = "", domain: str = "",
                      industry: str = "", location: str = "",
                      company_size: str = "", keyword: str = "",
                      years_of_experience: str = "",
                      match_experience: str = "",
                      data_types: str = "", reveal_info: bool = False,
                      search_json: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r

        if search_json:
            try:
                body = json.loads(search_json)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid search_json: {e}"}
        else:
            body: dict[str, Any] = {"page": int(page)}
            if name:
                body["name"] = name
            if job_title:
                body["job_title"] = _parse_csv(job_title)
            if seniority:
                body["seniority"] = _parse_csv(seniority)
            if job_function:
                body["job_function"] = _parse_csv(job_function)
            if skills:
                body["skills"] = _parse_csv(skills)
            if company:
                body["company"] = _parse_csv(company)
            if domain:
                body["domain"] = _parse_csv(domain)
            if industry:
                body["industry"] = _parse_csv(industry)
            if location:
                body["location"] = _parse_csv(location)
            if company_size:
                body["company_size"] = _parse_csv(company_size)
            if keyword:
                body["keyword"] = keyword
            if years_of_experience:
                body["years_of_experience"] = _parse_csv(years_of_experience)
            if match_experience:
                body["match_experience"] = match_experience
            if data_types:
                body["data_types"] = _parse_csv(data_types)
            if reveal_info:
                body["reveal_info"] = True

        return _req("POST", "/v1/people/search", r["api_key"], body=body)

    def count_people(self, name: str = "", job_title: str = "",
                     seniority: str = "", job_function: str = "",
                     skills: str = "", company: str = "",
                     domain: str = "", industry: str = "",
                     location: str = "", company_size: str = "",
                     keyword: str = "", years_of_experience: str = "",
                     match_experience: str = "",
                     account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if job_title:
            body["job_title"] = _parse_csv(job_title)
        if seniority:
            body["seniority"] = _parse_csv(seniority)
        if job_function:
            body["job_function"] = _parse_csv(job_function)
        if skills:
            body["skills"] = _parse_csv(skills)
        if company:
            body["company"] = _parse_csv(company)
        if domain:
            body["domain"] = _parse_csv(domain)
        if industry:
            body["industry"] = _parse_csv(industry)
        if location:
            body["location"] = _parse_csv(location)
        if company_size:
            body["company_size"] = _parse_csv(company_size)
        if keyword:
            body["keyword"] = keyword
        if years_of_experience:
            body["years_of_experience"] = _parse_csv(years_of_experience)
        if match_experience:
            body["match_experience"] = match_experience
        return _req("POST", "/v1/people/count", r["api_key"], body=body)

    def decision_makers(self, linkedin_url: str = "", domain: str = "",
                        name: str = "", page: int = 1,
                        reveal_info: bool = False,
                        account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not linkedin_url and not domain and not name:
            return {"error": "At least one of linkedin_url, domain, or name is required"}
        params: dict[str, Any] = {}
        if linkedin_url:
            params["linkedin_url"] = linkedin_url
        if domain:
            params["domain"] = domain
        if name:
            params["name"] = name
        if page > 1:
            params["page"] = page
        if reveal_info:
            params["reveal_info"] = "true"
        return _req("GET", "/v1/people/decision-makers", r["api_key"], params=params)

    # =================================================================
    # COMPANY
    # =================================================================

    def search_companies(self, page: int = 1, name: str = "",
                         domain: str = "", size: str = "",
                         location: str = "", industry: str = "",
                         min_revenue: int = 0, max_revenue: int = 0,
                         year_founded_from: int = 0,
                         year_founded_to: int = 0,
                         linkedin_url: str = "",
                         account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        body: dict[str, Any] = {"page": int(page)}
        if name:
            body["name"] = _parse_csv(name)
        if domain:
            body["domain"] = _parse_csv(domain)
        if size:
            body["size"] = _parse_csv(size)
        if location:
            body["location"] = _parse_csv(location)
        if industry:
            body["industries"] = _parse_csv(industry)
        if min_revenue:
            body["min_revenue"] = int(min_revenue)
        if max_revenue:
            body["max_revenue"] = int(max_revenue)
        if year_founded_from:
            body["year_founded_from"] = int(year_founded_from)
        if year_founded_to:
            body["year_founded_to"] = int(year_founded_to)
        if linkedin_url:
            body["linkedin_url"] = _parse_csv(linkedin_url)
        return _req("POST", "/v1/company/search", r["api_key"], body=body)

    def enrich_domains(self, domains: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not domains:
            return {"error": "domains is required (comma-separated)"}
        domain_list = _parse_csv(domains)
        if len(domain_list) > 30:
            return {"error": f"Max 30 domains per call, got {len(domain_list)}"}
        return _req("POST", "/v1/domain/enrich", r["api_key"],
                     body={"domains": domain_list})

    # =================================================================
    # REVERSE LOOKUP
    # =================================================================

    def email_to_linkedin(self, email: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not email:
            return {"error": "email is required"}
        return _req("GET", "/v1/people/person", r["api_key"],
                     params={"email": email})

    # =================================================================
    # CHECKERS (FREE)
    # =================================================================

    def check_personal_email(self, profile_url: str = "",
                             account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        return _req("GET", "/v1/people/linkedin/personal_email_status",
                     r["api_key"], params={"profile": profile_url})

    def check_work_email(self, profile_url: str = "",
                         account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        return _req("GET", "/v1/people/linkedin/work_email_status",
                     r["api_key"], params={"profile": profile_url})

    def check_phone(self, profile_url: str = "",
                    account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not profile_url:
            return {"error": "profile_url is required"}
        return _req("GET", "/v1/people/linkedin/phone_status",
                     r["api_key"], params={"profile": profile_url})

    # =================================================================
    # EMAIL VERIFICATION
    # =================================================================

    def verify_email(self, email: str = "", account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not email:
            return {"error": "email is required"}
        return _req("GET", "/v1/email/verify", r["api_key"],
                     params={"email": email})

    def verify_emails_bulk(self, emails_json: str = "",
                           account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not emails_json:
            return {"error": "emails_json is required (JSON array of emails)"}
        try:
            emails = json.loads(emails_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid emails_json: {e}"}
        if not isinstance(emails, list) or len(emails) > 1000:
            return {"error": "emails_json must be a JSON array of max 1000 emails"}
        return _req("POST", "/v1/email/verify/batch", r["api_key"],
                     body={"emails": emails})

    def verify_emails_bulk_results(self, job_id: str = "",
                                   account: str = "") -> dict:
        r = _resolve(account)
        if "error" in r:
            return r
        if not job_id:
            return {"error": "job_id is required"}
        return _req("GET", f"/v1/email/verify/batch/{job_id}", r["api_key"])

    # =================================================================
    # HEALTH CHECK
    # =================================================================

    def health_check(self) -> dict[str, Any]:
        try:
            r = _resolve()
            if "error" in r:
                return {"status": "no_credentials", "detail": r["error"]}
            resp = httpx.get(
                f"{_BASE}/v1/stats",
                headers={"token": r["api_key"]},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return {"status": "ok"}
            return {"status": "error", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
