"""Web tools — fetch URLs as clean markdown, search via SearXNG.

Extraction priority for web_read:
1. crawl4ai (headless browser + markdown) — best for JS-rendered pages
2. trafilatura (content extraction from raw HTML) — best for articles
3. markdownify (HTML → markdown conversion) — good general fallback
4. basic regex stripping — last resort

After extraction, content is optionally summarized through a secondary
LLM (like OpenClaude's Haiku pass) to extract only the relevant info
for the research question, keeping context windows manageable.
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
SUMMARIZE_THRESHOLD = 5000  # Summarize content longer than this
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen2.5:7b")
LLM_MANAGER_URL = os.environ.get("LLM_MANAGER_URL", "http://llm-manager-backend.llm-manager.svc:8081")
LLM_API_KEY = os.environ.get("LLM_MANAGER_API_KEY", "")


async def _summarize_with_llm(content: str, url: str, context: str = "") -> str:
    """Run content through a small fast model to extract key information.

    Like OpenClaude's Haiku pass — the main research model never sees
    raw 30K-char pages. It gets a focused summary instead.
    """
    if not LLM_API_KEY:
        # No API key available, return raw content truncated
        log.debug("No LLM API key for summarization, returning raw content")
        return content[:MAX_OUTPUT_CHARS]

    prompt = (
        f"Extract the key information from this web page.\n"
        f"URL: {url}\n\n"
        f"Focus on facts, data, comparisons, and actionable information. "
        f"Remove navigation, ads, boilerplate, and repetitive content. "
        f"Keep URLs/links that are cited as sources. "
        f"Return clean, structured text — NOT HTML.\n\n"
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
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2048,
                    "temperature": 0.1,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                summary = data["choices"][0]["message"].get("content", "")
                if summary:
                    log.info("Summarized %d chars → %d chars via %s", len(content), len(summary), SUMMARY_MODEL)
                    return summary

    except Exception as e:
        log.warning("LLM summarization failed: %s", e)

    # Fallback: return raw content truncated
    return content[:MAX_OUTPUT_CHARS]


class WebRead:
    """Fetch a URL and return its content as clean, summarized text.

    Extraction: crawl4ai → trafilatura → markdownify → basic regex
    Then: summarized through a secondary LLM if content is large.
    """

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL and return its content as clean, readable text. "
            "Strips navigation, ads, and noise. Large pages are automatically "
            "summarized to extract key information. Use this for reading "
            "web pages, articles, and documentation."
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
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: url is required"

        t0 = time.monotonic()
        method = "unknown"
        raw_content = ""

        # Try extraction methods in priority order
        for extractor_name, extractor_fn in [
            ("crawl4ai", self._crawl4ai_fetch),
            ("trafilatura", self._trafilatura_fetch),
            ("markdownify", self._markdownify_fetch),
            ("basic", self._basic_fetch),
        ]:
            try:
                raw_content = await extractor_fn(url)
                method = extractor_name
                break
            except ImportError:
                continue
            except Exception as e:
                log.debug("%s failed for %s: %s", extractor_name, url, e)
                continue

        if not raw_content:
            return f"Error: could not fetch content from {url}"

        # Summarize large content through secondary LLM
        if len(raw_content) > SUMMARIZE_THRESHOLD:
            result = await _summarize_with_llm(raw_content, url)
            method += "+summarized"
        else:
            result = raw_content

        elapsed = time.monotonic() - t0
        log.info("WebRead: %s via %s → %d chars (raw=%d) in %.1fs",
                 url, method, len(result), len(raw_content), elapsed)

        return result

    async def _crawl4ai_fetch(self, url: str) -> str:
        from crawl4ai import AsyncWebCrawler
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
        if not result.success:
            raise RuntimeError(f"crawl4ai: {result.error_message}")
        return result.markdown or ""

    async def _trafilatura_fetch(self, url: str) -> str:
        import trafilatura
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if not downloaded:
            raise RuntimeError("trafilatura: fetch failed")
        text = await asyncio.to_thread(
            trafilatura.extract, downloaded,
            include_links=True, include_formatting=True,
            include_tables=True, output_format="txt",
        )
        if not text:
            raise RuntimeError("trafilatura: extraction failed")
        return text

    async def _markdownify_fetch(self, url: str) -> str:
        from markdownify import markdownify as md
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL", "--max-time", "30",
            "-H", "User-Agent: Mozilla/5.0 (compatible; Mycroft/1.0)",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
        if proc.returncode != 0:
            raise RuntimeError(f"curl failed: exit {proc.returncode}")
        html = stdout.decode(errors="replace")
        # Strip scripts/styles before converting
        html = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = md(html, strip=['img', 'input', 'button', 'form', 'iframe'])
        # Clean up excessive whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def _basic_fetch(self, url: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL", "--max-time", "30",
            "-H", "User-Agent: Mozilla/5.0 (compatible; Mycroft/1.0)",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
        if proc.returncode != 0:
            raise RuntimeError(f"curl: {stderr.decode()[:200]}")
        html = stdout.decode(errors="replace")
        text = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) < 50:
            raise RuntimeError("no useful content")
        return text


class WebSearch:
    """Search the web using SearXNG (self-hosted) and return results."""

    SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://mycroft-search.mycroft.svc:8080")

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for a query and return the top results with titles, "
            "URLs, and snippets. Use this to find relevant pages before using "
            "web_read to fetch their full content."
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
            log.warning("WebSearch SearXNG unavailable for: %s (exit %d)", query, proc.returncode)
            return f"Search failed for: {query} (SearXNG unavailable)"

        try:
            data = _json.loads(stdout.decode())
        except _json.JSONDecodeError:
            log.warning("WebSearch invalid response for: %s", query)
            return f"Search returned invalid response for: {query}"

        results = []
        for r in data.get("results", [])[:max_results]:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200]
            if title and url:
                results.append(f"- [{title}]({url})\n  {snippet}")

        elapsed = time.monotonic() - t0
        log.info("WebSearch: '%s' → %d results in %.1fs", query, len(results), elapsed)

        if not results:
            return f"No results found for: {query}"

        return f"Search results for: {query}\n\n" + "\n\n".join(results)


class WikiRead:
    """Fetch a Wikipedia article summary or full sections via the REST API."""

    @property
    def name(self) -> str:
        return "wiki_read"

    @property
    def description(self) -> str:
        return (
            "Get clean text from Wikipedia. Returns the article summary by default. "
            "Use this instead of web_read for Wikipedia — it returns structured text, "
            "not HTML. Pass a topic name, not a URL."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The Wikipedia article topic (e.g. 'Tom Hanks', 'Python (programming language)')",
                },
                "full": {
                    "type": "boolean",
                    "description": "If true, return the full article intro + sections. Default: summary only.",
                },
            },
            "required": ["topic"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        topic = args.get("topic", "")
        if not topic:
            return "Error: topic is required"

        full = args.get("full", False)
        t0 = time.monotonic()

        # URL-encode the topic
        encoded = topic.replace(" ", "_")

        try:
            if full:
                # Get summary + search for related sections
                summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", summary_url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                data = _json.loads(stdout.decode())
                result = f"# {data.get('title', topic)}\n\n{data.get('extract', '')}"

                # Also get the full article text via the TextExtracts API
                extract_url = (
                    f"https://en.wikipedia.org/w/api.php?action=query&titles={encoded}"
                    f"&prop=extracts&explaintext=1&format=json"
                )
                proc2 = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", extract_url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=15)
                data2 = _json.loads(stdout2.decode())
                pages = data2.get("query", {}).get("pages", {})
                for page_id, page in pages.items():
                    if page.get("extract"):
                        result = f"# {page.get('title', topic)}\n\n{page['extract']}"
                        break
            else:
                # Summary only — clean, fast
                url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-sf", "--max-time", "10", url,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                data = _json.loads(stdout.decode())

                if data.get("type") == "disambiguation":
                    return f"'{topic}' is a disambiguation page. Try a more specific topic."

                result = f"# {data.get('title', topic)}\n\n{data.get('extract', '')}"
                if data.get("description"):
                    result = f"# {data.get('title', topic)}\n_{data['description']}_\n\n{data.get('extract', '')}"

        except Exception as e:
            log.warning("WikiRead failed for '%s': %s", topic, e)
            return f"Wikipedia article not found for: {topic}"

        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n\n... (truncated)"

        elapsed = time.monotonic() - t0
        log.info("WikiRead: '%s' (full=%s) → %d chars in %.1fs", topic, full, len(result), elapsed)

        return result
