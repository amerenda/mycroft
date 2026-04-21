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
from typing import Any

log = logging.getLogger(__name__)

# Phase configs by effort tier
PHASE_CONFIG = {
    "light": {
        # Light skips the pipeline — single task, no report
        "use_pipeline": False,
    },
    "regular": {
        "use_pipeline": True,
        "gather": {
            "model": "qwen3.5:9b",
            "max_iterations": 5,
            "tools": ["web_search", "web_read", "wiki_read", "run_command"],
        },
        "write": {
            "model": "llama3.1:8b",
            "max_iterations": 3,
            "tools": ["write_file", "read_file"],
        },
    },
    "deep": {
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
}

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


async def run_research_pipeline(
    instruction: str,
    effort: str,
    task_manager,
    argo,
    db,
    on_update,
    config: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Run the two-phase research pipeline.

    Returns (task_id, report_content) where report_content is None if
    the pipeline hasn't completed yet (async Argo execution).
    """
    phase_config = PHASE_CONFIG.get(effort or "regular", PHASE_CONFIG["regular"])

    if not phase_config.get("use_pipeline"):
        # Light tier — no pipeline, return None to use normal single-task flow
        return "", None

    gather_cfg = phase_config["gather"]
    write_cfg = phase_config["write"]

    # Phase 1: Create gatherer task
    gather_config = {
        "instruction": instruction,
        "model_override": gather_cfg["model"],
        "max_iterations_override": gather_cfg["max_iterations"],
        "tools_override": gather_cfg["tools"],
        "system_prompt_override": GATHERER_PROMPT,
        "phase": "gather",
    }

    gather_task_id = await task_manager.create_task(
        agent_type="researcher",
        instruction=instruction,
        trigger="pipeline",
        repo="",
        config=gather_config,
    )

    log.info("Research pipeline: gather task %s (model=%s, iter=%d)",
             gather_task_id[:8], gather_cfg["model"], gather_cfg["max_iterations"])

    # Submit to Argo
    try:
        await argo.submit(
            agent_type="researcher",
            task_id=gather_task_id,
            params={
                "instruction": instruction,
                "model_override": gather_cfg["model"],
            },
            on_update=on_update,
        )
    except Exception as e:
        log.error("Failed to submit gather task: %s", e)
        raise

    # Wait for gatherer to complete
    findings = await _wait_for_task(gather_task_id, db, timeout=600)

    if not findings:
        log.warning("Gatherer produced no findings for: %s", instruction[:80])
        findings = f"(No findings gathered for: {instruction})"

    log.info("Research pipeline: gatherer done (%d chars). Starting writer.", len(findings))

    # Phase 2: Create writer task with findings injected
    writer_instruction = (
        f"Write a research report based on these findings.\n\n"
        f"Original question: {instruction}\n\n"
        f"Research findings:\n{findings[:15000]}"
    )

    write_config = {
        "instruction": writer_instruction,
        "model_override": write_cfg["model"],
        "max_iterations_override": write_cfg["max_iterations"],
        "tools_override": write_cfg["tools"],
        "system_prompt_override": WRITER_PROMPT,
        "phase": "write",
        "parent_task_id": gather_task_id,
    }

    write_task_id = await task_manager.create_task(
        agent_type="researcher",
        instruction=writer_instruction,
        trigger="pipeline",
        repo="",
        config=write_config,
    )

    log.info("Research pipeline: writer task %s (model=%s, iter=%d)",
             write_task_id[:8], write_cfg["model"], write_cfg["max_iterations"])

    # Submit to Argo
    try:
        await argo.submit(
            agent_type="researcher",
            task_id=write_task_id,
            params={
                "instruction": writer_instruction,
                "model_override": write_cfg["model"],
            },
            on_update=on_update,
        )
    except Exception as e:
        log.error("Failed to submit writer task: %s", e)
        raise

    return write_task_id, None  # Async — coordinator will get notified on completion


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
