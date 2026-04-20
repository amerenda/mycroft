"""Web tools — fetch URLs as clean markdown for research agents."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 30000


class WebRead:
    """Fetch a URL and return its content as clean markdown.

    Uses crawl4ai for HTML-to-markdown conversion. Falls back to
    raw HTTP + basic stripping if crawl4ai is not installed.
    """

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL and return its content as clean, readable markdown. "
            "Handles JavaScript-rendered pages. Use this instead of curl for "
            "reading web pages — it strips navigation, ads, and HTML noise."
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
        url = args["url"]
        log.info("WebRead: %s", url)

        try:
            return await self._crawl4ai_fetch(url)
        except ImportError:
            log.info("crawl4ai not installed, falling back to basic fetch")
            return await self._basic_fetch(url)
        except Exception as e:
            log.warning("crawl4ai failed, falling back to basic: %s", e)
            return await self._basic_fetch(url)

    async def _crawl4ai_fetch(self, url: str) -> str:
        """Fetch using crawl4ai for clean markdown output."""
        from crawl4ai import AsyncWebCrawler

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)

        if not result.success:
            return f"Error fetching {url}: {result.error_message}"

        md = result.markdown or ""
        if len(md) > MAX_OUTPUT_CHARS:
            md = md[:MAX_OUTPUT_CHARS] + f"\n\n... (truncated, {len(result.markdown)} total chars)"

        return md or f"(page fetched but no content extracted from {url})"

    async def _basic_fetch(self, url: str) -> str:
        """Fallback: fetch with curl and do basic HTML stripping."""
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sL", "--max-time", "30", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)

        if proc.returncode != 0:
            return f"Error fetching {url}: {stderr.decode()[:200]}"

        html = stdout.decode(errors="replace")

        # Basic HTML stripping — remove tags, decode entities
        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n... (truncated)"

        return text or f"(no content extracted from {url})"


class WebSearch:
    """Search the web using SearXNG (self-hosted) and return results."""

    # SearXNG runs in the same namespace as the agent
    SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng.mycroft.svc:8080")

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
        query = args["query"]
        max_results = args.get("max_results", 5)
        log.info("WebSearch: %s (max=%d)", query, max_results)

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
            return f"Search timed out for: {query}"

        if proc.returncode != 0:
            return f"Search failed for: {query} (SearXNG unavailable)"

        try:
            data = _json.loads(stdout.decode())
        except _json.JSONDecodeError:
            return f"Search returned invalid response for: {query}"

        results = []
        for r in data.get("results", [])[:max_results]:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200]
            if title and url:
                results.append(f"- [{title}]({url})\n  {snippet}")

        if not results:
            return f"No results found for: {query}"

        return f"Search results for: {query}\n\n" + "\n\n".join(results)
