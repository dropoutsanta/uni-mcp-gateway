"""SmartScout Amazon analytics plugin for the MCP Gateway.

Search brands, products, subcategories, and search terms on Amazon via
SmartScout's internal API.  Auth uses email/password login with JWT auto-refresh.
Multi-account via {account}.user / {account}.password credentials.

Required credentials per account:
  user     – SmartScout login email
  password – SmartScout login password
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_API = "https://smartscoutapi-east.azurewebsites.net"
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_JWT_LIFETIME = 3500


def _list_accounts() -> list[str]:
    try:
        creds = get_credentials("smartscout")
    except RuntimeError:
        return []
    accounts: set[str] = set()
    for k in creds:
        if "." in k:
            accounts.add(k.split(".")[0])
    if "user" in creds:
        accounts.add("default")
    return sorted(accounts)


def _resolve(account: str) -> dict[str, Any]:
    try:
        creds = get_credentials("smartscout")
    except RuntimeError:
        return {"error": "No SmartScout credentials configured."}
    selected = account
    if not selected:
        avail = _list_accounts()
        if len(avail) == 1:
            selected = avail[0]
        elif len(avail) > 1:
            return {"error": "Multiple accounts. Specify `account`.", "available": avail}
        else:
            return {"error": "No SmartScout credentials found."}
    prefix = "" if selected == "default" else f"{selected}."
    user = creds.get(f"{prefix}user", "")
    pw = creds.get(f"{prefix}password", "")
    if not user or not pw:
        return {"error": f"Missing credentials for '{selected}'."}
    return {"account": selected, "user": user, "password": pw}


def _jwt(account: str, user: str, pw: str) -> str:
    cached = _TOKEN_CACHE.get(account)
    if cached and cached[1] > time.time():
        return cached[0]
    r = httpx.post(f"{_API}/api/authentication/login",
                   json={"userName": user, "password": pw}, timeout=15)
    r.raise_for_status()
    token = r.json()["token"]
    _TOKEN_CACHE[account] = (token, time.time() + _JWT_LIFETIME)
    return token


def _hdrs(token: str, mp: str = "US") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-SmartScout-Marketplace": mp,
        "Content-Type": "application/json-patch+json",
        "Accept": "text/plain",
    }


def _get(token: str, path: str, mp: str = "US", **params: Any) -> Any:
    r = httpx.get(f"{_API}{path}", headers=_hdrs(token, mp),
                  params=params or None, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {}


def _post(token: str, path: str, body: dict, mp: str = "US") -> Any:
    r = httpx.post(f"{_API}{path}", headers=_hdrs(token, mp),
                   json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {}


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        return {"_error": str(e)}


class SmartScoutPlugin(MCPPlugin):
    name = "smartscout"

    def __init__(self):  # noqa: C901
        self.tools = {
            # ── Guide ──
            "how_to_use_me": ToolDef(
                access="read", handler=self.how_to_use_me,
                description=(
                    "START HERE. Returns a guide explaining every SmartScout tool, "
                    "when to use each one, and step-by-step playbooks for common "
                    "workflows (prospect research, competitor analysis, category "
                    "exploration). Call this first if you're unsure which tool to pick. "
                    "No params required."
                ),
            ),

            # ── Step 1: Find a brand ──
            "search_brands": ToolDef(
                access="read", handler=self.search_brands,
                description=(
                    "FIND BRANDS — two modes:\n\n"
                    "MODE 1 (by name): Pass 'name' to look up a specific brand. Returns "
                    "enriched brand details (ID, revenue, products, score). Use this "
                    "when you have a company/brand name and need the brand_id.\n\n"
                    "MODE 2 (browse/filter): Omit 'name' and filter by category + "
                    "revenue to discover brands. Works with parent categories too — "
                    "e.g. 'Sports & Outdoors' searches ALL child subcategories "
                    "(Camping, Fishing, Fitness, etc.) in one call. Use this to find "
                    "brands in a market segment.\n\n"
                    "Params: name (optional — partial match), subcategory_id (filter "
                    "by category — works with parent or child IDs, get from "
                    "subcategories or search_subcategories), min_revenue (monthly $, "
                    "e.g. 1000000 for $1M/mo), max_revenue, count_only (bool — fast "
                    "approximate count + preview, no per-brand enrichment), "
                    "scan_pages (1-50, default 20 — how many pages of 100 products "
                    "to scan; increase for more coverage), start_page (default 0 — "
                    "continue scanning from a previous call's coverage.continue_with), "
                    "sort_by (default monthlyRevenue), sort_dir, limit (1-200), "
                    "offset, marketplace (default US).\n\n"
                    "Returns a 'coverage' object showing products_scanned, "
                    "unique_brands_found, has_more, and continue_with instructions. "
                    "If has_more=true and you need more brands, call again with the "
                    "suggested start_page.\n\n"
                    "IMPORTANT: When user says '$X/year', divide by 12 for monthly. "
                    "E.g. '$5M/year' = min_revenue=416667."
                ),
            ),

            # ── Step 2: Deep-dive on a brand ──
            "get_brand": ToolDef(
                access="read", handler=self.get_brand,
                description=(
                    "PROFILE — full brand snapshot once you have the brand_id. Returns "
                    "~30 fields: monthly revenue, units, MoM/YoY growth, TTM, brand "
                    "score, total products, avg price, review count/rating, ad spend, "
                    "Amazon vs seller revenue split, storefront URL, sponsored brand "
                    "win rate, top-spot win rate, ranking products/search terms count. "
                    "Use this for a single-call overview. For trends over time, use "
                    "brand_history instead.\n\n"
                    "Params: brand_id (required), marketplace (default US)."
                ),
            ),
            "brand_history": ToolDef(
                access="read", handler=self.brand_history,
                description=(
                    "TRENDS — weekly time-series of a brand's revenue, units sold, and "
                    "avg Buy Box price going back months/years. Use this to see growth "
                    "trajectory, seasonality, or revenue declines. Returns one row per "
                    "week. Do NOT use this for category-level breakdown — use "
                    "brand_history_by_category for that.\n\n"
                    "Params: brand_id (required), start_date (ISO, default 2024-01-01), "
                    "supercharged (default true), marketplace (default US)."
                ),
            ),
            "brand_history_by_category": ToolDef(
                access="read", handler=self.brand_history_by_category,
                description=(
                    "CATEGORY MIX — how a brand's revenue splits across Amazon "
                    "subcategories over time. Use this to understand which product "
                    "categories drive a brand's revenue and whether they're expanding "
                    "into new categories. Different from brand_history which shows "
                    "total revenue; this breaks it down by subcategory.\n\n"
                    "Params: brand_id (required), start_date, end_date, "
                    "limit (default 10 categories), marketplace (default US)."
                ),
            ),
            "brand_coverage": ToolDef(
                access="read", handler=self.brand_coverage,
                description=(
                    "DISTRIBUTION — which 3P sellers carry this brand and how much "
                    "revenue each generates. Shows seller name, monthly revenue, "
                    "estimated brand %, number of offers, and month-over-month coverage "
                    "change. Use this to understand a brand's seller/distribution "
                    "landscape. Different from product_sellers which shows Buy Box "
                    "competition on a single ASIN.\n\n"
                    "Params: brand_id (required), marketplace (default US)."
                ),
            ),
            "brand_tailored_report": ToolDef(
                access="read", handler=self.brand_tailored_report,
                description=(
                    "REPORT STATUS — checks if SmartScout has a pre-generated tailored "
                    "report ready for this brand (total ASINs tracked, stale ASINs). "
                    "Rarely needed directly — brand_report already includes this. Only "
                    "call this if you specifically need to check report freshness.\n\n"
                    "Params: brand_id (required), marketplace (default US)."
                ),
            ),

            # ── Products ──
            "search_products": ToolDef(
                access="read", handler=self.search_products,
                description=(
                    "FIND PRODUCTS — search/list Amazon products filtered by brand, "
                    "category, ASIN, or title keyword. Returns a table of products with: "
                    "ASIN, title, rank, monthly revenue, units sold, MoM growth, Buy Box "
                    "price, number of sellers. Use this to find a brand's top products "
                    "or look up a specific ASIN. The product 'id' in the response is "
                    "needed for product_sellers, product_history, and "
                    "product_organic_ranks.\n\n"
                    "Params: brand_id / category_id / subcategory_id / asin / title "
                    "(at least one filter recommended), sort_by (default "
                    "monthlyRevenueEstimate), sort_dir, limit (1-100), offset, "
                    "marketplace (default US)."
                ),
            ),
            "product_sellers": ToolDef(
                access="read", handler=self.product_sellers,
                description=(
                    "BUY BOX — who sells a specific product and who wins the Buy Box. "
                    "Returns each seller's name, price, Buy Box win %, FBA status, and "
                    "estimated monthly revenue on that listing. Use this to analyze "
                    "Buy Box competition on a single ASIN. Different from brand_coverage "
                    "which shows sellers across an entire brand.\n\n"
                    "Params: product_id (required — get this from search_products), "
                    "marketplace (default US)."
                ),
            ),
            "product_history": ToolDef(
                access="read", handler=self.product_history,
                description=(
                    "PRODUCT TRENDS — weekly time-series of Buy Box price, revenue, "
                    "units, and sales rank for one or more products. Use this to see "
                    "how a product's performance changed over time (price wars, demand "
                    "spikes, ranking drops). Can batch up to ~10 product IDs in one "
                    "call.\n\n"
                    "Params: product_ids (required — list of numeric IDs from "
                    "search_products), start_date (ISO, default 2025-01-01), "
                    "marketplace (default US)."
                ),
            ),
            "product_organic_ranks": ToolDef(
                access="read", handler=self.product_organic_ranks,
                description=(
                    "SEO SNAPSHOT — what search terms a product currently ranks for "
                    "organically and at what position. Returns term + rank + search "
                    "volume. Use this to understand a product's organic search "
                    "visibility right now. For how ranks changed over time, use "
                    "product_rank_history instead.\n\n"
                    "Params: product_id (required), exclude_ads (default true), "
                    "exclude_rank_history (default true), marketplace (default US)."
                ),
            ),
            "product_rank_history": ToolDef(
                access="read", handler=self.product_rank_history,
                description=(
                    "RANK CHANGES — daily history of how a product's organic rank "
                    "shifted across its search terms. Use this to spot rank gains/losses "
                    "over time (e.g. 'did they lose page 1 on their main keyword?'). "
                    "Different from product_organic_ranks which is a current snapshot.\n\n"
                    "Params: product_id (required), marketplace (default US)."
                ),
            ),
            "product_relevancy": ToolDef(
                access="read", handler=self.product_relevancy,
                description=(
                    "COMPETITORS — find products that directly compete with a given "
                    "ASIN based on Amazon's relevancy algorithm. Returns competing "
                    "products with their revenue, rank, brand, and a relevancy score. "
                    "Use this to map out the competitive landscape for a specific "
                    "product listing.\n\n"
                    "Params: asin (required — the ASIN string like 'B09XYZ123'), "
                    "competitors_only (default true), marketplace (default US)."
                ),
            ),

            # ── Search terms / advertising ──
            "search_terms_for_product": ToolDef(
                access="read", handler=self.search_terms_for_product,
                description=(
                    "AD INTELLIGENCE — sponsored ad win rates for every search term a "
                    "product appears on. Shows top-spot win rate, top-group win rate, "
                    "sponsored brand win rate, and sponsored video win rate per term. "
                    "Use this to understand a product's advertising strategy and PPC "
                    "performance. Different from product_organic_ranks which shows "
                    "organic rank position (not ad data).\n\n"
                    "Params: product_id (required), sort_by, sort_dir, limit "
                    "(default 100), marketplace (default US)."
                ),
            ),

            # ── Category / market exploration ──
            "subcategories": ToolDef(
                access="read", handler=self.subcategories,
                description=(
                    "CATEGORY TREE — list the top-level Amazon categories (Health & "
                    "Household, Electronics, Home & Kitchen, etc.) with their IDs. Use "
                    "this as the starting point to explore categories. Returns just "
                    "names + IDs. For rich category stats (revenue, brand count, growth), "
                    "use search_subcategories instead.\n\n"
                    "Params: marketplace (default US)."
                ),
            ),
            "search_subcategories": ToolDef(
                access="read", handler=self.search_subcategories,
                description=(
                    "MARKET SIZING — browse Amazon subcategories ranked by total "
                    "revenue with rich stats: total monthly revenue, brand count, ASIN "
                    "count, units sold, avg price, avg reviews/rating, MoM/YoY growth, "
                    "China seller revenue %, Amazon vs 3P split, TTM. Use this to "
                    "identify large/growing market opportunities or size a prospect's "
                    "category.\n\n"
                    "Params: sort_by (default totalMonthlyRevenue), sort_dir, "
                    "limit (1-100), offset, marketplace (default US)."
                ),
            ),
            "subcategory_brands": ToolDef(
                access="read", handler=self.subcategory_brands,
                description=(
                    "CATEGORY LEADERS — top brands within a specific subcategory with "
                    "their revenue over time. Use this to see who dominates a category "
                    "and how market share is shifting. You need a subcategory_id from "
                    "subcategories or search_subcategories.\n\n"
                    "Params: subcategory_id (required), marketplace (default US)."
                ),
            ),
            "subcategory_history": ToolDef(
                access="read", handler=self.subcategory_history,
                description=(
                    "CATEGORY TRENDS — revenue/units time-series for an entire "
                    "subcategory. Use this to see if a market is growing, shrinking, or "
                    "seasonal. Different from subcategory_brands which breaks down by "
                    "brand; this shows the total category trajectory.\n\n"
                    "Params: subcategory_id (required), marketplace (default US)."
                ),
            ),

            # ── All-in-one ──
            "brand_report": ToolDef(
                access="read", handler=self.brand_report,
                description=(
                    "FULL DOSSIER — comprehensive brand intelligence report in a single "
                    "call. Internally fetches and compresses 8+ data sources: brand "
                    "profile, revenue history, category breakdown, top products with "
                    "Buy Box sellers, organic ranks, ad win rates, seller distribution, "
                    "competitors, and subcategory landscape. Returns everything under "
                    "~10K tokens.\n\n"
                    "USE THIS when you need a complete picture of a brand for prospect "
                    "research, competitive analysis, or pitch prep. Do NOT call "
                    "individual tools (get_brand, brand_history, search_products, etc.) "
                    "separately if brand_report covers your needs — it's faster and "
                    "cheaper as one call.\n\n"
                    "Params: brand_id (required — find it with search_brands first), "
                    "top_n_products (default 5, max 10), marketplace (default US)."
                ),
            ),
        }

    def _auth(self, args: dict) -> tuple[str, str] | dict:
        acct = _resolve(args.get("account", ""))
        if "error" in acct:
            return acct
        token = _jwt(acct["account"], acct["user"], acct["password"])
        mp = args.get("marketplace", "US")
        return token, mp

    # ── Guide ──

    def how_to_use_me(self, **kwargs) -> Any:
        return {
            "overview": (
                "SmartScout gives you Amazon marketplace intelligence — brand revenue, "
                "product performance, Buy Box data, search term rankings, ad win rates, "
                "and category analytics. All data is estimated by SmartScout from public "
                "Amazon data. This is the playbook your agency team uses to research "
                "Amazon brand prospects before outreach."
            ),
            "primary_use_cases": [
                {
                    "name": "Research a specific prospect (by name or website)",
                    "steps": [
                        "STEP 1: FIND THE BRAND — search_brands(name='CompanyName'). "
                        "If no results, the company likely sells under a DIFFERENT brand "
                        "name on Amazon. Many companies use product brand names that differ "
                        "from their corporate name (e.g. 'Black Diamond Coatings' sells as "
                        "'Dominator', 'Church & Dwight' sells as 'OxiClean'). When this "
                        "happens, visit their website to find their actual product brand "
                        "names, then search for those instead. Try multiple brand names if "
                        "the website shows several product lines.",

                        "STEP 2: PULL THE DOSSIER — brand_report(brand_id=X, top_n_products=7). "
                        "This single call fetches everything: brand profile, revenue history, "
                        "category breakdown, top products with Buy Box sellers, organic "
                        "ranks, ad win rates, seller coverage, competitors, and subcategory "
                        "landscape. It's compressed to <10K tokens.",

                        "STEP 3: FORMAT THE REPORT — present the data in the exact format "
                        "shown in the 'report_template' section below. Do not dump raw JSON. "
                        "Organize it into clear sections with tables and bullet points. "
                        "Always end with 'Key Takeaways' that highlight actionable insights "
                        "for a sales conversation.",
                    ],
                },
                {
                    "name": "Find brands in a category by revenue (prospecting lists)",
                    "when": (
                        "User asks things like 'find outdoor equipment brands over $10M', "
                        "'who are the biggest brands in Home & Kitchen', 'brands doing "
                        "over $5M/mo in pet supplies', etc."
                    ),
                    "steps": [
                        "STEP 1: FIND THE CATEGORY — search_subcategories(limit=50) to "
                        "browse top-level categories, or subcategories() for just the "
                        "root-level names + IDs. Find the subcategory_id for the market "
                        "the user is asking about (e.g. 'Outdoor Recreation' = some ID).",

                        "STEP 2: FILTER BRANDS — search_brands(subcategory_id=X, "
                        "min_revenue=10000000, limit=25). This returns brands in that "
                        "category above the revenue threshold. NO name param needed.",

                        "STEP 2b: CHECK COVERAGE — every category search returns a "
                        "'coverage' object showing: products_scanned, unique_brands_found, "
                        "has_more (bool), and continue_with (instructions). If has_more "
                        "is true and you need more results, call search_brands again with "
                        "the start_page value from coverage.continue_with. You can also "
                        "increase scan_pages (default 20, max 50) for deeper scanning. "
                        "If new_brands_last_3_pages is low (< 5), you've found most brands.",

                        "STEP 3: OPTIONAL — for each brand of interest, pull "
                        "brand_report(brand_id=Y) for a full dossier.",
                    ],
                    "example": (
                        "User: 'find outdoor equipment brands doing over $10M/mo'\n"
                        "→ search_subcategories(limit=50) → find 'Outdoor Recreation' ID\n"
                        "→ search_brands(subcategory_id=ID, min_revenue=10000000) → done\n"
                        "That's 2 calls total. Do NOT search by name when the user "
                        "wants to browse a category.\n\n"
                        "User: 'brands doing $500K-$5M/year in outdoor'\n"
                        "→ $500K-$5M/year = $41,667-$416,667/month\n"
                        "→ search_subcategories → find category ID\n"
                        "→ search_brands(subcategory_id=ID, min_revenue=41667, "
                        "max_revenue=416667) → done\n\n"
                        "User: 'I need ALL brands, not just the first page'\n"
                        "→ First call returns coverage.has_more=true, continue_with says "
                        "'start_page=20'\n"
                        "→ search_brands(subcategory_id=ID, min_revenue=41667, "
                        "start_page=20) → gets more brands\n"
                        "→ Repeat until has_more=false or new_brands_last_3_pages < 5"
                    ),
                },
                {
                    "name": "Find brand owners & contact info (SmartScout → AI Ark pipeline)",
                    "when": (
                        "User wants to find the actual people behind Amazon brands — "
                        "owners, founders, CEOs — with emails for outreach. SmartScout "
                        "has zero contact info. You must pair it with AI Ark."
                    ),
                    "steps": [
                        "STEP 1: FIND BRANDS — use search_brands(subcategory_id=X, "
                        "min_revenue=Y) to get a list of Amazon brands. Each brand "
                        "has a name but NO domain, NO contact info.",

                        "STEP 2: RESOLVE DOMAINS — for each brand, call "
                        "ai_ark_search_companies(company_names='BrandName', size=3). "
                        "This returns company matches with domains. Pick the best "
                        "match by checking: (a) brand name appears in the domain, "
                        "(b) company name closely matches, (c) industry makes sense "
                        "(retail, consumer goods, manufacturing — not a consulting "
                        "firm or NGO with the same name). Skip brands with no match "
                        "or only suspicious matches (foreign domains for US brands, "
                        "totally different industries).",

                        "STEP 3: FIND PEOPLE — for each resolved domain, call "
                        "ai_ark_search_people(domains='resolved-domain.com', "
                        "job_titles='CEO,Founder,Owner,President', "
                        "max_per_company=2, size=10). Or use ai_ark_search_people("
                        "domains='resolved-domain.com', seniority_levels='vp,c_suite,"
                        "founder,owner', max_per_company=2).",

                        "STEP 4: GET EMAILS — for the people you found, call "
                        "ai_ark_export_people_with_email with the same filters, "
                        "then poll ai_ark_get_export_results(track_id=X) for "
                        "verified email addresses.",
                    ],
                    "accuracy_notes": (
                        "Expect ~53% domain resolution rate from AI Ark company "
                        "search. The misses are mostly:\n"
                        "- Chinese/generic Amazon-only brands (TYZDMY, GLYLF) with "
                        "no corporate web presence\n"
                        "- Short/ambiguous names (CAP, AXV) that match wrong companies\n\n"
                        "To improve accuracy:\n"
                        "- Add locations='United States' to ai_ark_search_companies "
                        "if you know the brands are US-based\n"
                        "- Verify the domain makes sense (brand name should appear "
                        "in the domain)\n"
                        "- For high-value misses, fall back to perplexity_ask("
                        "'What company owns the Amazon brand X? What is their website?')\n\n"
                        "DO NOT use ai_ark_search_people with company_names instead "
                        "of domains — it's too fuzzy and returns wrong people from "
                        "unrelated companies with similar names."
                    ),
                    "example": (
                        "User: 'find me the owners of top outdoor brands doing $1M+/mo'\n"
                        "→ search_subcategories → find 'Sports & Outdoors' ID\n"
                        "→ search_brands(subcategory_id=3375251, min_revenue=1000000, "
                        "limit=20) → get 20 brands\n"
                        "→ For each brand: ai_ark_search_companies(company_names="
                        "'BrandName', size=3) → pick best match → extract domain\n"
                        "→ For each domain: ai_ark_search_people(domains='domain.com', "
                        "job_titles='CEO,Founder,Owner', max_per_company=2)\n"
                        "→ Present results as a table: Brand | Revenue | Contact | "
                        "Title | Domain"
                    ),
                },
            ],
            "report_template": {
                "instructions": (
                    "Format every prospect report EXACTLY like this. Fill in data from "
                    "brand_report results. If a section has no data, skip it — don't "
                    "show empty sections. Use markdown formatting."
                ),
                "format": (
                    "## Brand Overview\n"
                    "- **Amazon Brand:** [name] (ID [id])\n"
                    "- **Monthly Revenue:** $[X]\n"
                    "- **TTM Revenue:** $[X]\n"
                    "- **Monthly Units:** [X]\n"
                    "- **Total Products:** [X] ASINs\n"
                    "- **Avg Price:** $[X]\n"
                    "- **Brand Score:** [X]/10\n"
                    "- **Reviews:** [X] total, [X] avg rating\n"
                    "- **Ad Spend:** ~$[X]/month\n"
                    "- **Storefront:** [URL or 'None']\n"
                    "- **YoY Growth:** [X]%\n"
                    "- **MoM Growth:** [X]% (note if seasonal)\n\n"
                    "[If revenue_history shows clear seasonality, note the pattern: "
                    "'Revenue peaks [month range] and dips [month range]']\n\n"
                    "---\n\n"
                    "## Top Products\n\n"
                    "| ASIN | Product | Revenue | Units | Buy Box | Sellers |\n"
                    "|------|---------|---------|-------|---------|--------|\n"
                    "[One row per product from the 'products' array]\n\n"
                    "[Call out if revenue is concentrated in one SKU — "
                    "e.g. 'Product X drives Y% of total brand revenue']\n\n"
                    "---\n\n"
                    "## Distribution / Buy Box\n"
                    "- [Top seller]: [X]% of brand revenue ([Y] offers)\n"
                    "- [Other sellers if any]\n"
                    "- [Note if brand controls own Buy Box or has reseller issues]\n"
                    "- [Note FBA vs merchant fulfilled]\n\n"
                    "---\n\n"
                    "## Search / SEO\n"
                    "- Ranking on [X] search terms across products\n"
                    "- Key branded terms: [list top branded search terms]\n"
                    "- Key generic terms: [list top generic/non-branded terms]\n"
                    "- [Note any strong ad win rates on specific terms]\n\n"
                    "---\n\n"
                    "## Competitive Landscape\n"
                    "[From the 'competitors' section — name top 3-5 competitors, "
                    "their estimated revenue, and whether they're gaining share]\n\n"
                    "---\n\n"
                    "## Key Takeaways for Outreach\n"
                    "[3-7 bullet points that would be valuable in a sales conversation. "
                    "Focus on:]\n"
                    "- Revenue concentration risk (one product = X% of revenue)\n"
                    "- Seasonality patterns and timing implications\n"
                    "- Buy Box control (do they own it or are they losing it?)\n"
                    "- FBA vs merchant fulfilled (opportunity?)\n"
                    "- Ad spend efficiency (high spend + low win rates = opportunity)\n"
                    "- Competitive pressure (are Chinese sellers eating share?)\n"
                    "- Category growth/decline trends\n"
                    "- Obvious gaps (no storefront, low review count, weak SEO)\n"
                ),
            },
            "tool_map": {
                "FINDING BRANDS": {
                    "search_brands (by name)": (
                        "Pass name='CompanyName' to look up a specific brand and get "
                        "its brand_id. If no results, check their website for the "
                        "actual product brand name they sell under on Amazon."
                    ),
                    "search_brands (by category/revenue)": (
                        "Omit name. Pass subcategory_id + min_revenue to find brands "
                        "in a market above a revenue threshold. Works with PARENT "
                        "categories — 'Sports & Outdoors' automatically searches all "
                        "child categories. Returns a 'coverage' object: "
                        "products_scanned, unique_brands_found, has_more, "
                        "continue_with. If has_more=true, call again with the "
                        "start_page from continue_with to get more brands. Use "
                        "scan_pages (default 20, max 50) to control depth."
                    ),
                    "search_brands (count_only)": (
                        "Add count_only=true for a FAST approximate count + top 20 "
                        "preview. Skips per-brand enrichment. Returns coverage stats "
                        "showing how much of the category was scanned. If has_more "
                        "is true, call again with start_page=X and count_only=true "
                        "to scan deeper. Good first call to understand the data size "
                        "before pulling the full enriched list."
                    ),
                },
                "FULL REPORT (use this 90% of the time)": {
                    "brand_report": (
                        "ALL-IN-ONE dossier. Fetches 8+ data sources in one call. "
                        "Use this instead of calling individual tools separately."
                    ),
                },
                "INDIVIDUAL TOOLS (only if you need more detail beyond brand_report)": {
                    "get_brand": "Full brand profile snapshot (30+ fields).",
                    "brand_history": "Revenue/units/price weekly time-series.",
                    "brand_history_by_category": "Revenue split by Amazon subcategory.",
                    "brand_coverage": "Which 3P sellers carry the brand (brand-wide).",
                    "search_products": "Find/list products by brand, ASIN, category, or keyword.",
                    "product_sellers": "Buy Box data for a single product listing.",
                    "product_history": "Price/revenue/rank time-series for products.",
                    "product_organic_ranks": "Current organic search rankings for a product.",
                    "product_rank_history": "How organic ranks changed over time.",
                    "product_relevancy": "Find competing products for an ASIN.",
                    "search_terms_for_product": "Ad/PPC win rates per search term.",
                    "brand_tailored_report": "Report freshness check (rarely needed).",
                },
                "MARKET / CATEGORY EXPLORATION": {
                    "subcategories": "List top-level Amazon category names + IDs.",
                    "search_subcategories": "Browse categories ranked by revenue with full stats.",
                    "subcategory_brands": "Top brands in a specific category.",
                    "subcategory_history": "Category revenue trends over time.",
                },
            },
            "common_pitfalls": [
                (
                    "DON'T FUMBLE — EVERY QUERY SHOULD TAKE 1-3 CALLS MAX: "
                    "If you're making more than 3 tool calls to answer a question, "
                    "you're doing it wrong. Read this guide carefully. Examples of "
                    "efficient workflows:\n"
                    "- 'Research BrandX' → search_brands + brand_report = 2 calls\n"
                    "- 'Brands over $10M in outdoor' → search_subcategories + "
                    "search_brands(subcategory_id, min_revenue) = 2 calls\n"
                    "- 'Who sells ProductX' → product_sellers = 1 call\n"
                    "Do NOT waste calls searching for tools, reading schemas, or "
                    "trying random endpoints. This guide tells you everything."
                ),
                (
                    "search_brands HAS TWO MODES: (1) Pass 'name' to look up a "
                    "specific brand. (2) Omit 'name' and pass subcategory_id + "
                    "min_revenue to BROWSE brands by category and revenue. Mode 2 "
                    "works with parent categories — 'Sports & Outdoors' (3375251) "
                    "searches all children automatically. Add count_only=true for a "
                    "fast count without full enrichment. Use mode 2 when the user "
                    "wants a list of brands in a market, not a specific company lookup."
                ),
                (
                    "BRAND NAME != COMPANY NAME: Many companies sell under different "
                    "brand names on Amazon. 'Black Diamond Coatings' → 'Dominator'. "
                    "'Church & Dwight' → 'OxiClean'. If search_brands returns nothing, "
                    "visit their website, find the brand names on their product pages, "
                    "and search for those instead."
                ),
                (
                    "DON'T CALL INDIVIDUAL TOOLS IF brand_report COVERS IT: "
                    "brand_report already includes brand profile, history, products, "
                    "Buy Box data, sellers, competitors, and category landscape. Only "
                    "use individual tools if you need deeper detail on a specific area "
                    "that brand_report summarized too aggressively."
                ),
                (
                    "product_id vs asin: product_id is a SmartScout numeric ID (from "
                    "search_products results). asin is the Amazon string like 'B09XYZ'. "
                    "product_sellers/product_history/product_organic_ranks need "
                    "product_id. product_relevancy needs asin."
                ),
                (
                    "brand_coverage vs product_sellers: brand_coverage shows all "
                    "sellers across an entire brand. product_sellers shows Buy Box "
                    "competition on a single ASIN. Different questions, different tools."
                ),
                (
                    "product_organic_ranks vs product_rank_history vs "
                    "search_terms_for_product: Three different things. organic_ranks = "
                    "current SEO positions. rank_history = how positions changed over "
                    "time. search_terms_for_product = ad/PPC win rates (not organic)."
                ),
                (
                    "SEASONALITY: Many Amazon brands are seasonal. Look at "
                    "revenue_history in the report and note the peaks/dips. This is "
                    "valuable sales intelligence — e.g. pitch during their slow season "
                    "when they're thinking about next year's strategy."
                ),
            ],
            "other_playbooks": {
                "Find big brands in a category (prospecting)": (
                    "1. search_subcategories(limit=50) → find the category ID\n"
                    "2. search_brands(subcategory_id=X, min_revenue=10000000) → brands over $10M\n"
                    "That's it — 2 calls. Use offset for pagination if you need more."
                ),
                "Competitor deep-dive": (
                    "1. brand_report(brand_id=X) → check 'competitors' section\n"
                    "2. For each competitor, search_brands(name='CompetitorBrand') → get their brand_id\n"
                    "3. brand_report(brand_id=Y) on competitor → compare side by side"
                ),
                "Category opportunity sizing": (
                    "1. search_subcategories(limit=20) → biggest categories by revenue\n"
                    "2. subcategory_brands(subcategory_id=X) → who dominates\n"
                    "3. subcategory_history(subcategory_id=X) → growing or shrinking?"
                ),
                "Buy Box / pricing audit": (
                    "1. search_products(brand_id=X, limit=10) → find products + IDs\n"
                    "2. product_sellers(product_id=Y) → Buy Box winners per listing\n"
                    "3. product_history(product_ids=[Y,Z]) → pricing trends over time"
                ),
                "SEO / advertising audit": (
                    "1. search_products(brand_id=X, limit=5) → top products\n"
                    "2. product_organic_ranks(product_id=Y) → current positions\n"
                    "3. search_terms_for_product(product_id=Y) → ad win rates\n"
                    "4. product_rank_history(product_id=Y) → rank trajectory"
                ),
            },
        }

    # ── Brand handlers ──

    _BRAND_FIELDS = [
        "id", "name", "monthlyRevenue", "monthlyUnitsSold",
        "momGrowth", "totalProducts", "avgPrice", "reviewRating",
        "totalReviews", "brandScore", "ttm",
        "primaryCategoryId", "primarySubcategoryId",
    ]

    def search_brands(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth

        name = kwargs.get("name", "")
        subcat = kwargs.get("subcategory_id") or kwargs.get("category_id")
        min_rev = kwargs.get("min_revenue")
        max_rev = kwargs.get("max_revenue")

        # MODE 1: name lookup — search by name, enrich with get_brand
        if name:
            results = _post(token, "/api/brands/search", {
                "loadDefaultData": False,
                "filter": {"name": name},
                "pageFilter": {
                    "startRow": 0,
                    "endRow": min(kwargs.get("limit", 25), 100),
                    "sortModel": [{"colId": "monthlyRevenue", "sort": "desc"}],
                    "fields": ["id", "name", "monthlyRevenue"],
                },
            }, mp)
            out = []
            for b in (results.get("payload") or []):
                bid = b.get("id")
                if bid:
                    detail = _safe(_get, token, "/api/brands", mp, Id=bid)
                    if isinstance(detail, dict) and "_error" not in detail:
                        rev = detail.get("monthlyRevenue") or 0
                        if min_rev and rev < min_rev:
                            continue
                        if max_rev and rev > max_rev:
                            continue
                        out.append({k: detail.get(k) for k in self._BRAND_FIELDS})
            return {"brands": out, "total": len(out)}

        # MODE 2: category browse — find brands via products in the subcategory
        if subcat:
            return self._brands_in_subcategory(token, mp, subcat, min_rev, max_rev, kwargs)

        # MODE 3: global browse (no subcategory filter — API ignores it anyway)
        limit = min(kwargs.get("limit", 25), 100)
        offset = kwargs.get("offset", 0)
        results = _post(token, "/api/brands/search", {
            "loadDefaultData": False,
            "filter": {},
            "pageFilter": {
                "startRow": offset, "endRow": offset + limit,
                "sortModel": [{"colId": kwargs.get("sort_by", "monthlyRevenue"),
                               "sort": kwargs.get("sort_dir", "desc")}],
                "fields": ["id", "name", "monthlyRevenue"],
            },
        }, mp)
        if not min_rev and not max_rev:
            return results
        filtered = []
        for b in (results.get("payload") or []):
            rev = b.get("monthlyRevenue") or 0
            if min_rev and rev < min_rev:
                continue
            if max_rev and rev > max_rev:
                continue
            filtered.append(b)
        return {"payload": filtered, "total": len(filtered)}

    def _brands_in_subcategory(
        self, token: str, mp: str, subcat_id: int,
        min_rev: float | None, max_rev: float | None, kwargs: dict,
    ) -> dict:
        """Discover brands in a subcategory by scanning products, then enriching."""
        limit = min(kwargs.get("limit", 100), 200)
        count_only = kwargs.get("count_only", False)
        scan_pages = min(kwargs.get("scan_pages", 20), 50)
        start_page = kwargs.get("start_page", 0)

        brand_ids: dict[int, str] = {}
        brand_prod_rev: dict[int, float] = {}
        new_brands_per_page: list[int] = []
        products_scanned = 0
        exhausted = False

        for page_idx in range(scan_pages):
            page_num = start_page + page_idx
            before = len(brand_ids)
            prods = _safe(_post, token, "/api/products/search", {
                "loadDefaultData": False,
                "filter": {"subcategoryId": subcat_id},
                "pageFilter": {
                    "startRow": page_num * 100, "endRow": (page_num + 1) * 100,
                    "sortModel": [{"colId": "monthlyRevenueEstimate", "sort": "desc"}],
                    "fields": ["brandId", "brandName", "monthlyRevenueEstimate"],
                },
            }, mp)
            if not isinstance(prods, dict) or "payload" not in prods:
                exhausted = True
                break
            batch = prods["payload"]
            products_scanned += len(batch)
            for p in batch:
                bid = p.get("brandId")
                if not bid:
                    continue
                if bid not in brand_ids:
                    brand_ids[bid] = p.get("brandName", "")
                    brand_prod_rev[bid] = 0
                brand_prod_rev[bid] += p.get("monthlyRevenueEstimate") or 0
            new_brands_per_page.append(len(brand_ids) - before)
            if len(batch) < 100:
                exhausted = True
                break

        next_page = start_page + len(new_brands_per_page)
        recent_new = sum(new_brands_per_page[-3:]) if new_brands_per_page else 0
        coverage: dict[str, Any] = {
            "products_scanned": products_scanned,
            "unique_brands_found": len(brand_ids),
            "new_brands_last_3_pages": recent_new,
            "scanned_pages": f"{start_page}-{next_page - 1}",
            "exhausted": exhausted,
        }
        if not exhausted:
            coverage["has_more"] = True
            coverage["continue_with"] = (
                f"Call again with start_page={next_page} to scan deeper. "
                f"Last 3 pages found {recent_new} new brands."
            )
        else:
            coverage["has_more"] = False
            coverage["note"] = "All products in this category have been scanned."

        if count_only:
            count = 0
            approx_brands = []
            for bid, name in brand_ids.items():
                approx_rev = brand_prod_rev.get(bid, 0)
                if min_rev and approx_rev < min_rev * 0.3:
                    continue
                if max_rev and approx_rev > max_rev * 3:
                    continue
                count += 1
                approx_brands.append({"id": bid, "name": name,
                                      "approx_category_revenue": round(approx_rev, 2)})
            approx_brands.sort(key=lambda b: b["approx_category_revenue"], reverse=True)
            return {
                "approximate_count": count,
                "subcategory_id": subcat_id,
                "coverage": coverage,
                "top_brands_preview": approx_brands[:20],
            }

        brands_out = []
        for bid in brand_ids:
            detail = _safe(_get, token, "/api/brands", mp, Id=bid)
            if not isinstance(detail, dict) or "_error" in detail:
                continue
            rev = detail.get("monthlyRevenue") or 0
            if min_rev and rev < min_rev:
                continue
            if max_rev and rev > max_rev:
                continue
            brands_out.append({k: detail.get(k) for k in self._BRAND_FIELDS})

        sort_key = kwargs.get("sort_by", "monthlyRevenue")
        reverse = kwargs.get("sort_dir", "desc") == "desc"
        brands_out.sort(key=lambda b: b.get(sort_key) or 0, reverse=reverse)

        offset = kwargs.get("offset", 0)
        result_page = brands_out[offset:offset + limit]
        return {
            "brands": result_page,
            "total_matching": len(brands_out),
            "subcategory_id": subcat_id,
            "coverage": coverage,
        }

    def get_brand(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _get(token, "/api/brands", mp, Id=kwargs["brand_id"])

    def brand_history(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/brands/history", {
            "brandId": kwargs["brand_id"],
            "startDate": kwargs.get("start_date", "2024-01-01T00:00:00.000Z"),
            "supercharged": kwargs.get("supercharged", True),
        }, mp)

    def brand_history_by_category(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/brands/history-by-category", {
            "brandId": kwargs["brand_id"],
            "startDate": kwargs.get("start_date", "2025-01-01T00:00:00.000Z"),
            "endDate": kwargs.get("end_date", "2026-12-31T23:59:59.999Z"),
            "limit": kwargs.get("limit", 10),
        }, mp)

    def brand_coverage(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/brandcoverage/search",
                     {"brandId": kwargs["brand_id"]}, mp)

    def brand_tailored_report(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/brands/tailored-report/status",
                     {"brandId": kwargs["brand_id"]}, mp)

    # ── Product handlers ──

    def search_products(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        limit = min(kwargs.get("limit", 25), 100)
        offset = kwargs.get("offset", 0)
        body: dict[str, Any] = {
            "loadDefaultData": False,
            "filter": {},
            "pageFilter": {
                "startRow": offset, "endRow": offset + limit,
                "sortModel": [{"colId": kwargs.get("sort_by", "monthlyRevenueEstimate"),
                               "sort": kwargs.get("sort_dir", "desc")}],
                "fields": ["imageUrl", "asin", "brandName", "title", "rank",
                           "monthlyRevenueEstimate", "monthlyUnitsSold",
                           "momGrowth", "rankScoreGrowth", "brandId", "categoryId",
                           "dateSeen", "buyBoxPrice", "numberOfSellers"],
            },
        }
        f = body["filter"]
        if kwargs.get("brand_id"):
            f["brandId"] = kwargs["brand_id"]
        if kwargs.get("category_id"):
            f["categoryId"] = kwargs["category_id"]
        if kwargs.get("subcategory_id"):
            f["subcategoryId"] = kwargs["subcategory_id"]
        if kwargs.get("asin"):
            f["asin"] = kwargs["asin"]
        if kwargs.get("title"):
            f["title"] = kwargs["title"]
        if kwargs.get("min_revenue"):
            f["minMonthlyRevenueEstimate"] = kwargs["min_revenue"]
        if kwargs.get("max_revenue"):
            f["maxMonthlyRevenueEstimate"] = kwargs["max_revenue"]
        return _post(token, "/api/products/search", body, mp)

    def product_sellers(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        raw = _post(token, "/api/products/sellers",
                    {"productId": kwargs["product_id"]}, mp)
        if not isinstance(raw, list):
            return raw
        return [
            {
                "seller": (s.get("seller", {}) or {}).get("name"),
                "seller_id": s.get("sellerId"),
                "price": s.get("price"),
                "buybox_pct": s.get("buyBoxPercentage"),
                "is_fba": s.get("isFba"),
                "revenue": s.get("monthlyRevenue"),
            }
            for s in raw
        ]

    def product_history(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/products/history/scope", {
            "productIds": kwargs["product_ids"],
            "startDate": kwargs.get("start_date", "2025-01-01T00:00:00.000Z"),
        }, mp)

    def product_organic_ranks(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/products/organic-ranks", {
            "productId": kwargs["product_id"],
            "excludeAdvertisingCampaigns": kwargs.get("exclude_ads", True),
            "excludeRankHistory": kwargs.get("exclude_rank_history", True),
        }, mp)

    def product_rank_history(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/products/organic-ranks/history",
                     {"productId": kwargs["product_id"]}, mp)

    def product_relevancy(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/relevancy/products", {
            "asin": kwargs["asin"],
            "competitorsOnly": kwargs.get("competitors_only", True),
        }, mp)

    # ── Search term handlers ──

    def search_terms_for_product(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        limit = min(kwargs.get("limit", 100), 20000)
        return _post(token, "/api/search-terms/products", {
            "filter": {"productId": kwargs["product_id"]},
            "pageFilter": {
                "endRow": limit,
                "sortModel": [{"colId": kwargs.get("sort_by", "searchTermId"),
                               "sort": kwargs.get("sort_dir", "asc")}],
                "fields": ["searchTerm.searchTermValue",
                           "topSpotWinRate", "topGroupWinRate",
                           "sponsoredBrandWinRate", "sponsoredVideoWinRate"],
            },
        }, mp)

    # ── Subcategory handlers ──

    def subcategories(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _get(token, "/api/subcategories/root-nodes", mp)

    def search_subcategories(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        limit = min(kwargs.get("limit", 25), 100)
        offset = kwargs.get("offset", 0)
        return _post(token, "/api/subcategories/search", {
            "loadDefaultData": False,
            "filter": {},
            "pageFilter": {
                "startRow": offset, "endRow": offset + limit,
                "sortModel": [{"colId": kwargs.get("sort_by", "totalMonthlyRevenue"),
                               "sort": kwargs.get("sort_dir", "desc")}],
                "fields": ["id", "subcategoryName", "totalMonthlyRevenue",
                           "totalBrands", "totalAsins", "totalNumberUnitsSold",
                           "avgPrice", "avgReviews", "avgRating",
                           "momGrowth", "momGrowth12", "chinaRevenuePct",
                           "azRevenuePct", "sellerRevenuePct", "ttm",
                           "isParent", "level", "isLeafNode", "parentId"],
            },
        }, mp)

    def subcategory_brands(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/subcategories/history/brand-subcategories",
                     {"subcategoryId": kwargs["subcategory_id"]}, mp)

    def subcategory_history(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        return _post(token, "/api/subcategories/history",
                     {"subcategoryId": kwargs["subcategory_id"]}, mp)

    # ── Composite report ──

    def brand_report(self, **kwargs) -> Any:
        auth = self._auth(kwargs)
        if isinstance(auth, dict):
            return auth
        token, mp = auth
        bid = kwargs["brand_id"]
        top_n = min(kwargs.get("top_n_products", 5), 10)

        brand = _safe(_get, token, "/api/brands", mp, Id=bid)
        tailored = _safe(_post, token, "/api/brands/tailored-report/status",
                         {"brandId": bid}, mp)
        history_raw = _safe(_post, token, "/api/brands/history", {
            "brandId": bid, "startDate": "2024-01-01T00:00:00.000Z",
            "supercharged": True,
        }, mp)
        hist_cat_raw = _safe(_post, token, "/api/brands/history-by-category", {
            "brandId": bid, "startDate": "2025-01-01T00:00:00.000Z",
            "endDate": "2026-12-31T23:59:59.999Z", "limit": 5,
        }, mp)
        products_raw = _safe(_post, token, "/api/products/search", {
            "loadDefaultData": False,
            "filter": {"brandId": bid},
            "pageFilter": {
                "startRow": 0, "endRow": top_n,
                "sortModel": [{"colId": "monthlyRevenueEstimate", "sort": "desc"}],
                "fields": ["imageUrl", "asin", "brandName", "title", "rank",
                           "monthlyRevenueEstimate", "monthlyUnitsSold",
                           "momGrowth", "rankScoreGrowth", "brandId", "categoryId",
                           "dateSeen", "buyBoxPrice", "numberOfSellers"],
            },
        }, mp)
        coverage = _safe(_post, token, "/api/brandcoverage/search",
                         {"brandId": bid}, mp)

        report: dict[str, Any] = {}

        if isinstance(brand, dict) and "_error" not in brand:
            report["brand"] = {
                k: brand.get(k) for k in [
                    "id", "name", "monthlyRevenue", "monthlyUnitsSold",
                    "momGrowth", "momGrowth12", "ttm", "brandScore",
                    "totalProducts", "totalReviews", "avgPrice", "avgVolume",
                    "reviewRating", "totalAdSpend", "azRevenuePct",
                    "hasSingleSeller", "hasStorefront", "storefrontUrl",
                    "sponsoredBrandWinRate", "topSpotWinRate",
                    "rankingProducts", "rankingSearchTerms",
                    "primaryCategoryId", "primarySubcategoryId",
                ]
            }

        if isinstance(tailored, dict) and "_error" not in tailored:
            report["tailored_status"] = {
                "isReady": tailored.get("isReady"),
                "totalAsins": tailored.get("totalAsins"),
                "staleAsins": tailored.get("staleAsins"),
            }

        if isinstance(history_raw, list) and history_raw:
            sampled = []
            step = max(1, len(history_raw) // 12)
            for i in range(0, len(history_raw), step):
                h = history_raw[i]
                sampled.append({
                    "date": h.get("date", "")[:10],
                    "revenue": h.get("estimateWeeklyRevenue"),
                    "units": h.get("weeklyEstimatedUnitSales"),
                    "price": h.get("avgBuyBoxPrice"),
                    "asins": h.get("totalAsins"),
                })
            if history_raw[-1] not in [history_raw[i] for i in range(0, len(history_raw), step)]:
                h = history_raw[-1]
                sampled.append({
                    "date": h.get("date", "")[:10],
                    "revenue": h.get("estimateWeeklyRevenue"),
                    "units": h.get("weeklyEstimatedUnitSales"),
                    "price": h.get("avgBuyBoxPrice"),
                    "asins": h.get("totalAsins"),
                })
            report["revenue_history"] = {"total_weeks": len(history_raw), "samples": sampled}

        if isinstance(hist_cat_raw, list) and hist_cat_raw:
            cats = []
            for entry in hist_cat_raw[:5]:
                sb = entry.get("subcategoryBrand", {})
                hists = entry.get("subcategoryBrandHistories", [])
                latest = hists[-1] if hists else {}
                cats.append({
                    "subcategory_id": sb.get("subcategoryId"),
                    "latest_revenue": latest.get("estimateWeeklyRevenue"),
                    "latest_units": latest.get("weeklyEstimatedUnitSales"),
                    "data_points": len(hists),
                })
            report["category_breakdown"] = cats

        products = []
        product_ids = []
        product_asins = []
        if isinstance(products_raw, dict) and "payload" in products_raw:
            for p in products_raw["payload"][:top_n]:
                products.append({
                    "asin": p.get("asin"),
                    "title": (p.get("title") or "")[:80],
                    "revenue": p.get("monthlyRevenueEstimate"),
                    "units": p.get("monthlyUnitsSold"),
                    "rank": p.get("rank"),
                    "mom_growth": p.get("momGrowth"),
                    "buybox_price": p.get("buyBoxPrice"),
                    "num_sellers": p.get("numberOfSellers"),
                })
                pid = p.get("id") or p.get("productId")
                if pid:
                    product_ids.append(pid)
                if p.get("asin"):
                    product_asins.append(p["asin"])
        report["products"] = products

        # Per-product details: ranks, search terms, Buy Box sellers
        product_details = []
        for i, pid in enumerate(product_ids[:3]):
            detail: dict[str, Any] = {"product_id": pid}
            if i < len(product_asins):
                detail["asin"] = product_asins[i]

            ranks_raw = _safe(_post, token, "/api/products/organic-ranks", {
                "productId": pid,
                "excludeAdvertisingCampaigns": True,
                "excludeRankHistory": True,
            }, mp)
            if isinstance(ranks_raw, list):
                top_ranks = sorted(ranks_raw, key=lambda x: x.get("organicRank") or 9999)[:15]
                detail["organic_ranks"] = [
                    {
                        "term": (r.get("searchTerm", {}) or {}).get("searchTermValue", ""),
                        "rank": r.get("organicRank"),
                        "volume": (r.get("searchTerm", {}) or {}).get("estimateSearchVolume"),
                    }
                    for r in top_ranks
                ]
                detail["total_ranked_terms"] = len(ranks_raw)

            terms_raw = _safe(_post, token, "/api/search-terms/products", {
                "filter": {"productId": pid},
                "pageFilter": {
                    "endRow": 20000,
                    "sortModel": [{"colId": "searchTermId", "sort": "asc"}],
                    "fields": ["searchTerm.searchTermValue",
                               "topSpotWinRate", "topGroupWinRate",
                               "sponsoredBrandWinRate", "sponsoredVideoWinRate"],
                },
            }, mp)
            if isinstance(terms_raw, dict) and "payload" in terms_raw:
                terms = [
                    {
                        "term": (t.get("searchTerm", {}) or {}).get("searchTermValue", ""),
                        "top_spot": t.get("topSpotWinRate"),
                        "top_group": t.get("topGroupWinRate"),
                        "sp_brand": t.get("sponsoredBrandWinRate"),
                        "sp_video": t.get("sponsoredVideoWinRate"),
                    }
                    for t in terms_raw["payload"]
                ]
                detail["search_term_ad_rates"] = terms
                detail["total_search_terms"] = len(terms)

            sellers_raw = _safe(_post, token, "/api/products/sellers",
                                {"productId": pid}, mp)
            if isinstance(sellers_raw, list):
                detail["buybox_sellers"] = [
                    {
                        "seller": (s.get("seller", {}) or {}).get("name"),
                        "price": s.get("price"),
                        "buybox_pct": s.get("buyBoxPercentage"),
                        "is_fba": s.get("isFba"),
                        "revenue": s.get("monthlyRevenue"),
                    }
                    for s in sellers_raw[:10]
                ]

            product_details.append(detail)

        if product_details:
            report["product_details"] = product_details

        if product_ids:
            prod_hist = _safe(_post, token, "/api/products/history/scope", {
                "productIds": product_ids[:5],
                "startDate": "2025-06-01T00:00:00.000Z",
            }, mp)
            if isinstance(prod_hist, list):
                ph_summary = []
                for entry in prod_hist:
                    hists = entry.get("histories", [])
                    if hists:
                        latest = hists[-1]
                        earliest = hists[0]
                        ph_summary.append({
                            "weeks": len(hists),
                            "latest_price": latest.get("buyBoxPrice"),
                            "latest_revenue": latest.get("estimateWeeklyRevenue"),
                            "latest_units": latest.get("weeklyEstimatedUnitSales"),
                            "latest_rank": latest.get("salesRank"),
                            "earliest_price": earliest.get("buyBoxPrice"),
                            "earliest_revenue": earliest.get("estimateWeeklyRevenue"),
                        })
                report["product_history"] = ph_summary

        if isinstance(coverage, dict) and "payload" in coverage:
            report["sellers"] = [
                {
                    "name": s.get("sellerName"),
                    "revenue": s.get("monthlyRevenue"),
                    "brand_pct": s.get("estimateBrandPercentage"),
                    "offers": s.get("numberOffers"),
                    "coverage_change": s.get("moMCoverageChange"),
                }
                for s in coverage["payload"][:10]
            ]

        if product_asins:
            rel_raw = _safe(_post, token, "/api/relevancy/products", {
                "asin": product_asins[0], "competitorsOnly": True,
            }, mp)
            if isinstance(rel_raw, list):
                competitors = []
                for r in rel_raw[:15]:
                    p = r.get("product", {}) or {}
                    competitors.append({
                        "asin": p.get("asin"),
                        "title": (p.get("title") or "")[:60],
                        "brand": (p.get("brand", {}) or {}).get("name"),
                        "revenue": p.get("monthlyRevenueEstimate"),
                        "rank": p.get("rank"),
                        "relevancy": r.get("relevancyScore"),
                    })
                report["competitors"] = {
                    "for_asin": product_asins[0],
                    "total_found": len(rel_raw),
                    "top_15": competitors,
                }

        subcat_id = (brand if isinstance(brand, dict) else {}).get("primarySubcategoryId")
        if subcat_id:
            sc_brands = _safe(_post, token,
                              "/api/subcategories/history/brand-subcategories",
                              {"subcategoryId": subcat_id}, mp)
            if isinstance(sc_brands, list):
                landscape = []
                for entry in sc_brands[:10]:
                    b = entry.get("brand", {}) or {}
                    hists = entry.get("subcategoryBrandHistories", [])
                    latest = hists[-1] if hists else {}
                    landscape.append({
                        "brand": b.get("name"),
                        "brand_id": b.get("id"),
                        "revenue": latest.get("estimateWeeklyRevenue"),
                        "units": latest.get("weeklyEstimatedUnitSales"),
                        "market_share": latest.get("marketShare"),
                    })
                report["subcategory_landscape"] = {
                    "subcategory_id": subcat_id, "top_brands": landscape,
                }

        return report
