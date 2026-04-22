"""Baseline searcher test harness.

Calls SearXNG directly (raw JSON) and WebSearch.execute() (what the LLM sees)
for a set of varied queries. Shows what info is gained/lost in formatting.

Run:
    cd ~/claude/projects/mycroft
    SEARXNG_URL=http://10.43.55.167:8080 python -m pytest tests/test_searcher_baseline.py -s -v
or:
    SEARXNG_URL=http://10.43.55.167:8080 python tests/test_searcher_baseline.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time

# Make sure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://10.43.55.167:8080")

# Varied queries: factual, technical, recent, ambiguous, multi-word
TEST_QUERIES = [
    "kube-proxy vs Cilium CNI performance 2025",
    "how many films has Tom Hanks appeared in",
    "Longhorn distributed storage kubernetes setup",
    "qwen3 model ollama tool calling",
    "home assistant zigbee2mqtt z-wave",
]


def raw_searxng(query: str, max_results: int = 10) -> dict:
    """Call SearXNG directly and return full JSON."""
    params = f"q={query.replace(' ', '+')}&format=json&pageno=1"
    proc = subprocess.run(
        ["curl", "-sf", "--max-time", "15", f"{SEARXNG_URL}/search?{params}"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return {"error": f"curl failed: {proc.returncode}", "results": []}
    try:
        return json.loads(proc.stdout.decode())
    except json.JSONDecodeError as e:
        return {"error": str(e), "results": []}


def analyze_raw(data: dict, max_results: int = 10) -> dict:
    """Extract stats from raw SearXNG response."""
    results = data.get("results", [])[:max_results]
    engines_per_result = [r.get("engines", [r.get("engine", "?")]) for r in results]
    if isinstance(engines_per_result[0], str) if engines_per_result else False:
        engines_per_result = [[e] for e in engines_per_result]

    all_engines = {e for engines in engines_per_result for e in engines}
    snippet_lengths = [len(r.get("content", "")) for r in results]
    has_date = sum(1 for r in results if r.get("publishedDate") or r.get("parsed_url"))

    return {
        "total_reported": data.get("number_of_results", "?"),
        "returned": len(results),
        "engines_seen": sorted(all_engines),
        "snippet_len_min": min(snippet_lengths) if snippet_lengths else 0,
        "snippet_len_max": max(snippet_lengths) if snippet_lengths else 0,
        "snippet_len_avg": int(sum(snippet_lengths) / len(snippet_lengths)) if snippet_lengths else 0,
        "has_date_fields": has_date,
        "results": [
            {
                "title": r.get("title", "")[:80],
                "url": r.get("url", ""),
                "snippet_full": r.get("content", ""),
                "snippet_300": r.get("content", "")[:300],
                "engines": r.get("engines", [r.get("engine", "?")]),
                "score": r.get("score"),
                "publishedDate": r.get("publishedDate"),
            }
            for r in results
        ],
    }


async def run_web_search_tool(query: str, max_results: int = 5) -> str:
    """Run the actual WebSearch tool (what the LLM sees)."""
    from runtime.tools.web import WebSearch
    ws = WebSearch()
    return await ws.execute({"query": query, "max_results": max_results})


def print_separator(char="─", width=80):
    print(char * width)


def run_baseline():
    print("\n" + "=" * 80)
    print("MYCROFT SEARCHER BASELINE TEST")
    print(f"SearXNG: {SEARXNG_URL}")
    print("=" * 80)

    for query in TEST_QUERIES:
        print_separator("═")
        print(f"QUERY: {query!r}")
        print_separator("═")

        # --- Raw SearXNG ---
        t0 = time.monotonic()
        raw = raw_searxng(query, max_results=10)
        elapsed_raw = time.monotonic() - t0

        if "error" in raw:
            print(f"  RAW ERROR: {raw['error']}")
            continue

        stats = analyze_raw(raw, max_results=10)
        print(f"\n[RAW SEARXNG] ({elapsed_raw:.1f}s)")
        print(f"  Total reported: {stats['total_reported']}")
        print(f"  Results returned: {stats['returned']}")
        print(f"  Engines: {', '.join(stats['engines_seen'])}")
        print(f"  Snippet lengths: min={stats['snippet_len_min']} avg={stats['snippet_len_avg']} max={stats['snippet_len_max']} chars")
        print(f"  Results with date: {stats['has_date_fields']}")

        print(f"\n  Top 5 results (raw):")
        for i, r in enumerate(stats["results"][:5], 1):
            engines = r["engines"] if isinstance(r["engines"], list) else [r["engines"]]
            snippet = r["snippet_full"]
            truncated = "(FULL)" if len(snippet) <= 300 else f"(TRUNCATED at 300/{len(snippet)})"
            print(f"  {i}. [{', '.join(str(e) for e in engines)}] {r['title']}")
            print(f"     {r['url'][:90]}")
            print(f"     Snippet {truncated}: {snippet[:300]!r}")
            if r.get("publishedDate"):
                print(f"     Date: {r['publishedDate']}")
            print()

        # --- WebSearch tool output (what LLM sees) ---
        t0 = time.monotonic()
        llm_output = asyncio.run(run_web_search_tool(query, max_results=5))
        elapsed_tool = time.monotonic() - t0

        print(f"[LLM VIEW — WebSearch.execute()] ({elapsed_tool:.1f}s)")
        print(f"  Output length: {len(llm_output)} chars")
        print()
        for line in llm_output.split("\n"):
            print(f"  {line}")
        print()

        # --- Delta analysis ---
        print("[DELTA ANALYSIS]")
        full_snippets = [r["snippet_full"] for r in stats["results"][:5]]
        trunc_snippets = [r["snippet_300"] for r in stats["results"][:5]]
        chars_dropped = sum(len(f) - len(t) for f, t in zip(full_snippets, trunc_snippets))
        print(f"  Chars dropped by 300-char truncation (top 5): {chars_dropped}")
        print(f"  Engines available but not shown to LLM: {', '.join(stats['engines_seen'])}")
        print(f"  Dates available: {stats['has_date_fields']} results (not shown to LLM)")
        print()

    print_separator("═")
    print("DONE")
    print_separator("═")


if __name__ == "__main__":
    run_baseline()
