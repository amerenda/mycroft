"""Coordinator — FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from common.config import PlatformConfig
from common.llm import LLMClient
from common.metrics import (
    coordinator_info, tasks_created_total, tasks_completed_total,
    tasks_active, task_duration_seconds, argo_submissions_total,
    telegram_messages_total, intent_classifications_total,
    llm_metrics_callback,
)
from common.models import IntentType, TaskConfig, TaskStatus
from coordinator.argo_submitter import ArgoSubmitter
from coordinator.db import CoordinatorDB
from coordinator.task_manager import TaskManager
from coordinator.telegram import TelegramBot
from coordinator.trigger_router import TriggerRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("coordinator")

# ---------------------------------------------------------------------------
# Global state (initialized in lifespan)
# ---------------------------------------------------------------------------

config: PlatformConfig
db: CoordinatorDB
task_manager: TaskManager
argo: ArgoSubmitter
telegram_bot: TelegramBot
trigger_router: TriggerRouter
llm: LLMClient


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _llm_heartbeat_loop(llm_url: str, api_key: str):
    """Send periodic heartbeat to llm-manager to stay 'online'."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await asyncio.sleep(60)
                await client.post(
                    f"{llm_url}/api/apps/heartbeat",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"metadata": {"component": "coordinator"}},
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("LLM heartbeat failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, db, task_manager, argo, telegram_bot, trigger_router, llm

    config = PlatformConfig()

    # Database
    db = CoordinatorDB(config.kb_dsn)
    await db.connect()

    # Task manager
    task_manager = TaskManager(db.kb)

    # LLM: discover API key from llm-manager (same pattern as ecdysis)
    llm_api_key = config.llm_manager_api_key
    if config.llm_registration_secret:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{config.llm_manager_url}/api/apps/discover",
                    json={
                        "name": "mycroft-coordinator",
                        "base_url": "",
                        "registration_secret": config.llm_registration_secret,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                llm_api_key = data.get("api_key", "")
                log.info("Discovered LLM API key from llm-manager (key=%s...)", llm_api_key[:8])
        except Exception as e:
            log.error("Failed to discover LLM API key: %s", e)

    llm = LLMClient(config.llm_manager_url, llm_api_key, config.intent_model)
    llm.set_metrics_callback(llm_metrics_callback)
    coordinator_info.info({"version": config.agent_image_tag})

    # Trigger router
    trigger_router = TriggerRouter()
    agents_dir = Path(__file__).resolve().parent.parent / "agents"
    trigger_router.load_manifests(agents_dir)

    # Argo submitter
    argo = ArgoSubmitter(
        namespace=config.argo_namespace,
        image_repo=config.agent_image_repo,
        image_tag=config.agent_image_tag,
        llm_manager_url=config.llm_manager_url,
    )

    # Telegram bot
    telegram_bot = TelegramBot(
        token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        llm=llm,
        on_engineering_task=_handle_engineering_task,
        on_status_query=_handle_status_query,
    )
    if config.telegram_bot_token:
        await telegram_bot.setup()
        await telegram_bot.start_polling()
        log.info("Telegram bot initialized (polling mode)")

    # Schema migrations (idempotent)
    await db.kb.ensure_tasks_table()

    from coordinator.tool_schemas import ensure_schema_table, seed_default_schemas
    await ensure_schema_table(db.kb.pool)
    await seed_default_schemas(db.kb.pool)

    from coordinator.reports import ensure_reports_table
    await ensure_reports_table(db.kb.pool)

    from coordinator.editor_store import ensure_editor_tables, seed_from_filesystem, list_agents as _list_agents
    await ensure_editor_tables(db.kb.pool)
    await seed_from_filesystem(db.kb.pool, _AGENTS_DIR, _WORKFLOWS_DIR)

    # Register all DB-stored agents into trigger_router so UI-created agents work
    for row in await _list_agents(db.kb.pool):
        trigger_router.register(row["name"], row.get("manifest", ""), row.get("prompts", ""))

    # LISTEN/NOTIFY for agent completion events
    await db.start_listener(_on_agent_event)

    # Periodic heartbeat to llm-manager (keeps app "online")
    _heartbeat_task = None
    if llm_api_key:
        _heartbeat_task = asyncio.create_task(_llm_heartbeat_loop(config.llm_manager_url, llm_api_key))

    log.info("Coordinator started")
    yield

    # Shutdown
    if _heartbeat_task:
        _heartbeat_task.cancel()
    if config.telegram_bot_token:
        await telegram_bot.stop_polling()
    await db.close()
    await llm.close()
    log.info("Coordinator stopped")


app = FastAPI(title="Mycroft Coordinator", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Research pipeline (background)
# ---------------------------------------------------------------------------

async def _start_research_pipeline(
    instruction: str,
    workflow: str,
    gather_model: str | None = None,
    write_model: str | None = None,
    gather_tools: list[str] | None = None,
) -> str:
    """Start the two-phase gather→write pipeline. Returns the gather task ID."""
    from coordinator.research_pipeline import WORKFLOW_CONFIG, GATHERER_PROMPT

    wf_config = WORKFLOW_CONFIG.get(workflow, WORKFLOW_CONFIG["research-regular"])
    gather_cfg = wf_config["gather"]

    resolved_gather_model = gather_model or gather_cfg["model"]
    resolved_gather_tools = gather_tools or gather_cfg["tools"]

    gather_config = {
        "instruction": instruction,
        "model_override": resolved_gather_model,
        "max_iterations_override": gather_cfg["max_iterations"],
        "tools_override": resolved_gather_tools,
        "system_prompt_override": GATHERER_PROMPT,
        "phase": "gather",
        "workflow": workflow,
    }

    gather_task_id = await task_manager.create_task(
        agent_type="researcher",
        instruction=instruction,
        trigger="pipeline",
        repo="",
        config=gather_config,
    )
    tasks_created_total.labels(agent_type="researcher", trigger="pipeline").inc()
    tasks_active.labels(agent_type="researcher").inc()

    gather_wf_name = await argo.submit(
        agent_type="researcher",
        task_id=gather_task_id,
        params={"instruction": instruction, "model_override": resolved_gather_model},
        manifest=trigger_router.get_manifest("researcher"),
        on_update=_on_workflow_update,
    )
    await db.kb.update_task(gather_task_id, argo_workflow_name=gather_wf_name)

    log.info("Research pipeline started: gather=%s model=%s workflow=%s",
             gather_task_id[:8], resolved_gather_model, workflow)

    asyncio.create_task(
        _pipeline_writer_phase(gather_task_id, instruction, workflow, write_model=write_model)
    )

    return gather_task_id


async def _pipeline_writer_phase(
    gather_task_id: str,
    instruction: str,
    workflow: str,
    write_model: str | None = None,
):
    """Background: wait for gatherer to finish, then launch the writer."""
    from coordinator.research_pipeline import WORKFLOW_CONFIG, WRITER_PROMPT, _wait_for_task

    try:
        status_hint = await _wait_for_task(gather_task_id, db, timeout=600)

        # Abort writer if gather was cancelled
        if not status_hint or status_hint.startswith("("):
            gather_task = await task_manager.get_task(gather_task_id)
            was_cancelled = (
                gather_task
                and gather_task.result
                and "Cancelled" in gather_task.result.get("error", "")
            )
            if was_cancelled:
                log.info("Pipeline writer phase aborted — gather task %s was cancelled", gather_task_id[:8])
                return

        # Read full findings from KB — task.result["summary"] is truncated to 1000 chars
        gather_record = await db.kb.get(f"/agents/researcher/results/{gather_task_id}")
        findings = (gather_record.content if gather_record else None) or status_hint
        if not findings:
            log.warning("Pipeline gatherer %s produced no usable findings, proceeding anyway", gather_task_id[:8])
            findings = f"(Limited findings for: {instruction})"

        log.info("Pipeline gatherer %s done (%d chars). Launching writer.", gather_task_id[:8], len(findings))

        wf_config = WORKFLOW_CONFIG.get(workflow, WORKFLOW_CONFIG["research-regular"])
        write_cfg = wf_config["write"]

        resolved_write_model = write_model or write_cfg["model"]

        writer_instruction = (
            f"Write a research report based on these findings.\n\n"
            f"Original question: {instruction}\n\n"
            f"Research findings:\n{findings[:15000]}"
        )

        write_config = {
            "instruction": writer_instruction,
            "model_override": resolved_write_model,
            "max_iterations_override": write_cfg["max_iterations"],
            "tools_override": write_cfg["tools"],
            "system_prompt_override": WRITER_PROMPT,
            "phase": "write",
            "workflow": workflow,
            "parent_task_id": gather_task_id,
        }

        write_task_id = await task_manager.create_task(
            agent_type="researcher",
            instruction=writer_instruction,
            trigger="pipeline",
            repo="",
            config=write_config,
        )

        write_wf_name = await argo.submit(
            agent_type="researcher",
            task_id=write_task_id,
            params={"instruction": writer_instruction, "model_override": resolved_write_model},
            manifest=trigger_router.get_manifest("researcher"),
            on_update=_on_workflow_update,
        )
        await db.kb.update_task(write_task_id, argo_workflow_name=write_wf_name)

        log.info("Pipeline writer launched: %s model=%s (parent=%s)",
                 write_task_id[:8], resolved_write_model, gather_task_id[:8])

    except Exception as e:
        log.exception("Pipeline writer phase failed for gather=%s", gather_task_id[:8])


async def _start_dynamic_pipeline(
    instruction: str,
    workflow_name: str,
    steps: list[dict],
) -> str:
    """Start an N-step dynamic pipeline from a DB-stored workflow definition. Returns first task ID."""
    if not steps:
        raise ValueError(f"Workflow '{workflow_name}' has no pipeline steps")

    step = steps[0]
    is_last = len(steps) == 1
    agent_type = step.get("agent", "researcher")

    step_prompt = step.get("prompt_override") or trigger_router.get_prompts(agent_type) or None
    task_config = {
        "instruction": instruction,
        "model_override": step.get("model") or None,
        "max_iterations_override": step.get("max_iterations") or None,
        "tools_override": step.get("tools") or None,
        "system_prompt_override": step_prompt,
        "phase": "pipeline-step-0",
        "is_last_step": is_last,
        "workflow": workflow_name,
        "pipeline_original_instruction": instruction,
    }

    task_id = await task_manager.create_task(
        agent_type=agent_type,
        instruction=instruction,
        trigger="pipeline",
        repo="",
        config=task_config,
    )
    tasks_created_total.labels(agent_type=agent_type, trigger="pipeline").inc()
    tasks_active.labels(agent_type=agent_type).inc()

    model = step.get("model") or None
    params: dict = {"instruction": instruction}
    if model:
        params["model_override"] = model
    wf_name = await argo.submit(
        agent_type=agent_type, task_id=task_id, params=params,
        manifest=trigger_router.get_manifest(agent_type),
        on_update=_on_workflow_update,
    )
    await db.kb.update_task(task_id, argo_workflow_name=wf_name)

    log.info("Dynamic pipeline started: step=1/%d agent=%s workflow=%s task=%s",
             len(steps), agent_type, workflow_name, task_id[:8])

    if not is_last:
        asyncio.create_task(
            _run_dynamic_pipeline_steps(task_id, agent_type, instruction, workflow_name, steps, 1)
        )

    return task_id


async def _run_dynamic_pipeline_steps(
    prev_task_id: str,
    prev_agent_type: str,
    original_instruction: str,
    workflow_name: str,
    steps: list[dict],
    step_index: int,
) -> None:
    """Background: wait for previous step to finish, then launch the next step."""
    from coordinator.research_pipeline import _wait_for_task

    try:
        await _wait_for_task(prev_task_id, db, timeout=600)

        # Read full output from KB — task.result["summary"] is truncated to 1000 chars
        record = await db.kb.get(f"/agents/{prev_agent_type}/results/{prev_task_id}")
        prev_output = (record.content if record else None) or f"(No output from step {step_index - 1})"

        step = steps[step_index]
        is_last = step_index == len(steps) - 1
        agent_type = step.get("agent", "researcher")

        step_instruction = (
            f"Original question: {original_instruction}\n\n"
            f"Previous step output:\n{prev_output[:15000]}"
        )

        step_prompt = step.get("prompt_override") or trigger_router.get_prompts(agent_type) or None
        task_config = {
            "instruction": step_instruction,
            "model_override": step.get("model") or None,
            "max_iterations_override": step.get("max_iterations") or None,
            "tools_override": step.get("tools") or None,
            "system_prompt_override": step_prompt,
            "phase": f"pipeline-step-{step_index}",
            "is_last_step": is_last,
            "workflow": workflow_name,
            "pipeline_original_instruction": original_instruction,
            "parent_task_id": prev_task_id,
        }

        task_id = await task_manager.create_task(
            agent_type=agent_type,
            instruction=step_instruction,
            trigger="pipeline",
            repo="",
            config=task_config,
        )
        tasks_created_total.labels(agent_type=agent_type, trigger="pipeline").inc()
        tasks_active.labels(agent_type=agent_type).inc()

        model = step.get("model") or None
        params: dict = {"instruction": step_instruction}
        if model:
            params["model_override"] = model
        wf_name = await argo.submit(
            agent_type=agent_type, task_id=task_id, params=params,
            manifest=trigger_router.get_manifest(agent_type),
            on_update=_on_workflow_update,
        )
        await db.kb.update_task(task_id, argo_workflow_name=wf_name)

        log.info("Dynamic pipeline step %d/%d: agent=%s workflow=%s task=%s",
                 step_index + 1, len(steps), agent_type, workflow_name, task_id[:8])

        if not is_last:
            asyncio.create_task(
                _run_dynamic_pipeline_steps(task_id, agent_type, original_instruction, workflow_name, steps, step_index + 1)
            )

    except Exception:
        log.exception("Dynamic pipeline step %d failed (prev_task=%s)", step_index, prev_task_id[:8])


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_engineering_task(
    instruction: str, agent_type: str, repo: str,
    model_override: str | None = None, system_prompt_override: str | None = None,
    max_tokens: int | None = None, temperature: float | None = None,
    max_iterations: int | None = None, effort: str | None = None,
    tools_override: list[str] | None = None,
    workflow: str | None = None,
) -> str:
    """Handle an engineering task from Telegram or API."""
    manifest = trigger_router.get_manifest(agent_type)
    if not manifest:
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Inject DB-stored prompts as system_prompt_override if none explicitly set
    if not system_prompt_override:
        system_prompt_override = trigger_router.get_prompts(agent_type) or None

    # Check concurrency
    if not await task_manager.can_launch(agent_type, manifest.max_concurrent):
        raise ValueError(f"Max concurrent tasks reached for {agent_type}")

    # Build task config
    task_config: dict[str, Any] = {}
    if model_override:
        task_config["model_override"] = model_override
    if system_prompt_override:
        task_config["system_prompt_override"] = system_prompt_override
    if max_tokens is not None:
        task_config["max_tokens"] = max_tokens
    if temperature is not None:
        task_config["temperature"] = temperature
    if max_iterations is not None:
        task_config["max_iterations_override"] = max_iterations
    if effort:
        task_config["effort"] = effort
    if workflow:
        task_config["workflow"] = workflow
    if tools_override:
        task_config["tools_override"] = tools_override

    # Create task
    task_id = await task_manager.create_task(
        agent_type=agent_type,
        instruction=instruction,
        trigger="manual",
        repo=repo,
        config=task_config,
    )
    tasks_created_total.labels(agent_type=agent_type, trigger="manual").inc()
    tasks_active.labels(agent_type=agent_type).inc()

    # Submit Argo Workflow with retries
    params: dict[str, Any] = {"instruction": instruction, "repo": repo}
    if model_override:
        params["model_override"] = model_override

    max_retries = 3
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            wf_name = await argo.submit(
                agent_type=agent_type,
                task_id=task_id,
                params=params,
                manifest=manifest,
                on_update=_on_workflow_update,
            )
            await db.kb.update_task(task_id, argo_workflow_name=wf_name)
            log.info("Workflow %s submitted for task %s (attempt %d)", wf_name, task_id[:8], attempt)
            argo_submissions_total.labels(agent_type=agent_type, result="success").inc()
            return task_id
        except Exception as e:
            last_error = e
            log.warning("Argo submission attempt %d/%d failed for task %s: %s", attempt, max_retries, task_id[:8], e)
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)  # 2s, 4s backoff

    # All retries exhausted
    log.error("Failed to submit Argo Workflow after %d attempts: %s", max_retries, last_error)
    argo_submissions_total.labels(agent_type=agent_type, result="failure").inc()
    tasks_active.labels(agent_type=agent_type).dec()
    tasks_completed_total.labels(agent_type=agent_type, status="failed").inc()
    await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": f"Workflow submission failed after {max_retries} attempts: {last_error}"})
    raise last_error


async def _on_workflow_update(task_id: str, status: str, message: str):
    """Called by Argo watcher on workflow state changes."""

    # Don't overwrite a task the user already cancelled
    current = await task_manager.get_task(task_id)
    if current and current.status in (TaskStatus.failed, TaskStatus.completed):
        log.debug("Ignoring workflow update for already-terminal task %s (%s)", task_id[:8], current.status)
        return

    if status in ("failed", "error"):
        await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": message})
        task = await task_manager.get_task(task_id)
        if task:
            tasks_active.labels(agent_type=task.agent_type).dec()
            tasks_completed_total.labels(agent_type=task.agent_type, status="failed").inc()
        log.warning("Task %s failed: %s", task_id[:8], message)

    elif status == "succeeded":
        await db.kb.update_task(task_id, status=TaskStatus.completed)
        task = await task_manager.get_task(task_id)
        if task:
            tasks_active.labels(agent_type=task.agent_type).dec()
            tasks_completed_total.labels(agent_type=task.agent_type, status="completed").inc()
        try:
            await telegram_bot.send(f"Task {task_id[:8]} completed: {message}")
        except Exception:
            log.warning("Failed to send Telegram update for task %s", task_id[:8])


async def _handle_status_query(text: str) -> str:
    """Handle a status query — look up recent tasks."""
    tasks = await task_manager.list_tasks(limit=5)
    if not tasks:
        return "No tasks found."

    lines = []
    for t in tasks:
        status_icon = {"pending": "...", "running": ">>", "completed": "OK", "failed": "XX"}
        icon = status_icon.get(t.status.value, "??")
        summary = ""
        if t.result:
            summary = t.result.get("summary", t.result.get("error", ""))[:100]
        lines.append(f"[{icon}] {t.id[:8]} {t.agent_type} — {summary or t.trigger}")

    return "\n".join(lines)


async def _on_agent_event(event: dict[str, Any]) -> None:
    """Handle PG NOTIFY from agent completion.

    Only sends Telegram messages for successful completions with results.
    Failures, iteration limits, and other noise go to logs + metrics only.
    """
    scope = event.get("scope", "")
    source = event.get("source", "")

    # Agent result — route based on agent type
    if "/results/" in scope:
        record = await db.kb.get(scope)
        if not record:
            return

        is_researcher = "researcher" in source

        # Dynamic pipeline: route final step to report handler regardless of agent type
        task_id_hint = source.split("/")[-1] if "/" in source else ""
        task_hint = await task_manager.get_task(task_id_hint) if task_id_hint else None
        if task_hint and task_hint.config.get("is_last_step"):
            await _handle_researcher_result(record, source)
            return

        # Researcher results → save locally, optionally post to Sazed, notify Telegram
        if is_researcher:
            await _handle_researcher_result(record, source)
        elif telegram_bot:
            # Other agents (coder, etc.) → Telegram directly
            pr_url = ""
            if record.metadata:
                pr_url = record.metadata.get("pr_url", "")

            msg = f"Agent finished: {source}\n{record.content[:500]}"
            if pr_url:
                msg += f"\nPR: {pr_url}"

            try:
                await telegram_bot.send(msg)
            except Exception:
                log.warning("Failed to send Telegram notification", exc_info=True)

    # Notifications — log only, don't send to Telegram
    elif scope.startswith("/notifications/alex/"):
        record = await db.kb.get(scope)
        if record:
            log.info("Agent notification: %s", record.content[:200])


def _extract_title_summary(content: str) -> tuple[str, str]:
    """Extract title and summary from markdown report content."""
    title = "Research Report"
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            title = line.removeprefix("# ").strip().strip("*").strip()
            break
        elif line and not line.startswith("#"):
            title = line.strip("*").strip()[:80]
            break

    summary = ""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## summary"):
            summary_lines = []
            for sl in lines[i + 1:]:
                if sl.startswith("## "):
                    break
                if sl.strip():
                    summary_lines.append(sl.strip())
            summary = " ".join(summary_lines)[:500]
            break
    if not summary:
        summary = content[:300]

    return title, summary


async def _handle_researcher_result(record, source: str) -> None:
    """Save researcher result to local DB and optionally post to Sazed + Telegram."""
    from coordinator.reports import create_report

    content = record.content or ""
    task_id = source.split("/")[-1] if "/" in source else ""

    # Skip non-final pipeline steps — only final step (or standalone) creates a report
    task = await task_manager.get_task(task_id) if task_id else None
    if task:
        phase = task.config.get("phase", "")
        is_last = task.config.get("is_last_step", False)
        if phase == "gather":
            log.debug("Skipping report for gather phase task %s", task_id[:8])
            return
        if phase.startswith("pipeline-step-") and not is_last:
            log.debug("Skipping report for intermediate pipeline step task %s (phase=%s)", task_id[:8], phase)
            return

    title, summary = _extract_title_summary(content)

    # Build report metadata
    workflow = (task.config.get("workflow", "") if task else "") or "research-regular"
    models_used: dict[str, str] = {}
    commit_sha = getattr(config, "agent_image_tag", "") or ""

    if task:
        writer_model = task.config.get("model_override", "")
        if writer_model:
            models_used["write"] = writer_model
        parent_id = task.config.get("parent_task_id", "")
        if parent_id:
            parent = await task_manager.get_task(parent_id)
            if parent:
                gather_model = parent.config.get("model_override", "")
                if gather_model:
                    models_used["gather"] = gather_model

    # Append metadata footer to content
    meta_parts = [f"Workflow: {workflow}"]
    if models_used:
        meta_parts.append("Models: " + ", ".join(f"{k}={v}" for k, v in models_used.items()))
    if commit_sha:
        meta_parts.append(f"Build: {commit_sha}")
    content_with_meta = content + "\n\n---\n\n*" + " · ".join(meta_parts) + "*"

    # Always save to local reports DB
    try:
        await create_report(
            db.kb.pool,
            title=title,
            content=content_with_meta,
            summary=summary,
            tags=[],
            source_task_id=task_id,
            effort=workflow,
            workflow=workflow,
            models_used=models_used,
            commit_sha=commit_sha,
        )
        log.info("Report saved to local DB: %s (task=%s)", title[:50], task_id[:8])
    except Exception as e:
        log.warning("Failed to save report to local DB: %s", e)

    # Optionally post to Sazed
    report_url = None
    if config.sazed_url:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{config.sazed_url}/api/reports",
                    json={"title": title, "content": content_with_meta, "summary": summary,
                          "tags": [], "source_task_id": task_id},
                )
                resp.raise_for_status()
                data = resp.json()
                slug = data.get("slug", data.get("id", ""))
                report_url = f"{config.sazed_url}/r/{slug}"
                log.info("Report posted to Sazed: %s", report_url)
        except Exception as e:
            log.error("Failed to post report to Sazed: %s", e)

    # Notify Telegram
    if telegram_bot:
        try:
            msg = f"{summary}"
            if report_url:
                msg += f"\n\nFull report: {report_url}"
            await telegram_bot.send(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Forge Runner API
# ---------------------------------------------------------------------------

from coordinator.forge_runner import run_forge, get_run, ForgeResult


class ForgeRunRequest(BaseModel):
    instruction: str
    repo: str = ""
    model: str = "qwen3:14b"
    system_prompt: str | None = None


@app.post("/api/forge/run")
async def forge_run(req: ForgeRunRequest):
    if not req.repo:
        raise HTTPException(400, "repo is required (e.g. 'amerenda/mycroft')")

    llm_api_key = config.llm_manager_api_key
    run_id = await run_forge(
        instruction=req.instruction,
        repo=req.repo,
        model=req.model,
        system_prompt=req.system_prompt,
        llm_url=config.llm_manager_url,
        llm_api_key=llm_api_key,
    )
    return {"run_id": run_id}


@app.get("/api/forge/runs/{run_id}")
async def forge_run_status(run_id: str):
    result = get_run(run_id)
    if not result:
        raise HTTPException(404, "Run not found")
    return {
        "run_id": result.run_id,
        "status": result.status,
        "exit_code": result.exit_code,
        "git_diff": result.git_diff,
        "files_changed": result.files_changed,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    agent_type: str = ""
    instruction: str
    repo: str = ""
    trigger: str = "manual"
    model: str | None = None
    system_prompt: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    max_iterations: int | None = None
    workflow: str | None = None              # research-quick, research-regular, research-deep, coder
    effort: str | None = None               # deprecated alias for workflow
    tools_override: list[str] | None = None
    gather_model: str | None = None
    write_model: str | None = None


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    from coordinator.research_pipeline import resolve_workflow, WORKFLOW_CONFIG

    try:
        # Resolve workflow name (accepts either workflow= or deprecated effort=)
        workflow = resolve_workflow(req.workflow, req.effort)

        # Research pipelines
        if workflow in ("research-regular", "research-deep"):
            gather_task_id = await _start_research_pipeline(
                req.instruction,
                workflow,
                gather_model=req.gather_model or req.model,
                write_model=req.write_model,
                gather_tools=req.tools_override or None,
            )
            return {"task_id": gather_task_id}

        if workflow == "research-quick":
            task_id = await _handle_engineering_task(
                instruction=req.instruction,
                agent_type="researcher",
                repo=req.repo,
                model_override=req.model or "qwen3.5:9b",
                system_prompt_override=req.system_prompt,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                max_iterations=req.max_iterations or 6,
                effort=None,
                tools_override=req.tools_override or ["web_search", "wiki_read", "web_read"],
                workflow="research-quick",
            )
            return {"task_id": task_id}

        # Unknown workflow name — look up in DB and run as dynamic pipeline
        if workflow and workflow not in ("coder",):
            from coordinator.editor_store import get_workflow as _get_wf
            wf_def = await _get_wf(db.kb.pool, workflow)
            if wf_def:
                pipeline_json = wf_def.get("pipeline_json") or {}
                steps = pipeline_json.get("steps", [])
                if not steps:
                    raise ValueError(f"Workflow '{workflow}' has no pipeline steps defined")
                first_task_id = await _start_dynamic_pipeline(req.instruction, workflow, steps)
                return {"task_id": first_task_id}
            raise ValueError(f"Unknown workflow: '{workflow}'")

        # coder, direct agent_type, or no workflow specified (test button path)
        agent_type = req.agent_type or workflow
        if not agent_type:
            raise ValueError("workflow or agent_type is required")
        task_id = await _handle_engineering_task(
            instruction=req.instruction,
            agent_type=agent_type,
            repo=req.repo,
            model_override=req.model,
            system_prompt_override=req.system_prompt,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            max_iterations=req.max_iterations,
            effort=None,
            tools_override=req.tools_override,
            workflow=workflow,
        )
        return {"task_id": task_id}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to create task: {e}")


@app.get("/api/tasks")
async def list_tasks(agent_type: str | None = None, status: str | None = None, limit: int = 20):
    task_status = TaskStatus(status) if status else None
    tasks = await task_manager.list_tasks(agent_type=agent_type, status=task_status, limit=limit)
    return [t.model_dump() for t in tasks]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.model_dump()


async def _stop_task_workflow(task: Any) -> None:
    """Terminate the Argo Workflow for a task, using in-memory map or DB-stored name."""
    # Try in-memory map first (fast path, works while coordinator is up)
    if await argo.terminate_task(task.id):
        return
    # Fallback: use the wf_name persisted to DB (survives restarts)
    if task.argo_workflow_name:
        await argo._terminate_workflow(task.argo_workflow_name)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await _stop_task_workflow(task)
    await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": "Cancelled by user"})
    return {"status": "cancelled"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await _stop_task_workflow(task)
    await db.kb.delete_task(task_id)
    return {"status": "deleted"}


@app.delete("/api/tasks")
async def delete_all_tasks():
    count = await db.kb.delete_all_tasks()
    return {"status": "deleted", "count": count}


class DispatchRequest(BaseModel):
    event_type: str
    payload: dict[str, Any] = {}


@app.post("/api/dispatch")
async def dispatch_event(req: DispatchRequest):
    """Event dispatch endpoint for ARC GitHub Actions."""
    agent_types = trigger_router.route(req.event_type, req.payload)
    launched = []
    for agent_type in agent_types:
        instruction = req.payload.get("instruction", json.dumps(req.payload))
        task_id = await _handle_engineering_task(
            instruction=instruction,
            agent_type=agent_type,
            repo=req.payload.get("repo", ""),
        )
        launched.append({"agent_type": agent_type, "task_id": task_id})
    return {"launched": launched}


@app.get("/api/tasks/{task_id}/conversation")
async def get_task_conversation(task_id: str):
    """View the full conversation log (prompt, LLM responses, tool calls) for a task."""
    record = await db.kb.get(f"/tasks/{task_id}/conversation")
    if not record:
        raise HTTPException(404, "No conversation found for this task")
    try:
        messages = json.loads(record.content)
    except (json.JSONDecodeError, TypeError):
        messages = []
    return {"task_id": task_id, "messages": messages}


@app.get("/api/tasks/{task_id}/prompt")
async def get_task_prompt(task_id: str):
    """View the system prompt and user message for a task."""
    record = await db.kb.get(f"/tasks/{task_id}/conversation")
    if not record:
        # Try inbox
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        inbox = await db.kb.get(f"/agents/{task.agent_type}/inbox/{task_id}")
        return {
            "task_id": task_id,
            "instruction": inbox.content if inbox else task.config.get("instruction", ""),
            "system_prompt": None,
            "note": "Agent hasn't started yet — no conversation persisted",
        }

    messages = json.loads(record.content)
    system_prompt = next((m["content"] for m in messages if m.get("role") == "system"), None)
    user_message = next((m["content"] for m in messages if m.get("role") == "user"), None)
    return {
        "task_id": task_id,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "total_messages": len(messages),
    }


class TestTaskRequest(BaseModel):
    agent_type: str = "coder"
    instruction: str
    model: str | None = None


@app.post("/api/tasks/test")
async def test_task(req: TestTaskRequest):
    """Preview the prompt that would be sent to an agent. Does not create a task."""
    from runtime.context import build_system_prompt, build_user_message
    from runtime.tools.base import load_tools

    manifest = trigger_router.get_manifest(req.agent_type)
    if not manifest:
        raise HTTPException(400, f"Unknown agent type: {req.agent_type}")

    if req.model:
        manifest = manifest.model_copy()
        manifest.model = req.model

    # Build prompt preview — use DB prompts as system override if available
    db_prompts = trigger_router.get_prompts(req.agent_type)
    tools = load_tools(manifest.tools)
    system_prompt = db_prompts or build_system_prompt(manifest, tools.schemas())
    user_message = build_user_message(req.instruction, [])

    return {
        "agent_type": req.agent_type,
        "model": manifest.model,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "tools": [t["function"]["name"] for t in tools.schemas()],
    }


@app.post("/webhooks/telegram")
async def telegram_webhook(request: Request):
    """Telegram webhook endpoint (for future use with Cloudflare Tunnel)."""
    if not telegram_bot.app:
        raise HTTPException(503, "Telegram bot not configured")

    from telegram import Update
    data = await request.json()
    update = Update.de_json(data, telegram_bot.app.bot)
    await telegram_bot.app.process_update(update)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reports API
# ---------------------------------------------------------------------------

from coordinator.reports import (
    create_report, get_report, list_reports, update_report,
    delete_report, delete_all_reports,
)


class CreateReportRequest(BaseModel):
    title: str
    content: str
    summary: str = ""
    tags: list[str] = []
    source_task_id: str = ""
    effort: str = "regular"


@app.get("/api/reports")
async def api_list_reports(limit: int = 50):
    return await list_reports(db.kb.pool, limit)


@app.get("/api/reports/{report_id}")
async def api_get_report(report_id: str):
    report = await get_report(db.kb.pool, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@app.post("/api/reports")
async def api_create_report(req: CreateReportRequest):
    report_id = await create_report(
        db.kb.pool, req.title, req.content, req.summary,
        req.tags, req.source_task_id, req.effort,
    )
    return {"id": report_id}


@app.delete("/api/reports/{report_id}")
async def api_delete_report(report_id: str):
    deleted = await delete_report(db.kb.pool, report_id)
    if not deleted:
        raise HTTPException(404, "Report not found")
    return {"status": "deleted"}


@app.delete("/api/reports")
async def api_delete_all_reports():
    count = await delete_all_reports(db.kb.pool)
    return {"status": "deleted", "count": count}


# ---------------------------------------------------------------------------
# Tool Schema API
# ---------------------------------------------------------------------------

from coordinator.tool_schemas import (
    get_schema, list_schemas, get_schema_history, upsert_schema, delete_schema,
)


@app.get("/api/tools/schemas")
async def api_list_schemas():
    return await list_schemas(db.kb.pool)


@app.get("/api/tools/schemas/{name}")
async def api_get_schema(name: str, version: int | None = None):
    result = await get_schema(db.kb.pool, name, version)
    if not result:
        raise HTTPException(404, f"Tool schema '{name}' not found")
    return result


@app.get("/api/tools/schemas/{name}/history")
async def api_schema_history(name: str):
    return await get_schema_history(db.kb.pool, name)


class UpsertSchemaRequest(BaseModel):
    schema: dict[str, Any]
    schema_version: str = "1.0.0"
    changelog: str = ""
    updated_by: str = "ui"


@app.put("/api/tools/schemas/{name}")
async def api_upsert_schema(name: str, req: UpsertSchemaRequest):
    new_version = await upsert_schema(
        db.kb.pool, name, req.schema, req.schema_version, req.changelog, req.updated_by,
    )
    return {"name": name, "version": new_version}


@app.delete("/api/tools/schemas/{name}")
async def api_delete_schema(name: str):
    deleted = await delete_schema(db.kb.pool, name)
    if not deleted:
        raise HTTPException(404, f"Tool schema '{name}' not found")
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# LLM Manager proxy
# ---------------------------------------------------------------------------


@app.get("/api/models")
async def list_models():
    """Proxy available models from llm-manager."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{config.llm_manager_url}/api/models")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning("Failed to fetch models from llm-manager: %s", e)
        return []


# ---------------------------------------------------------------------------
# Agent + Workflow file editors
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"
_WORKFLOWS_DIR = _REPO_ROOT / "workflows"

_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _safe_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(400, "Invalid name — use lowercase letters, digits, hyphens, underscores")
    return name


class AgentPayload(BaseModel):
    manifest: str
    prompts: str = ""


class WorkflowPayload(BaseModel):
    content: str = ""
    pipeline_json: dict | None = None


@app.get("/api/agents")
async def list_agents():
    from coordinator.editor_store import list_agents as _list_agents
    return await _list_agents(db.kb.pool)


@app.get("/api/agents/{name}")
async def get_agent(name: str):
    _safe_name(name)
    from coordinator.editor_store import get_agent as _get_agent
    agent = await _get_agent(db.kb.pool, name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    return agent


@app.put("/api/agents/{name}")
async def save_agent(name: str, payload: AgentPayload):
    _safe_name(name)
    from coordinator.editor_store import save_agent as _save_agent, slugify
    canonical = await _save_agent(db.kb.pool, name, payload.manifest, payload.prompts)
    trigger_router.register(canonical, payload.manifest, payload.prompts or "")
    return {"status": "saved", "name": canonical}


@app.delete("/api/agents/{name}")
async def delete_agent(name: str):
    _safe_name(name)
    from coordinator.editor_store import delete_agent as _delete_agent
    deleted = await _delete_agent(db.kb.pool, name)
    if not deleted:
        raise HTTPException(404, f"Agent '{name}' not found")
    trigger_router.unregister(name)
    return {"status": "deleted", "name": name}


@app.get("/api/workflows")
async def list_workflows():
    from coordinator.editor_store import list_workflows as _list_workflows
    return await _list_workflows(db.kb.pool)


@app.get("/api/workflows/{name}")
async def get_workflow(name: str):
    _safe_name(name)
    from coordinator.editor_store import get_workflow as _get_workflow
    wf = await _get_workflow(db.kb.pool, name)
    if not wf:
        raise HTTPException(404, f"Workflow '{name}' not found")
    return wf


@app.put("/api/workflows/{name}")
async def save_workflow(name: str, payload: WorkflowPayload):
    _safe_name(name)
    from coordinator.editor_store import save_workflow as _save_workflow
    await _save_workflow(db.kb.pool, name, payload.content, payload.pipeline_json)
    return {"status": "saved", "name": name}


@app.delete("/api/workflows/{name}")
async def delete_workflow(name: str):
    _safe_name(name)
    from coordinator.editor_store import delete_workflow as _delete_workflow
    deleted = await _delete_workflow(db.kb.pool, name)
    if not deleted:
        raise HTTPException(404, f"Workflow '{name}' not found")
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Debug UI
# ---------------------------------------------------------------------------

from fastapi.responses import HTMLResponse

@app.get("/debug", response_class=HTMLResponse)
async def debug_page():
    """Simple debug UI for testing agents."""
    return DEBUG_HTML


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    from fastapi.responses import Response
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


DEBUG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mycroft Debug</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1000px; margin: 0 auto; }
  h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }
  h2 { color: #8b949e; margin: 20px 0 10px; font-size: 1.1em; }
  .panel { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin-bottom: 16px; }
  label { display: block; color: #8b949e; font-size: 0.85em; margin-bottom: 4px; }
  input, select, textarea { width: 100%; padding: 8px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 4px; color: #c9d1d9; font-family: inherit; font-size: 0.9em; }
  textarea { min-height: 80px; resize: vertical; }
  .row { display: flex; gap: 12px; margin-bottom: 12px; }
  .row > * { flex: 1; }
  button { padding: 8px 20px; border-radius: 4px; border: none; cursor: pointer; font-size: 0.9em; font-weight: 600; }
  .btn-primary { background: #238636; color: #fff; }
  .btn-primary:hover { background: #2ea043; }
  .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-secondary:hover { background: #30363d; }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  pre { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 12px; overflow-x: auto; font-size: 0.82em; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
  .msg { padding: 8px 12px; margin: 4px 0; border-radius: 4px; font-size: 0.85em; }
  .msg-system { background: #1c2128; border-left: 3px solid #8b949e; }
  .msg-user { background: #0c2d6b; border-left: 3px solid #58a6ff; }
  .msg-assistant { background: #1c2d1c; border-left: 3px solid #3fb950; }
  .msg-tool { background: #2d1c1c; border-left: 3px solid #f0883e; }
  .role { font-weight: 600; font-size: 0.75em; text-transform: uppercase; margin-bottom: 4px; }
  .task-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #21262d; font-size: 0.85em; }
  .task-row:hover { background: #161b22; }
  .task-info { cursor: pointer; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  .btn-delete { background: none; border: none; color: #6e7681; cursor: pointer; padding: 2px 6px; font-size: 0.85em; border-radius: 3px; }
  .btn-delete:hover { color: #da3633; background: #da363322; }
  .btn-danger { background: #da3633; color: #fff; font-size: 0.8em; padding: 4px 12px; }
  .btn-danger:hover { background: #f85149; }
  .tasks-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .status { padding: 2px 8px; border-radius: 3px; font-size: 0.75em; font-weight: 600; }
  .status-completed { background: #238636; }
  .status-running { background: #1f6feb; }
  .status-failed { background: #da3633; }
  .status-pending { background: #6e7681; }
  #spinner { display: none; margin-left: 8px; }
  .actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
</head>
<body>
<h1>Mycroft Debug Console</h1>

<div class="panel">
  <h2>Run Agent</h2>
  <div class="row">
    <div>
      <label>Agent Type</label>
      <select id="agentType"><option value="coder">coder</option></select>
    </div>
    <div>
      <label>Model (optional override)</label>
      <select id="model"><option value="">Default (from manifest)</option></select>
    </div>
  </div>
  <label>Instruction</label>
  <textarea id="instruction" placeholder="e.g. In the mycroft repo, add a README.md with a brief project description"></textarea>
  <details style="margin-top:12px">
    <summary style="cursor:pointer;color:#58a6ff;font-size:0.85em">System Prompt Override (optional)</summary>
    <textarea id="systemPrompt" style="margin-top:8px;min-height:200px;font-size:0.8em" placeholder="Leave empty to use default. The default prompt will be shown when you click Preview Prompt."></textarea>
  </details>
  <div class="actions">
    <button class="btn-primary" onclick="runTask()" id="runBtn">Run Task (via Argo)</button>
    <button class="btn-secondary" onclick="previewPrompt()">Preview Prompt</button>
    <span id="spinner">Running...</span>
  </div>
</div>

<div class="panel" id="promptPanel" style="display:none">
  <h2>Prompt Preview</h2>
  <div id="promptContent"></div>
</div>

<div class="panel" id="conversationPanel" style="display:none">
  <h2>Conversation <span id="convTaskId" style="color:#8b949e; font-weight:normal"></span></h2>
  <div id="conversationContent"></div>
</div>

<div class="panel">
  <div class="tasks-header">
    <h2>Recent Tasks</h2>
    <button class="btn-danger" onclick="clearAllTasks()">Clear All</button>
  </div>
  <div id="taskList">Loading...</div>
</div>

<script>
const API = '';

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  return r.json();
}

async function loadTasks() {
  try {
    const tasks = await api('/api/tasks?limit=10');
    const el = document.getElementById('taskList');
    if (!tasks.length) { el.innerHTML = '<em>No tasks yet</em>'; return; }
    el.innerHTML = tasks.map(t => `
      <div class="task-row">
        <span class="task-info" onclick="viewConversation('${t.id}')">${t.id.slice(0,8)} — ${t.agent_type} — ${esc((t.config?.instruction || '').slice(0,60))}</span>
        <div class="task-actions">
          <span class="status status-${t.status}">${t.status}</span>
          <button class="btn-delete" onclick="deleteTask('${t.id}')" title="Delete task">✕</button>
        </div>
      </div>
    `).join('');
  } catch(e) { document.getElementById('taskList').innerHTML = '<em>Error loading tasks</em>'; }
}

async function runTask() {
  const instruction = document.getElementById('instruction').value.trim();
  if (!instruction) return;
  const btn = document.getElementById('runBtn');
  const spinner = document.getElementById('spinner');
  btn.disabled = true; spinner.style.display = 'inline';

  try {
    const model = document.getElementById('model').value;
    const systemPrompt = document.getElementById('systemPrompt').value.trim();
    const r = await api('/api/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        agent_type: document.getElementById('agentType').value,
        instruction: instruction,
        model: model || null,
        system_prompt: systemPrompt || null,
      })
    });
    if (r.task_id) {
      pollConversation(r.task_id);
      loadTasks();
    } else {
      alert(JSON.stringify(r));
    }
  } catch(e) { alert('Error: ' + e); }
  finally { btn.disabled = false; spinner.style.display = 'none'; }
}

async function previewPrompt() {
  const instruction = document.getElementById('instruction').value.trim();
  if (!instruction) return;
  const model = document.getElementById('model').value.trim();
  try {
    const r = await api('/api/tasks/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        agent_type: document.getElementById('agentType').value,
        instruction: instruction,
        model: model || null,
      })
    });
    const panel = document.getElementById('promptPanel');
    panel.style.display = 'block';
    panel.querySelector('#promptContent').innerHTML = `
      <div class="msg msg-system"><div class="role">System Prompt</div><pre>${esc(r.system_prompt)}</pre></div>
      <div class="msg msg-user"><div class="role">User Message</div><pre>${esc(r.user_message)}</pre></div>
      <p style="margin-top:8px;font-size:0.82em;color:#8b949e">Tools: ${r.tools.join(', ')} | Model: ${r.model}</p>
    `;
    // Pre-fill system prompt textarea with default if empty
    const spEl = document.getElementById('systemPrompt');
    if (!spEl.value.trim()) spEl.value = r.system_prompt;
  } catch(e) { alert('Error: ' + e); }
}

async function viewConversation(taskId) {
  try {
    const r = await api('/api/tasks/' + taskId + '/conversation');
    renderConversation(taskId, r.messages || []);
  } catch(e) {
    const panel = document.getElementById('conversationPanel');
    panel.style.display = 'block';
    document.getElementById('convTaskId').textContent = '(' + taskId.slice(0,8) + ')';
    document.getElementById('conversationContent').innerHTML = '<em>No conversation data yet</em>';
  }
}

function renderConversation(taskId, messages) {
  const panel = document.getElementById('conversationPanel');
  panel.style.display = 'block';
  document.getElementById('convTaskId').textContent = '(' + taskId.slice(0,8) + ')';

  if (!messages.length) {
    document.getElementById('conversationContent').innerHTML = '<em>No messages yet</em>';
    return;
  }

  document.getElementById('conversationContent').innerHTML = messages.map(m => {
    const role = m.role || 'unknown';
    let content = m.content || '';
    if (m.tool_calls) {
      content += '\\n\\nTool calls:\\n' + m.tool_calls.map(tc =>
        tc.function.name + '(' + tc.function.arguments.slice(0,200) + ')'
      ).join('\\n');
    }
    return `<div class="msg msg-${role}"><div class="role">${role}</div><pre>${esc(content)}</pre></div>`;
  }).join('');
}

let pollTimer = null;
function pollConversation(taskId) {
  if (pollTimer) clearInterval(pollTimer);
  viewConversation(taskId);
  pollTimer = setInterval(async () => {
    const task = await api('/api/tasks/' + taskId);
    await viewConversation(taskId);
    if (task.status === 'completed' || task.status === 'failed') {
      clearInterval(pollTimer);
      pollTimer = null;
      loadTasks();
    }
  }, 5000);
}

function esc(s) { if (!s) return ''; return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function deleteTask(taskId) {
  try {
    await api('/api/tasks/' + taskId, { method: 'DELETE' });
    loadTasks();
  } catch(e) { alert('Error deleting task: ' + e); }
}

async function clearAllTasks() {
  if (!confirm('Delete all tasks? This cannot be undone.')) return;
  try {
    await api('/api/tasks', { method: 'DELETE' });
    loadTasks();
  } catch(e) { alert('Error clearing tasks: ' + e); }
}

async function loadModels() {
  try {
    const models = await api('/api/models');
    const el = document.getElementById('model');
    models
      .filter(m => m.downloaded)
      .sort((a, b) => (b.loaded ? 1 : 0) - (a.loaded ? 1 : 0) || a.name.localeCompare(b.name))
      .forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.name;
        const tags = [];
        if (m.loaded) tags.push('loaded');
        if (m.parameter_count) tags.push(m.parameter_count);
        if (m.quantization) tags.push(m.quantization);
        opt.textContent = m.name + (tags.length ? ' (' + tags.join(', ') + ')' : '');
        el.appendChild(opt);
      });
  } catch(e) { console.warn('Failed to load models:', e); }
}

loadModels();
loadTasks();
setInterval(loadTasks, 30000);
</script>
</body>
</html>
"""
