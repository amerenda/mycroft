"""Web tools — fetch URLs as clean markdown, search via SearXNG."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 30000


class WebRead:
    """Fetch a URL and return its content as clean markdown.

    Extraction priority:
    1. crawl4ai (headless browser + markdown conversion)
    2. trafilatura (content extraction from raw HTML, no browser)
    3. basic regex stripping (last resort)
    """

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL and return its content as clean, readable markdown. "
            "Strips navigation, ads, and HTML noise. Use this instead of curl "
            "for reading web pages."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and convert to markdown",
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

        try:
            result = await self._crawl4ai_fetch(url)
            method = "crawl4ai"
        except ImportError:
            try:
                result = await self._trafilatura_fetch(url)
                method = "trafilatura"
            except ImportError:
                result = await self._basic_fetch(url)
                method = "basic"
        except Exception as e:
            log.warning("crawl4ai failed for %s: %s", url, e)
            try:
                result = await self._trafilatura_fetch(url)
                method = "trafilatura"
            except ImportError:
                result = await self._basic_fetch(url)
                method = "basic"
            except Exception as e2:
                log.warning("trafilatura also failed for %s: %s", url, e2)
                result = await self._basic_fetch(url)
                method = "basic"

        elapsed = time.monotonic() - t0
        content_len = len(result)
        log.info("WebRead: %s via %s → %d chars in %.1fs", url, method, content_len, elapsed)

        return result

    async def _crawl4ai_fetch(self, url: str) -> str:
        """Fetch using crawl4ai (headless browser + markdown)."""
        from crawl4ai import AsyncWebCrawler

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)

        if not result.success:
            raise RuntimeError(f"crawl4ai error: {result.error_message}")

        md = result.markdown or ""
        if len(md) > MAX_OUTPUT_CHARS:
            md = md[:MAX_OUTPUT_CHARS] + f"\n\n... (truncated, {len(result.markdown)} total chars)"

        return md or f"(page fetched but no content extracted from {url})"

    async def _trafilatura_fetch(self, url: str) -> str:
        """Fetch using trafilatura (content extraction, no browser)."""
        import trafilatura

        # Fetch the page
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if not downloaded:
            raise RuntimeError(f"trafilatura could not fetch {url}")

        # Extract main content as markdown-like text
        text = await asyncio.to_thread(
            trafilatura.extract,
            downloaded,
            include_links=True,
            include_formatting=True,
            include_tables=True,
            output_format="txt",
        )

        if not text:
            raise RuntimeError(f"trafilatura extracted no content from {url}")

        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n\n... (truncated)"

        return text

    async def _basic_fetch(self, url: str) -> str:
        """Last resort: curl + regex HTML stripping."""
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL", "--max-time", "30",
            "-H", "User-Agent: Mozilla/5.0 (compatible; Mycroft/1.0)",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)

        if proc.returncode != 0:
            return f"Error fetching {url}: {stderr.decode()[:200]}"

        html = stdout.decode(errors="replace")

        # Strip scripts, styles, nav, footer, header
        text = re.sub(r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Remove very short "pages" that are likely error pages
        if len(text) < 50:
            return f"(no useful content extracted from {url})"

        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n... (truncated)"

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

        import json as _json

        # Query SearXNG JSON API
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
