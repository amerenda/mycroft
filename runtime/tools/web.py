"""Web tools — search, fetch, and read web content.

- WebSearch: search only, returns titles + URLs + snippets
- WebRead: fetches a URL and returns cleaned page content
- WikiRead: Wikipedia REST API for clean factual content

Tools are dumb — they fetch and return content. The agent does the reading.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import time
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 30000


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


# ── Search result filtering ──────────────────────────────────────────────────

# Domains that consistently produce off-topic noise in research queries.
# These appear via single-engine (usually Bing) results that don't relate
# to the query at all — login pages, dictionary definitions, unrelated products.
_NOISE_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com",
    "hbomax.com",
    "help.hbomax.com",
    "baidu.com",
    "zhidao.baidu.com",
    "merriam-webster.com",
    "dictionary.com",
    "facebook.com",
    "bestbuy.com",
    "amazon.com",    # catches shopping noise; search won't surface it for tech queries anyway
})

# Minimum SearXNG score to include a result. Results below this threshold
# are typically single-engine noise with no corroboration.
_MIN_SCORE = float(os.environ.get("SEARXNG_MIN_SCORE", "0.3"))


def _is_noise(result: dict) -> bool:
    """Return True if a SearXNG result should be filtered out."""
    url = result.get("url", "")
    try:
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return False

    if any(host == d or host.endswith("." + d) for d in _NOISE_DOMAINS):
        return True

    score = result.get("score", 1.0)
    if score < _MIN_SCORE:
        return True

    return False


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

        # Fetch more from SearXNG than needed so filtering doesn't leave us short
        fetch_n = max(max_results * 3, 15)
        qs = urllib.parse.urlencode({"q": query, "format": "json", "pageno": 1})
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sf", "--max-time", "15",
            f"{self.SEARXNG_URL}/search?{qs}",
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

        raw = data.get("results", [])
        filtered = [r for r in raw if not _is_noise(r)]
        dropped = len(raw) - len(filtered)
        if dropped:
            log.info("WebSearch: filtered %d noise results for: %s", dropped, query)

        results = []
        seen_urls: set[str] = set()
        for r in filtered:
            if len(results) >= max_results:
                break
            title = r.get("title", "")
            url = r.get("url", "")
            if not title or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            snippet = r.get("content", "")[:400]
            date = r.get("publishedDate", "")
            date_str = f" [{date[:10]}]" if date else ""
            results.append(f"- **{title}**{date_str}\n  {url}\n  {snippet}")

        elapsed = time.monotonic() - t0
        log.info("WebSearch: '%s' → %d results (dropped %d noise) in %.1fs",
                 query, len(results), dropped, elapsed)

        if not results:
            return f"No results found for: {query}"

        return f"Search results for: {query} ({len(results)} results)\n\n" + "\n\n".join(results)


# ── WebRead tool ─────────────────────────────────────────────────────────────

class WebRead:
    """Fetch a URL and return its cleaned text content."""

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "Fetch a web page and return its cleaned text content. "
            "Use web_search first to find URLs, then web_read to get the full content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch",
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: url is required"

        t0 = time.monotonic()
        content = await _fetch_content(url)
        if not content:
            return f"Error: could not fetch content from {url}"

        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n\n... (truncated)"

        elapsed = time.monotonic() - t0
        log.info("WebRead: %s → %d chars in %.1fs", url, len(content), elapsed)
        return content


# ── WikiRead tool ────────────────────────────────────────────────────────────

class WikiRead:
    """Fetch a Wikipedia article via the REST API. Returns clean text."""

    @property
    def name(self) -> str:
        return "wiki_read"

    @property
    def description(self) -> str:
        return (
            "Get information from Wikipedia. Returns clean article text. "
            "Use full=true to get the complete article instead of just the summary."
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
                "full": {
                    "type": "boolean",
                    "description": "If true, fetch the full article. Default: summary only.",
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
                content = ""
                for page_id, page in pages.items():
                    if page.get("extract"):
                        content = f"# {page.get('title', topic)}\n\n{page['extract']}"
                        break
                if not content:
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
                content = f"# {data.get('title', topic)}\n{desc}{data.get('extract', '')}"

        except Exception as e:
            log.warning("WikiRead failed for '%s': %s", topic, e)
            return f"Wikipedia article not found for: {topic}"

        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n\n... (truncated)"

        elapsed = time.monotonic() - t0
        log.info("WikiRead: '%s' (full=%s) → %d chars in %.1fs", topic, full, len(content), elapsed)
        return content
