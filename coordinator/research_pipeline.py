"""Research pipeline — two-phase Argo DAG.

Phase 1: GATHERER (qwen3.5:9b)
  - Searches, reads, gathers information
  - Tools: web_search, web_read, wiki_read, run_command (NO write_file)
  - Ends when model responds with text (natural exit)
  - Output: text findings

Phase 2: WRITER (llama3.1:8b)
  - Receives gatherer's findings
  - Writes structured report to /workspace/report.md
  - Tools: write_file, read_file (NO web tools)
  - Output: report written

Orchestrated by the coordinator — each phase is a separate Argo task.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Workflow configs keyed by workflow name
WORKFLOW_CONFIG = {
    "research-quick": {
        "use_pipeline": False,  # Single researcher task, no pipeline
    },
    "research-regular": {
        "use_pipeline": True,
        "gather": {
            "model": "qwen3.5:9b",
            "max_iterations": 8,
            "tools": ["web_search", "web_read", "wiki_read", "run_command"],
        },
        "write": {
            "model": "llama3.1:8b",
            "max_iterations": 5,
            "tools": ["write_file", "read_file"],
        },
    },
    "research-deep": {
        "use_pipeline": True,
        "gather": {
            "model": "qwen3.5:9b",
            "max_iterations": 12,
            "tools": ["web_search", "web_read", "wiki_read", "run_command"],
        },
        "write": {
            "model": "llama3.1:8b",
            "max_iterations": 8,
            "tools": ["write_file", "read_file"],
        },
    },
}

# Deprecated: effort tier → workflow name mapping
_EFFORT_TO_WORKFLOW = {
    "light": "research-quick",
    "regular": "research-regular",
    "deep": "research-deep",
}


def resolve_workflow(workflow: str | None, effort: str | None) -> str | None:
    """Return canonical workflow name, accepting either workflow or deprecated effort.

    Returns None when neither is provided — caller runs agent directly, no pipeline.
    """
    if workflow:
        return workflow
    if effort:
        return _EFFORT_TO_WORKFLOW.get(effort, "research-regular")
    return None

GATHERER_PROMPT = """You are a research assistant. Your ONLY job is to search the web and gather information.

Use web_search to find relevant pages, then web_read to get their content.
For Wikipedia topics, use wiki_read (returns clean text, no HTML).

CRITICAL: Your training data is outdated. Search for current information. Trust web results over your memory.

When you have gathered enough information, respond with your findings as structured text:
- Key facts and data points
- Source URLs for each finding
- Any contradictions between sources

Do NOT write a report file. Do NOT use write_file. Just return your findings as text.
The report will be written by a separate agent after you finish."""

WRITER_PROMPT = """You are a report writer. Research has already been done — your job is to write it up.

The research findings are provided below. Write a structured report to /workspace/report.md using write_file.

Report format:
# Research: [Topic]

## Summary
2-3 opinionated sentences answering the research question.

## Findings
- Key finding 1 ([source](url))
- Key finding 2 ([source](url))

## Recommendation
What to do, ranked by priority.

## Sources
- [Title](url) — what this source provided

Write the report NOW. Do not search for more information. Use ONLY write_file and read_file."""



async def _wait_for_task(task_id: str, db, timeout: int = 600) -> str:
    """Poll task status until completion. Returns the result summary."""
    from common.models import TaskStatus

    elapsed = 0
    poll_interval = 3

    while elapsed < timeout:
        task = await db.kb.get_task(task_id)
        if not task:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        if task.status == TaskStatus.completed:
            result = task.result or {}
            return result.get("summary", "")

        if task.status == TaskStatus.failed:
            error = (task.result or {}).get("error", "unknown")
            log.warning("Task %s failed: %s", task_id[:8], error)
            return f"(Research failed: {error})"

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning("Task %s timed out after %ds", task_id[:8], timeout)
    return "(Research timed out)"
