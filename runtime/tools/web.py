"""Web tools — search, fetch, and read web content.

OpenClaude-inspired architecture:
- WebSearch: search only, returns titles + URLs + snippets (no page fetching)
- WebRead: fetches a URL with a prompt, secondary LLM extracts relevant info
- WikiRead: Wikipedia REST API for clean factual content

The search agent calls WebSearch to find pages.
The model (or a separate phase) calls WebRead with a focused prompt to extract specific info.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 30000
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen2.5:7b")
LLM_MANAGER_URL = os.environ.get("LLM_MANAGER_URL", "http://llm-manager-backend.llm-manager.svc:8081")
LLM_API_KEY = os.environ.get("LLM_MANAGER_API_KEY", "")


# ── Secondary LLM extraction ────────────────────────────────────────────────

async def _extract_with_llm(content: str, url: str, prompt: str = "") -> str:
    """Run page content through a small fast model with a focused prompt.

    Like OpenClaude's Haiku pass — the research model gets a targeted
    extraction, not raw page content.
    """
    if not LLM_API_KEY:
        log.debug("No LLM API key for extraction, returning raw content")
        return content[:MAX_OUTPUT_CHARS]

    extraction_prompt = prompt or (
        "Extract the key information from this web page. "
        "Focus on facts, data, comparisons, and actionable information. "
        "Remove navigation, ads, boilerplate, and repetitive content. "
        "Keep URLs/links that are cited as sources."
    )

    full_prompt = (
        f"{extraction_prompt}\n\n"
        f"URL: {url}\n\n"
        f"Page content:\n{content[:20000]}"
    )

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{LLM_MANAGER_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": SUMMARY_MODEL,
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": 2048,
                    "temperature": 0.1,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data["choices"][0]["message"].get("content", "")
                if result:
                    log.info("Extracted %d chars → %d chars via %s",
                             len(content), len(result), SUMMARY_MODEL)
                    return result
    except Exception as e:
        log.warning("LLM extraction failed: %s", e)

    return content[:MAX_OUTPUT_CHARS]


# ── Raw content fetching (internal, not a tool) ─────────────────────────────

async def _fetch_content(url: str) -> str:
    """Fetch and extract main content from a URL. Tries multiple methods."""
    for method_name, method_fn in [
        ("crawl4ai", _crawl4ai_fetch),
        ("trafilatura", _trafilatura_fetch),
        ("markdownify", _markdownify_fetch),
        ("basic", _basic_fetch),
    ]:
        try:
            content = await method_fn(url)
            if content and len(content) > 50:
                log.debug("Fetched %s via %s (%d chars)", url, method_name, len(content))
                return content
        except ImportError:
            continue
        except Exception as e:
            log.debug("%s failed for %s: %s", method_name, url, e)
            continue
    return ""


async def _crawl4ai_fetch(url: str) -> str:
    from crawl4ai import AsyncWebCrawler
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
    if not result.success:
        raise RuntimeError(f"crawl4ai: {result.error_message}")
    return result.markdown or ""


async def _trafilatura_fetch(url: str) -> str:
    import trafilatura
    downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
    if not downloaded:
        raise RuntimeError("fetch failed")
    text = await asyncio.to_thread(
        trafilatura.extract, downloaded,
        include_links=True, include_formatting=True,
        include_tables=True, output_format="txt",
    )
    if not text:
        raise RuntimeError("extraction failed")
    return text


async def _markdownify_fetch(url: str) -> str:
    from markdownify import markdownify as md
    proc = await asyncio.create_subprocess_exec(
        "curl", "-sL", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (compatible; Mycroft/1.0)",
        url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}")
    html = stdout.decode(errors="replace")
    html = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>',
                  '', html, flags=re.DOTALL | re.IGNORECASE)
    text = md(html, strip=['img', 'input', 'button', 'form', 'iframe'])
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def _basic_fetch(url: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "curl", "-sL", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (compatible; Mycroft/1.0)",
        url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
    if proc.returncode != 0:
        raise RuntimeError(f"curl: {stderr.decode()[:200]}")
    html = stdout.decode(errors="replace")
    text = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>',
                  '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── WebSearch tool ───────────────────────────────────────────────────────────

class WebSearch:
    """Search the web. Returns titles, URLs, and snippets ONLY.

    Does NOT fetch or read page content. Use web_read for that.
    """

    SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://mycroft-search.mycroft.svc:8080")

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web and return titles, URLs, and snippets. "
            "Does NOT read page content — use web_read for that. "
            "Snippets often contain the answer for simple questions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return "Error: query is required"

        max_results = int(args.get("max_results", 5))
        t0 = time.monotonic()

        params = f"q={query.replace(' ', '+')}&format=json&pageno=1"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sf", "--max-time", "15",
            f"{self.SEARXNG_URL}/search?{params}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("WebSearch timed out for: %s", query)
            return f"Search timed out for: {query}"

        if proc.returncode != 0:
            log.warning("WebSearch unavailable for: %s (exit %d)", query, proc.returncode)
            return f"Search failed for: {query} (search engine unavailable)"

        try:
            data = _json.loads(stdout.decode())
        except _json.JSONDecodeError:
            return f"Search returned invalid response for: {query}"

        results = []
        for r in data.get("results", [])[:max_results]:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:300]
            if title and url:
                results.append(f"- **{title}**\n  {url}\n  {snippet}")

        elapsed = time.monotonic() - t0
        total = data.get("number_of_results", len(results))
        log.info("WebSearch: '%s' → %d results (of %s) in %.1fs",
                 query, len(results), total, elapsed)

        if not results:
            return f"No results found for: {query}"

        return f"Search results for: {query} ({len(results)} results)\n\n" + "\n\n".join(results)


# ── WebRead tool ─────────────────────────────────────────────────────────────

class WebRead:
    """Fetch a URL and extract specific information using a prompt.

    Like OpenClaude's WebFetch — provide a prompt describing what you want
    and a secondary model will extract just that from the page.
    """

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "Fetch a web page and extract specific information from it. "
            "Provide a prompt describing what you want to know — a secondary "
            "model will read the page and return only the relevant content. "
            "Use web_search first to find URLs, then web_read to get details."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and read",
                },
                "prompt": {
                    "type": "string",
                    "description": "What to extract from the page (e.g. 'list the key features and pricing', 'how many movies are listed')",
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: url is required"

        prompt = args.get("prompt", "")
        t0 = time.monotonic()

        raw_content = await _fetch_content(url)
        if not raw_content:
            return f"Error: could not fetch content from {url}"

        # Always extract with LLM when prompt is given, or when content is large
        if prompt or len(raw_content) > 3000:
            result = await _extract_with_llm(raw_content, url, prompt)
            method = "extracted"
        else:
            result = raw_content
            method = "direct"

        elapsed = time.monotonic() - t0
        log.info("WebRead: %s → %s (%d→%d chars) in %.1fs",
                 url, method, len(raw_content), len(result), elapsed)

        return result


# ── WikiRead tool ────────────────────────────────────────────────────────────

class WikiRead:
    """Fetch a Wikipedia article via the REST API. Returns clean text."""

    @property
    def name(self) -> str:
        return "wiki_read"

    @property
    def description(self) -> str:
        return (
            "Get information from Wikipedia. Provide a prompt to extract specific "
            "facts (e.g. 'how many films are listed'). A secondary model reads the "
            "article and returns only the relevant answer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The Wikipedia article topic (e.g. 'Tom Hanks', 'List of Tom Hanks performances')",
                },
                "prompt": {
                    "type": "string",
                    "description": "What to extract (e.g. 'how many films total', 'what is the population'). A secondary model answers this from the article.",
                },
                "full": {
                    "type": "boolean",
                    "description": "If true, fetch the full article (more data but slower). Default: summary only.",
                },
            },
            "required": ["topic"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        topic = args.get("topic", "")
        if not topic:
            return "Error: topic is required"

        prompt = args.get("prompt", "")
        full = args.get("full", False)
        # Auto-upgrade to full when a specific prompt is given
        if prompt and not full:
            full = True
        t0 = time.monotonic()
        encoded = topic.replace(" ", "_")

        try:
            if full:
                extract_url = (
                    f"https://en.wikipedia.org/w/api.php?action=query&titles={encoded}"
                    f"&prop=extracts&explaintext=1&format=json"
                )
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", extract_url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                data = _json.loads(stdout.decode())
                pages = data.get("query", {}).get("pages", {})
                raw_content = ""
                for page_id, page in pages.items():
                    if page.get("extract"):
                        raw_content = f"# {page.get('title', topic)}\n\n{page['extract']}"
                        break
                if not raw_content:
                    return f"Wikipedia article not found for: {topic}"
            else:
                url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                data = _json.loads(stdout.decode())

                if data.get("type") == "disambiguation":
                    return f"'{topic}' is a disambiguation page. Try a more specific topic."

                desc = f"_{data['description']}_\n\n" if data.get("description") else ""
                raw_content = f"# {data.get('title', topic)}\n{desc}{data.get('extract', '')}"

        except Exception as e:
            log.warning("WikiRead failed for '%s': %s", topic, e)
            return f"Wikipedia article not found for: {topic}"

        # Run through secondary LLM when prompt is given or content is large
        if prompt or len(raw_content) > 3000:
            result = await _extract_with_llm(
                raw_content,
                f"https://en.wikipedia.org/wiki/{encoded}",
                prompt,
            )
            method = "extracted"
        else:
            result = raw_content
            method = "direct"

        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n\n... (truncated)"

        elapsed = time.monotonic() - t0
        log.info("WikiRead: '%s' (full=%s) → %d chars in %.1fs",
                 topic, full, len(result), elapsed)

        return result
