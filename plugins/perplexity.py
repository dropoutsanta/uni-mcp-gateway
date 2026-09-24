"""Perplexity AI plugin for the MCP Gateway.

Exposes all Perplexity APIs as MCP tools: Sonar (web-grounded chat),
Search (raw web results), Agent (multi-provider models), and Embeddings.

Required credentials:
  api_key – Perplexity API key (pplx-...)
"""

from __future__ import annotations

from typing import Any

import httpx

from plugin_base import MCPPlugin, ToolDef, get_credentials

_API = "https://api.perplexity.ai"
_TIMEOUT = 120


def _get_key() -> str:
    creds = get_credentials("perplexity")
    key = creds.get("api_key", "")
    if not key:
        raise RuntimeError("Perplexity api_key not configured.")
    return key


def _headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, body: dict, timeout: int = _TIMEOUT) -> dict:
    key = _get_key()
    r = httpx.post(f"{_API}{path}", headers=_headers(key), json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _clean_sonar_response(data: dict) -> dict:
    """Flatten a Sonar response into agent-friendly fields."""
    out: dict[str, Any] = {"id": data.get("id"), "model": data.get("model")}
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message", {})
        out["answer"] = msg.get("content", "")
    out["citations"] = data.get("citations") or []
    sr = data.get("search_results")
    if sr:
        out["search_results"] = [
            {"title": r.get("title"), "url": r.get("url"), "date": r.get("date")}
            for r in sr
        ]
    usage = data.get("usage")
    if usage:
        out["usage"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    related = data.get("related_questions")
    if related:
        out["related_questions"] = related
    return out


class PerplexityPlugin(MCPPlugin):
    name = "perplexity"
    tools: dict[str, ToolDef] = {}

    def __init__(self):
        self.tools = {
            "how_to_use_me": ToolDef(
                access="read", handler=self.how_to_use_me,
                description=(
                    "START HERE. Returns a guide explaining every Perplexity tool, "
                    "when to use each one, and example workflows. Read this before "
                    "calling any other perplexity tool."
                ),
            ),
            "ask": ToolDef(
                access="read", handler=self.ask,
                description=(
                    "Ask Perplexity a question and get a web-grounded answer with "
                    "citations. Uses the Sonar API. This is the primary tool — use "
                    "it for any question that benefits from real-time web knowledge.\n\n"
                    "Params: question (required), model (sonar|sonar-pro|"
                    "sonar-reasoning-pro|sonar-deep-research, default sonar-pro), "
                    "system_prompt (optional instructions), search_mode (web|academic|"
                    "sec, default web), search_domain_filter (list of domains to "
                    "restrict to), search_recency_filter (hour|day|week|month|year), "
                    "return_images (bool), return_related_questions (bool), "
                    "temperature (0-2), max_tokens (int).\n\n"
                    "Returns: answer text, citations (URLs), search_results, usage."
                ),
            ),
            "chat": ToolDef(
                access="read", handler=self.chat,
                description=(
                    "Multi-turn conversation with Perplexity Sonar. Like 'ask' but "
                    "accepts a full messages array for follow-up questions with "
                    "conversation context.\n\n"
                    "Params: messages (required — array of {role, content} objects), "
                    "model, system_prompt, search_mode, search_domain_filter, "
                    "search_recency_filter, return_images, return_related_questions, "
                    "temperature, max_tokens.\n\n"
                    "Use 'ask' for single questions. Use 'chat' when you need to "
                    "maintain conversation history across turns."
                ),
            ),
            "search": ToolDef(
                access="read", handler=self.search,
                description=(
                    "Raw web search — returns ranked search result pages with titles, "
                    "URLs, snippets, and dates. No AI summary, just search results. "
                    "Use when you need raw URLs/snippets rather than an AI answer.\n\n"
                    "Params: query (required — string or list of strings for multi-"
                    "query), max_results (1-20, default 10), search_domain_filter "
                    "(list of domains), search_recency_filter (hour|day|week|month|"
                    "year), search_after_date (MM/DD/YYYY), search_before_date "
                    "(MM/DD/YYYY), country (ISO 2-letter code), "
                    "search_language_filter (list of ISO 639-1 codes).\n\n"
                    "Returns: array of {title, url, snippet, date} results."
                ),
            ),
            "research": ToolDef(
                access="read", handler=self.research,
                description=(
                    "Deep research on a topic — uses sonar-deep-research model for "
                    "multi-step web research with comprehensive analysis. Slower but "
                    "much more thorough than 'ask'. Use for complex questions that "
                    "need synthesis from many sources.\n\n"
                    "Params: question (required), system_prompt, search_domain_filter, "
                    "return_related_questions (bool), max_tokens.\n\n"
                    "Returns: detailed answer with citations. May take 30-60+ seconds."
                ),
            ),
            "reason": ToolDef(
                access="read", handler=self.reason,
                description=(
                    "Web-grounded reasoning — uses sonar-reasoning-pro for questions "
                    "that need step-by-step logical analysis with web evidence. Good "
                    "for math, comparisons, fact-checking, and complex analysis.\n\n"
                    "Params: question (required), system_prompt, search_domain_filter, "
                    "search_recency_filter, max_tokens.\n\n"
                    "Returns: reasoned answer with citations."
                ),
            ),
            "agent": ToolDef(
                access="read", handler=self.agent,
                description=(
                    "Multi-provider Agent API — run prompts through third-party models "
                    "(OpenAI, Anthropic, Google, xAI) with Perplexity's built-in web "
                    "search and URL fetching tools. Useful when you need a specific "
                    "model (e.g. gpt-4o, claude-4-sonnet) with web access.\n\n"
                    "Params: input (required — string or messages array), model "
                    "(e.g. 'openai/gpt-4o', 'anthropic/claude-4-sonnet', "
                    "'google/gemini-2.5-pro', 'xai/grok-4-1'), "
                    "OR preset ('fast-search', 'pro-search', 'deep-research'), "
                    "instructions (system prompt), max_output_tokens, "
                    "max_steps (1-10, for research loops).\n\n"
                    "Returns: model output with search results and citations."
                ),
            ),
            "embed": ToolDef(
                access="read", handler=self.embed,
                description=(
                    "Generate text embeddings for semantic search, RAG, or clustering. "
                    "Accepts a single string or array of up to 512 strings.\n\n"
                    "Params: input (required — string or list of strings), "
                    "model (pplx-embed-v1-0.6b|pplx-embed-v1-4b, default "
                    "pplx-embed-v1-4b), dimensions (128-2560, optional).\n\n"
                    "Returns: array of embedding vectors + usage/cost info."
                ),
            ),
        }

    def how_to_use_me(self, **kwargs) -> Any:
        return {
            "overview": (
                "Perplexity gives you real-time web-grounded AI — ask questions "
                "and get answers backed by live web sources with citations. It also "
                "provides raw web search, deep multi-step research, reasoning, "
                "multi-provider model access, and text embeddings."
            ),
            "quick_guide": {
                "Simple question with web context": (
                    "ask(question='What is the latest iPhone model and price?')"
                ),
                "Question restricted to specific sites": (
                    "ask(question='Python 3.13 new features', "
                    "search_domain_filter=['python.org', 'docs.python.org'])"
                ),
                "Only recent results": (
                    "ask(question='AI funding rounds', search_recency_filter='week')"
                ),
                "Academic/research papers": (
                    "ask(question='mRNA vaccine efficacy studies', search_mode='academic')"
                ),
                "SEC filings": (
                    "ask(question='Tesla 10-K revenue breakdown', search_mode='sec')"
                ),
                "Deep research (slow, thorough)": (
                    "research(question='Compare all major cloud providers pricing for GPU instances')"
                ),
                "Step-by-step reasoning": (
                    "reason(question='Is it cheaper to fly or drive from NYC to DC for a family of 4?')"
                ),
                "Raw search results (no AI summary)": (
                    "search(query='best CRM for small business 2026', max_results=15)"
                ),
                "Use a specific model with web search": (
                    "agent(input='Summarize this quarter earnings for NVDA', "
                    "model='openai/gpt-4o')"
                ),
                "Multi-turn conversation": (
                    "chat(messages=[{role:'user', content:'What is SpaceX Starship?'}, "
                    "{role:'assistant', content:'...'}, "
                    "{role:'user', content:'When is the next launch?'}])"
                ),
            },
            "tool_picker": {
                "ask": "DEFAULT. Single question → web-grounded answer. Use 90% of the time.",
                "chat": "Multi-turn follow-ups. Only when you need conversation history.",
                "search": "Raw URLs + snippets. Use when you need links, not an answer.",
                "research": "Deep multi-step research. Slow (30-60s). Use for complex synthesis.",
                "reason": "Logical reasoning + web facts. Math, comparisons, fact-checking.",
                "agent": "Third-party models (GPT-4o, Claude, Gemini) with web search.",
                "embed": "Text → vector embeddings for RAG / semantic search.",
            },
            "models": {
                "sonar": "Fast, cheap. Good for simple lookups.",
                "sonar-pro": "DEFAULT. Best balance of quality and speed.",
                "sonar-reasoning-pro": "Chain-of-thought reasoning with web grounding.",
                "sonar-deep-research": "Multi-step research. Thorough but slow.",
            },
            "tips": [
                "Use 'ask' for 90% of queries. Only use research/reason for complex ones.",
                "search_domain_filter restricts to specific sites — great for docs lookups.",
                "search_recency_filter='day' gets you today's news only.",
                "search_mode='academic' searches scholarly papers and journals.",
                "search_mode='sec' searches SEC filings (10-K, 10-Q, etc.).",
                "The agent tool lets you use GPT-4o/Claude/Gemini WITH web search.",
            ],
        }

    def ask(self, **kwargs) -> Any:
        question = kwargs.get("question", "")
        if not question:
            return {"error": "question is required"}
        return self._sonar(question, kwargs)

    def chat(self, **kwargs) -> Any:
        messages = kwargs.get("messages")
        if not messages:
            return {"error": "messages array is required"}
        return self._sonar_raw(messages, kwargs)

    def search(self, **kwargs) -> Any:
        query = kwargs.get("query", "")
        if not query:
            return {"error": "query is required"}

        body: dict[str, Any] = {"query": query}
        if kwargs.get("max_results"):
            body["max_results"] = min(int(kwargs["max_results"]), 20)
        for k in ("search_domain_filter", "search_language_filter"):
            v = kwargs.get(k)
            if v:
                body[k] = v if isinstance(v, list) else [v]
        if kwargs.get("search_recency_filter"):
            body["search_recency_filter"] = kwargs["search_recency_filter"]
        if kwargs.get("search_after_date"):
            body["search_after_date_filter"] = kwargs["search_after_date"]
        if kwargs.get("search_before_date"):
            body["search_before_date_filter"] = kwargs["search_before_date"]
        if kwargs.get("country"):
            body["country"] = kwargs["country"]

        data = _post("/search", body)
        results = data.get("results", [])
        return {
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": r.get("snippet"),
                    "date": r.get("date"),
                }
                for r in results
            ],
            "total": len(results),
        }

    def research(self, **kwargs) -> Any:
        question = kwargs.get("question", "")
        if not question:
            return {"error": "question is required"}
        kwargs["model"] = "sonar-deep-research"
        return self._sonar(question, kwargs, timeout=180)

    def reason(self, **kwargs) -> Any:
        question = kwargs.get("question", "")
        if not question:
            return {"error": "question is required"}
        kwargs["model"] = "sonar-reasoning-pro"
        return self._sonar(question, kwargs, timeout=120)

    def agent(self, **kwargs) -> Any:
        inp = kwargs.get("input", "")
        if not inp:
            return {"error": "input is required"}

        body: dict[str, Any] = {}
        if isinstance(inp, list):
            body["input"] = inp
        else:
            body["input"] = str(inp)

        if kwargs.get("model"):
            body["model"] = kwargs["model"]
        if kwargs.get("models"):
            body["models"] = kwargs["models"]
        if kwargs.get("preset"):
            body["preset"] = kwargs["preset"]
        if not body.get("model") and not body.get("models") and not body.get("preset"):
            body["preset"] = "pro-search"

        if kwargs.get("instructions"):
            body["instructions"] = kwargs["instructions"]
        if kwargs.get("max_output_tokens"):
            body["max_output_tokens"] = int(kwargs["max_output_tokens"])
        if kwargs.get("max_steps"):
            body["max_steps"] = min(int(kwargs["max_steps"]), 10)

        body["stream"] = False
        data = _post("/v1/agent", body, timeout=180)

        out: dict[str, Any] = {
            "id": data.get("id"),
            "model": data.get("model"),
            "status": data.get("status"),
        }
        output_items = data.get("output", [])
        for item in output_items:
            item_type = item.get("type")
            if item_type == "message":
                content = item.get("content", [])
                for c in content:
                    if c.get("type") == "output_text":
                        out["answer"] = c.get("text", "")
                        out["citations"] = c.get("citations") or []
            elif item_type == "search_results":
                out.setdefault("search_results", []).extend(
                    {"title": r.get("title"), "url": r.get("url")}
                    for r in item.get("results", [])
                )
        usage = data.get("usage")
        if usage:
            out["usage"] = usage
        return out

    def embed(self, **kwargs) -> Any:
        inp = kwargs.get("input", "")
        if not inp:
            return {"error": "input is required"}

        body: dict[str, Any] = {
            "input": inp if isinstance(inp, list) else [inp],
            "model": kwargs.get("model", "pplx-embed-v1-4b"),
        }
        if kwargs.get("dimensions"):
            body["dimensions"] = int(kwargs["dimensions"])

        data = _post("/v1/embeddings", body)
        return {
            "embeddings": [
                {"index": e.get("index"), "dimensions": len(e.get("embedding", ""))}
                for e in data.get("data", [])
            ],
            "model": data.get("model"),
            "usage": data.get("usage"),
        }

    # -- internal helpers --

    def _sonar(self, question: str, opts: dict, timeout: int = _TIMEOUT) -> dict:
        messages = [{"role": "user", "content": question}]
        return self._sonar_raw(messages, opts, timeout)

    def _sonar_raw(self, messages: list, opts: dict, timeout: int = _TIMEOUT) -> dict:
        model = opts.get("model", "sonar-pro")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        if opts.get("system_prompt"):
            body["messages"] = [
                {"role": "system", "content": opts["system_prompt"]},
                *messages,
            ]
        if opts.get("max_tokens"):
            body["max_tokens"] = int(opts["max_tokens"])
        if opts.get("temperature") is not None:
            body["temperature"] = float(opts["temperature"])
        if opts.get("top_p") is not None:
            body["top_p"] = float(opts["top_p"])
        if opts.get("search_mode"):
            body["search_mode"] = opts["search_mode"]
        for k in ("search_domain_filter", "search_language_filter"):
            v = opts.get(k)
            if v:
                body[k] = v if isinstance(v, list) else [v]
        if opts.get("search_recency_filter"):
            body["search_recency_filter"] = opts["search_recency_filter"]
        if opts.get("search_after_date_filter"):
            body["search_after_date_filter"] = opts["search_after_date_filter"]
        if opts.get("search_before_date_filter"):
            body["search_before_date_filter"] = opts["search_before_date_filter"]
        if opts.get("return_images"):
            body["return_images"] = True
        if opts.get("return_related_questions"):
            body["return_related_questions"] = True
        if opts.get("disable_search"):
            body["disable_search"] = True

        data = _post("/v1/sonar", body, timeout=timeout)
        return _clean_sonar_response(data)
