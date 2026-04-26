"""Research pipeline — two-phase Argo DAG.

Phase 1: GATHERER (qwen3.5:9b)
  - Searches, reads, gathers information
  - Tools: web_search, web_read, wiki_read, run_command (NO write_file)
  - Ends when model responds with text (natural exit)
  - Output: text findings

Phase 2: WRITER (llama3.1:8b)
  - Receives gatherer's findings
  - Outputs the structured report as its final text response (no file tools)
  - Output: report content returned directly to coordinator

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
            "tools": [],  # writer outputs text directly — no file tools needed
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
            "tools": [],  # writer outputs text directly — no file tools needed
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

The research findings are in the conversation below. Write a structured report and output it as your response.

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

Output the FULL report as your response now. Do not use any tools. Do not search for more information."""



class _TaskFailed(Exception):
    pass


async def _wait_for_task(task_id: str, db, timeout: int = 600) -> str:
    """Poll task status until completion. Returns the result summary.

    Raises _TaskFailed if the task permanently fails (stayed failed >3 min,
    meaning Argo retries are exhausted). Transient failures (pod crash + Argo
    retry) reset the failed timer when the status goes back to running.
    """
    from common.models import TaskStatus

    elapsed = 0
    poll_interval = 3
    failed_since: float | None = None

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
            if failed_since is None:
                failed_since = elapsed
                log.warning("Task %s failed (waiting to see if Argo retries)...", task_id[:8])
            elif elapsed - failed_since > 180:
                error = (task.result or {}).get("error", "unknown")
                raise _TaskFailed(f"Task {task_id[:8]} permanently failed: {error}")
        else:
            if failed_since is not None:
                log.info("Task %s recovered from failure (Argo retry), resuming wait", task_id[:8])
            failed_since = None

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise _TaskFailed(f"Task {task_id[:8]} timed out after {timeout}s")
