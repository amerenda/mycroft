"""Coordinator — FastAPI application."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from common.config import PlatformConfig
from common.metrics import (
    coordinator_info, tasks_created_total, tasks_completed_total,
    tasks_active, task_duration_seconds, argo_submissions_total,
)
from common.models import TaskConfig, TaskStatus
from coordinator.argo_submitter import ArgoSubmitter
from coordinator.db import CoordinatorDB
from coordinator.task_manager import TaskManager
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
trigger_router: TriggerRouter

# SSE client queues — one per connected browser tab
_sse_clients: list[asyncio.Queue] = []

# ---------------------------------------------------------------------------
# In-memory log buffer
# ---------------------------------------------------------------------------

_LOG_BUFFER: collections.deque = collections.deque(maxlen=2000)


class _UILogHandler(logging.Handler):
    """Captures log records into the ring buffer for the Logs UI tab."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


_ui_log_handler = _UILogHandler()
_ui_log_handler.setFormatter(logging.Formatter("%(message)s"))
# Attach only to the coordinator subtree so we do not duplicate uvicorn/root handlers.
logging.getLogger("coordinator").addHandler(_ui_log_handler)


async def _broadcast_sse(event_type: str, data: dict) -> None:
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    _sse_clients[:] = [q for q in _sse_clients if not q.full()]
    for q in _sse_clients:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


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
    global config, db, task_manager, argo, trigger_router

    config = PlatformConfig()

    # Database
    db = CoordinatorDB(config.kb_dsn)
    await db.connect()

    # Task manager
    task_manager = TaskManager(db.kb)

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

    # Schema migrations (idempotent)
    await db.kb.ensure_tasks_table()

    from coordinator.tool_schemas import ensure_schema_table, seed_default_schemas, ensure_builtin_schemas
    await ensure_schema_table(db.kb.pool)
    await seed_default_schemas(db.kb.pool)
    await ensure_builtin_schemas(db.kb.pool)

    from coordinator.reports import ensure_reports_table
    await ensure_reports_table(db.kb.pool)

    from coordinator.editor_store import (
        ensure_editor_tables, seed_from_filesystem, list_agents as _list_agents,
        ensure_settings_table, seed_default_settings,
    )
    await ensure_editor_tables(db.kb.pool)
    await seed_from_filesystem(db.kb.pool, _AGENTS_DIR, _WORKFLOWS_DIR)
    await ensure_settings_table(db.kb.pool)
    await seed_default_settings(db.kb.pool)

    # Ensure KB schema extensions (expires_at for short-term memory)
    await db.kb.ensure_schema()

    # Register all DB-stored agents into trigger_router so UI-created agents work
    for row in await _list_agents(db.kb.pool):
        trigger_router.register(row["name"], row.get("manifest", ""), row.get("prompts", ""))

    # LISTEN/NOTIFY for agent completion events
    await db.start_listener(_on_agent_event)

    # Periodic heartbeat to llm-manager (keeps app "online")
    _heartbeat_task = None
    if config.llm_manager_api_key:
        _heartbeat_task = asyncio.create_task(_llm_heartbeat_loop(config.llm_manager_url, config.llm_manager_api_key))

    # Periodic cleanup of expired short-term KB records (every hour)
    async def _kb_cleanup_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                await db.kb.cleanup_expired()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("KB cleanup failed: %s", e)

    _cleanup_task = asyncio.create_task(_kb_cleanup_loop())

    log.info("Coordinator started")
    yield

    # Shutdown
    _cleanup_task.cancel()
    if _heartbeat_task:
        _heartbeat_task.cancel()
    await db.close()
    log.info("Coordinator stopped")


app = FastAPI(title="Mycroft Coordinator", lifespan=lifespan)


class _PipelineTaskFailed(Exception):
    pass


async def _wait_for_pipeline_task(task_id: str, timeout: int = 3600) -> str:
    """Poll until a pipeline step task reaches a terminal state. Returns result content."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = await task_manager.get_task(task_id)
        if task and task.status == TaskStatus.completed:
            result = task.result or {}
            return result.get("content", "") if isinstance(result, dict) else str(result)
        if task and task.status == TaskStatus.failed:
            err = (task.result or {}).get("error", "unknown") if isinstance(task.result, dict) else str(task.result)
            raise _PipelineTaskFailed(f"Task {task_id[:8]} failed: {err}")
        await asyncio.sleep(5)
    raise _PipelineTaskFailed(f"Task {task_id[:8]} timed out after {timeout}s")


async def _decompose_instruction(
    instruction: str, n: int, model: str, prompt_template: str,
) -> list[str]:
    """Call LLM to split an instruction into N focused angles. Falls back to N copies on error."""
    import re as _re
    from common.llm import LLMClient

    prompt = prompt_template.format(instruction=instruction, n=n)
    llm = LLMClient(config.llm_manager_url, config.llm_manager_api_key, model)
    try:
        response = await llm.chat(
            [{"role": "user", "content": prompt}],
            tools=None,
            max_tokens=512,
        )
        text = response.content or ""
        match = _re.search(r'\[.*?\]', text, _re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, list) and parsed:
                return [str(a) for a in parsed[:n]]
        log.warning("Decompose: no JSON array in response: %s", text[:200])
    except Exception as e:
        log.warning("Decompose LLM call failed: %s — using original instruction x%d", e, n)
    finally:
        await llm.close()
    return [instruction] * n


async def _submit_pipeline_agent(
    *,
    step: dict,
    instruction: str,
    system_suffix_extra: str = "",
    workflow_name: str,
    run_id: str,
    original_scope: str,
    scratch_scope: str,
    context_injection: list[str],
    phase: str,
    is_last: bool,
    parent_task_id: str | None = None,
    pipeline_max_iter_fallback: int | None = None,
) -> str:
    """Create and submit one pipeline agent task. Returns task_id."""
    agent_type = step.get("agent", "playground")
    step_manifest = trigger_router.get_manifest(agent_type)
    step_prompt = step.get("prompt_override") or trigger_router.get_prompts(agent_type) or None

    from coordinator.editor_store import get_setting as _get_setting
    platform_step_prompt = await _get_setting(db.kb.pool, "pipeline_step_prompt")
    iteration_warning_message = await _get_setting(db.kb.pool, "iteration_warning_message")
    base_prompt_template = await _get_setting(db.kb.pool, "base_system_prompt_template")

    suffix_parts = [p for p in [step_prompt, step.get("system_suffix"), platform_step_prompt, system_suffix_extra] if p]
    combined_suffix = "\n\n".join(suffix_parts) or None

    from common.config import PlatformConfig as _PCap
    _pcap = _PCap()
    step_mi = step.get("max_iterations")
    sm = None if step_mi in (None, "", False) else step_mi
    try:
        step_mi_int = int(sm) if sm is not None else None
    except (TypeError, ValueError):
        step_mi_int = None
    if step_mi_int is not None and step_mi_int >= 1:
        resolved_mi = step_mi_int
    elif pipeline_max_iter_fallback is not None:
        resolved_mi = int(pipeline_max_iter_fallback)
    else:
        resolved_mi = int(step_manifest.max_iterations) if step_manifest else 10
    resolved_mi = min(max(1, resolved_mi), int(_pcap.global_max_iterations))

    resolved_mt: int | None = None
    _step_mt = step.get("max_tokens")
    if _step_mt not in (None, "", False):
        try:
            _mti = int(_step_mt)
            if _mti > 0:
                resolved_mt = _mti
        except (TypeError, ValueError):
            pass
    if resolved_mt is None and step_manifest and step_manifest.max_tokens is not None:
        try:
            _mm = int(step_manifest.max_tokens)
            if _mm > 0:
                resolved_mt = _mm
        except (TypeError, ValueError):
            pass

    task_config = {
        "instruction": instruction,
        "model_override": step.get("model") or None,
        "max_iterations_override": resolved_mi,
        "max_tokens": resolved_mt,
        "tools_override": step.get("tools") or None,
        "context_injection": context_injection,
        "scratch_scope": scratch_scope,
        "step_description": step.get("description") or None,
        "system_suffix": combined_suffix,
        "iteration_warning_message": iteration_warning_message or None,
        "base_system_prompt_template": base_prompt_template or None,
        "phase": phase,
        "is_last_step": is_last,
        "workflow": workflow_name,
        "run_id": run_id,
    }
    if parent_task_id:
        task_config["parent_task_id"] = parent_task_id

    task_id = await task_manager.create_task(
        agent_type=agent_type, instruction=instruction,
        trigger="pipeline", repo="", config=task_config,
    )
    tasks_created_total.labels(agent_type=agent_type, trigger="pipeline").inc()
    tasks_active.labels(agent_type=agent_type).inc()

    model = step.get("model") or None
    params: dict = {"instruction": instruction}
    if model:
        params["model_override"] = model
    wf_name = await argo.submit(
        agent_type=agent_type, task_id=task_id, params=params,
        manifest=step_manifest,
        on_update=_on_workflow_update,
    )
    await db.kb.update_task(task_id, argo_workflow_name=wf_name)
    return task_id


async def _wait_and_collect(task_id: str, output_scope: str, agent_type: str) -> str | None:
    """Wait for one parallel task, copy its result to a /runs/ KB scope. Returns scope or None."""
    import uuid as _uuid
    try:
        await _wait_for_pipeline_task(task_id, timeout=3600)
    except _PipelineTaskFailed as e:
        log.warning("Parallel task %s failed: %s", task_id[:8], e)
        return None
    task = await task_manager.get_task(task_id)
    at = task.agent_type if task else agent_type
    result = await db.kb.get(f"/agents/{at}/results/{task_id}")
    content = (result.content if result else None) or f"(No output from {task_id[:8]})"
    await db.kb.pool.execute(
        """
        INSERT INTO memory_records
            (id, content, scope, categories, metadata, importance, source, needs_embedding, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,false, NOW() + INTERVAL '7 days')
        """,
        str(_uuid.uuid4()), content, output_scope, [], "{}", 0.5, "coordinator",
    )
    log.info("Parallel task %s → %s (%d chars)", task_id[:8], output_scope, len(content))
    return output_scope


async def _run_pipeline_from(
    steps: list[dict],
    step_index: int,
    *,
    instruction: str,
    workflow_name: str,
    run_id: str,
    original_scope: str,
    scratch_scope: str,
    context_scopes: list[str],
    parent_task_id: str | None = None,
    pipeline_max_iter_fallback: int | None = None,
) -> str:
    """Execute steps[step_index] and background the rest. Returns the first task_id launched.

    Step types:
      (default)        — sequential: one agent, waits for it, chains to next step
      type: parallel   — fan-out: N agents in parallel, waits for all, chains with merged context
        n              — number of parallel agents (default 3)
        decompose_model, decompose_prompt — optional LLM call to split instruction into N angles
    """
    step = steps[step_index]
    is_last = step_index == len(steps) - 1
    agent_type = step.get("agent", "playground")

    if step.get("type") == "parallel":
        n = int(step.get("n", 3))
        decompose_model = step.get("decompose_model", "")
        decompose_prompt_tmpl = step.get("decompose_prompt", "")

        if decompose_model and decompose_prompt_tmpl:
            angles = await _decompose_instruction(instruction, n, decompose_model, decompose_prompt_tmpl)
            log.info("Decomposed step %d into %d angles: %s",
                     step_index, len(angles), [a[:60] for a in angles])
        else:
            angles = [instruction] * n

        task_ids_and_scopes: list[tuple[str, str]] = []
        for i, angle in enumerate(angles):
            suffix = f"\n\nSearch focus: {angle}" if angle != instruction else ""
            task_id = await _submit_pipeline_agent(
                step=step, instruction=instruction,
                system_suffix_extra=suffix,
                workflow_name=workflow_name, run_id=run_id,
                original_scope=original_scope, scratch_scope=scratch_scope,
                context_injection=context_scopes,
                phase=f"parallel-{step_index}-{i}",
                is_last=is_last,
                parent_task_id=parent_task_id,
                pipeline_max_iter_fallback=pipeline_max_iter_fallback,
            )
            output_scope = f"/runs/{run_id}/parallel-{step_index}-{i}"
            task_ids_and_scopes.append((task_id, output_scope))
            log.info("Parallel agent %d/%d: agent=%s task=%s angle=%s",
                     i + 1, n, agent_type, task_id[:8], angle[:60])

        first_task_id = task_ids_and_scopes[0][0]

        if not is_last:
            async def _parallel_continuation(
                _tids: list[tuple[str, str]] = task_ids_and_scopes,
                _sidx: int = step_index,
                _parent: str = first_task_id,
            ) -> None:
                try:
                    gathered = await asyncio.gather(*[
                        _wait_and_collect(tid, scope, agent_type)
                        for tid, scope in _tids
                    ])
                    valid = [s for s in gathered if s]
                    if not valid:
                        log.error("All parallel agents failed at step %d — aborting", _sidx)
                        return
                    log.info("Parallel step %d complete: %d/%d succeeded", _sidx, len(valid), len(_tids))
                    await _run_pipeline_from(
                        steps, _sidx + 1,
                        instruction=instruction,
                        workflow_name=workflow_name,
                        run_id=run_id,
                        original_scope=original_scope,
                        scratch_scope=scratch_scope,
                        context_scopes=[original_scope] + valid,
                        parent_task_id=_parent,
                        pipeline_max_iter_fallback=pipeline_max_iter_fallback,
                    )
                except Exception:
                    log.exception("Parallel continuation failed at step %d", _sidx)

            asyncio.create_task(_parallel_continuation())

        return first_task_id

    else:
        # Sequential step
        task_id = await _submit_pipeline_agent(
            step=step, instruction=instruction,
            system_suffix_extra="",
            workflow_name=workflow_name, run_id=run_id,
            original_scope=original_scope, scratch_scope=scratch_scope,
            context_injection=context_scopes,
            phase=f"pipeline-step-{step_index}",
            is_last=is_last,
            pipeline_max_iter_fallback=pipeline_max_iter_fallback,
            parent_task_id=parent_task_id,
        )
        log.info("Pipeline step %d/%d: agent=%s workflow=%s task=%s run=%s",
                 step_index + 1, len(steps), agent_type, workflow_name, task_id[:8], run_id[:8])

        if not is_last:
            async def _sequential_continuation(
                _prev_task_id: str = task_id,
                _sidx: int = step_index,
                _agent_type: str = agent_type,
            ) -> None:
                import uuid as _uuid
                try:
                    await _wait_for_pipeline_task(_prev_task_id, timeout=3600)
                except _PipelineTaskFailed as e:
                    log.warning("Pipeline aborted at step %d: %s", _sidx, e)
                    return
                try:
                    result_record = await db.kb.get(f"/agents/{_agent_type}/results/{_prev_task_id}")
                    prev_content = (result_record.content if result_record else None) or f"(No output from step {_sidx})"
                    prev_scope = f"/runs/{run_id}/step-{_sidx}/output"
                    await db.kb.pool.execute(
                        """
                        INSERT INTO memory_records
                            (id, content, scope, categories, metadata, importance, source, needs_embedding, expires_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,false, NOW() + INTERVAL '7 days')
                        """,
                        str(_uuid.uuid4()), prev_content, prev_scope, [], "{}", 0.5, "coordinator",
                    )
                    await _run_pipeline_from(
                        steps, _sidx + 1,
                        instruction=instruction,
                        workflow_name=workflow_name,
                        run_id=run_id,
                        original_scope=original_scope,
                        scratch_scope=scratch_scope,
                        context_scopes=[original_scope, prev_scope],
                        parent_task_id=_prev_task_id,
                        pipeline_max_iter_fallback=pipeline_max_iter_fallback,
                    )
                except Exception:
                    log.exception("Sequential continuation failed at step %d", _sidx)

            asyncio.create_task(_sequential_continuation())

        return task_id


async def _start_dynamic_pipeline(
    instruction: str,
    workflow_name: str,
    steps: list[dict],
    runner_max_iterations: int | None = None,
) -> str:
    """Start an N-step dynamic pipeline from a DB-stored workflow definition. Returns first task ID."""
    if not steps:
        raise ValueError(f"Workflow '{workflow_name}' has no pipeline steps")

    import uuid as _uuid
    run_id = str(_uuid.uuid4())
    original_scope = f"/runs/{run_id}/original"
    scratch_scope = f"/runs/{run_id}/scratch"

    for scope, content in [(original_scope, instruction), (scratch_scope, "")]:
        await db.kb.pool.execute(
            """
            INSERT INTO memory_records
                (id, content, scope, categories, metadata, importance, source, needs_embedding, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, false, NOW() + INTERVAL '7 days')
            """,
            str(_uuid.uuid4()), content, scope, [], "{}", 0.5, "coordinator",
        )

    first_task_id = await _run_pipeline_from(
        steps, 0,
        instruction=instruction,
        workflow_name=workflow_name,
        run_id=run_id,
        original_scope=original_scope,
        scratch_scope=scratch_scope,
        context_scopes=[original_scope],
        parent_task_id=None,
        pipeline_max_iter_fallback=runner_max_iterations,
    )

    log.info("Pipeline started: workflow=%s steps=%d run=%s first_task=%s",
             workflow_name, len(steps), run_id[:8], first_task_id[:8])
    return first_task_id


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_engineering_task(
    instruction: str, agent_type: str, repo: str,
    model_override: str | None = None, system_prompt_override: str | None = None,
    max_tokens: int | None = None, temperature: float | None = None,
    max_iterations: int | None = None,
    tools_override: list[str] | None = None,
    workflow: str | None = None,
    extra_config: dict[str, Any] | None = None,
) -> str:
    """Handle an engineering task from the API (or dispatch)."""
    manifest = trigger_router.get_manifest(agent_type)
    if not manifest:
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Check concurrency
    if not await task_manager.can_launch(agent_type, manifest.max_concurrent):
        raise ValueError(f"Max concurrent tasks reached for {agent_type}")

    from coordinator.editor_store import get_setting as _get_setting
    iteration_warning_message = await _get_setting(db.kb.pool, "iteration_warning_message")
    base_prompt_template = await _get_setting(db.kb.pool, "base_system_prompt_template")

    # Build task config
    task_config: dict[str, Any] = {}
    if model_override:
        task_config["model_override"] = model_override
    # Explicit API override: canonical JSON keys + TaskConfig field for entrypoint
    if system_prompt_override:
        sp = system_prompt_override.strip()
        task_config["system_prompt"] = sp
        task_config["system_prompt_override"] = sp
    else:
        db_prompt = trigger_router.get_prompts(agent_type) or None
        if db_prompt:
            task_config["system_suffix"] = db_prompt
    if iteration_warning_message:
        task_config["iteration_warning_message"] = iteration_warning_message
    if base_prompt_template:
        task_config["base_system_prompt_template"] = base_prompt_template
    _mt = max_tokens
    if _mt is None and manifest.max_tokens is not None:
        _mt = manifest.max_tokens
    if _mt is not None:
        try:
            _mti = int(_mt)
            if _mti > 0:
                task_config["max_tokens"] = _mti
        except (TypeError, ValueError):
            pass
    if temperature is not None:
        task_config["temperature"] = temperature
    if workflow:
        task_config["workflow"] = workflow
    if tools_override:
        task_config["tools_override"] = tools_override
    if extra_config:
        for k, v in extra_config.items():
            if v is not None:
                task_config[k] = v

    import uuid as _uuid
    task_config["scratch_scope"] = f"/scratch/{_uuid.uuid4()}"

    from common.config import PlatformConfig as _PCo
    _pco = _PCo()
    _raw_mi = max_iterations if max_iterations is not None else manifest.max_iterations
    try:
        _mi = int(_raw_mi)
    except (TypeError, ValueError):
        _mi = int(manifest.max_iterations)
    task_config["max_iterations_override"] = min(max(1, _mi), int(_pco.global_max_iterations))

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

    current = await task_manager.get_task(task_id)

    if status in ("failed", "error"):
        # Argo is the source of truth for failure — override even a runtime-reported
        # completed status (pod can crash after the agent writes its result)
        if current and current.status == TaskStatus.failed:
            log.debug("Ignoring duplicate failure for task %s", task_id[:8])
            return
        was_already_terminal = current and current.status in (TaskStatus.completed, TaskStatus.failed)
        await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": message})
        task = await task_manager.get_task(task_id)
        if task and not was_already_terminal:
            tasks_active.labels(agent_type=task.agent_type).dec()
            tasks_completed_total.labels(agent_type=task.agent_type, status="failed").inc()
        log.warning("Task %s failed (Argo): %s", task_id[:8], message)
        await _broadcast_sse("task_update", {
            "task_id": task_id, "status": "failed",
            "agent_type": task.agent_type if task else "",
            "error": message[:200] if message else "",
        })

    elif status == "succeeded":
        if current and current.status in (TaskStatus.completed, TaskStatus.failed):
            log.debug("Ignoring Argo succeeded for already-terminal task %s (%s)", task_id[:8], current.status)
            return
        await db.kb.update_task(task_id, status=TaskStatus.completed)
        task = await task_manager.get_task(task_id)
        if task:
            tasks_active.labels(agent_type=task.agent_type).dec()
            tasks_completed_total.labels(agent_type=task.agent_type, status="completed").inc()
        await _broadcast_sse("task_update", {"task_id": task_id, "status": "completed",
                                             "agent_type": task.agent_type if task else ""})


async def _on_agent_event(event: dict[str, Any]) -> None:
    """Handle PG NOTIFY from agent completion (reports, logging)."""
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

        # Researcher results → save locally, optionally post to Sazed
        if is_researcher:
            await _handle_researcher_result(record, source)

    # Notifications — log only
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
    """Save researcher result to local DB and optionally post to Sazed."""
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
    workflow = (task.config.get("workflow", "") if task else "") or ""
    models_used: dict[str, str] = {}
    commit_sha = getattr(config, "agent_image_tag", "") or ""

    if task:
        model_override = task.config.get("model_override", "")
        if model_override:
            models_used["model"] = model_override

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
        await _broadcast_sse("report_saved", {"title": title, "task_id": task_id})
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

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    agent_type: str = ""
    instruction: str = ""
    repo: str = ""
    trigger: str = "manual"
    model: str | None = None
    system_prompt: str | None = None
    user_message: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    max_iterations: int | None = None
    workflow: str | None = None
    tools: list[str] | None = None
    tools_override: list[str] | None = None
    permissions: dict[str, Any] | None = None
    kb_recall: bool | None = None
    kb_recall_scopes: list[str] | None = None
    kb_recall_limit: int | None = None
    effort: str | None = None
    empty_response_nudge: str | None = None
    iteration_limit_reply: str | None = None


def _extra_task_config_from_playground(req: CreateTaskRequest) -> dict[str, Any]:
    """Fields stored in agent_tasks.config / inbox metadata (UI overrides)."""
    extra: dict[str, Any] = {}
    if req.user_message is not None:
        extra["user_message"] = req.user_message
    if req.tools is not None:
        extra["tools"] = req.tools
    elif req.tools_override is not None:
        extra["tools_override"] = req.tools_override
    if req.permissions is not None:
        extra["permissions"] = req.permissions
    if req.kb_recall is not None:
        extra["kb_recall"] = req.kb_recall
    if req.kb_recall_scopes is not None:
        extra["kb_recall_scopes"] = req.kb_recall_scopes
    if req.kb_recall_limit is not None:
        extra["kb_recall_limit"] = req.kb_recall_limit
    if req.effort is not None:
        extra["effort"] = req.effort
    if req.empty_response_nudge is not None:
        extra["empty_response_nudge"] = req.empty_response_nudge
    if req.iteration_limit_reply is not None:
        extra["iteration_limit_reply"] = req.iteration_limit_reply
    return extra


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    try:
        workflow = req.workflow or None

        # Workflow name — look up in DB and run as dynamic pipeline
        if workflow:
            from coordinator.editor_store import get_workflow as _get_wf
            wf_def = await _get_wf(db.kb.pool, workflow)
            if not wf_def:
                raise ValueError(f"Unknown workflow: '{workflow}'")
            pipeline_json = wf_def.get("pipeline_json") or {}
            steps = pipeline_json.get("steps", [])
            if not steps:
                raise ValueError(f"Workflow '{workflow}' has no pipeline steps defined")
            first_task_id = await _start_dynamic_pipeline(
                req.instruction, workflow, steps, runner_max_iterations=req.max_iterations,
            )
            return {"task_id": first_task_id}

        # Direct agent task (test button, API callers)
        agent_type = req.agent_type
        if not agent_type:
            raise ValueError("workflow or agent_type is required")
        if not (req.instruction or "").strip() and not (req.user_message or "").strip():
            raise HTTPException(400, "Provide non-empty instruction or user_message")
        extra = _extra_task_config_from_playground(req)
        tools_for_handle = None if req.tools is not None else req.tools_override
        task_id = await _handle_engineering_task(
            instruction=req.instruction,
            agent_type=agent_type,
            repo=req.repo,
            model_override=req.model,
            system_prompt_override=req.system_prompt,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            max_iterations=req.max_iterations,
            tools_override=tools_for_handle,
            workflow=None,
            extra_config=extra,
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


@app.get("/api/events")
async def sse_stream():
    """Server-Sent Events stream — broadcasts task and report updates to connected clients."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_clients.append(queue)

    async def stream():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            try:
                _sse_clients.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/logs")
async def api_logs(
    logger: str | None = None,
    level: str | None = None,
    q: str | None = None,
    since: float | None = None,
    limit: int = 500,
):
    """Return recent log records from the in-memory ring buffer.

    Query params:
      logger — filter by logger name prefix (e.g. "coordinator", "runtime")
      level  — minimum level: DEBUG, INFO, WARNING, ERROR
      q      — substring search in message text
      since  — Unix timestamp; return only records newer than this
      limit  — max records (default 500, max 2000)
    """
    level_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_level = level_order.get((level or "").upper(), 0)
    limit = min(max(1, limit), 2000)

    records = list(_LOG_BUFFER)
    if since is not None:
        records = [r for r in records if r["ts"] > since]
    if logger:
        records = [r for r in records if r["logger"].startswith(logger)]
    if min_level:
        records = [r for r in records if level_order.get(r["level"], 0) >= min_level]
    if q:
        q_lower = q.lower()
        records = [r for r in records if q_lower in r["msg"].lower()]

    return records[-limit:]


@app.get("/api/tasks/{task_id}/pipeline")
async def get_pipeline_chain(task_id: str):
    """Return all tasks in the same pipeline chain, sorted oldest-first."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    # Walk up to root via parent_task_id
    root = task
    visited: set[str] = set()
    while root.config.get("parent_task_id") and root.id not in visited:
        visited.add(root.id)
        parent = await task_manager.get_task(root.config["parent_task_id"])
        if not parent:
            break
        root = parent

    # Scan recent tasks to find all descendants of root
    all_tasks = await task_manager.list_tasks(limit=200)
    chain = [root]
    seen = {root.id}
    changed = True
    while changed:
        changed = False
        for t in all_tasks:
            if t.id not in seen and t.config.get("parent_task_id") in seen:
                chain.append(t)
                seen.add(t.id)
                changed = True

    chain.sort(key=lambda t: t.created_at or 0)
    return [t.model_dump() for t in chain]


@app.get("/api/tasks/{task_id}/kb-result")
async def get_task_kb_result(task_id: str):
    """Fetch the KB record written by an agent upon task completion."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    agent_type = task.agent_type.replace("_", "-")
    scope = f"/agents/{agent_type}/results/{task_id}"
    record = await db.kb.get(scope)
    if not record:
        raise HTTPException(404, f"No KB result at {scope}")
    return {"scope": scope, "content": record.content,
            "created_at": str(record.created_at) if record.created_at else None}


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
    agent_type: str = "playground"
    instruction: str = ""
    model: str | None = None
    max_iterations: int | None = None
    max_tokens: int | None = None
    system_prompt: str | None = None
    user_message: str | None = None
    tools: list[str] | None = None
    tools_override: list[str] | None = None
    permissions: dict[str, Any] | None = None
    kb_recall: bool | None = None
    kb_recall_scopes: list[str] | None = None
    kb_recall_limit: int | None = None
    effort: str | None = None
    empty_response_nudge: str | None = None
    iteration_limit_reply: str | None = None


@app.post("/api/tasks/test")
async def test_task(req: TestTaskRequest):
    """Preview resolved system/user prompts and tools (same resolution as runtime)."""
    from coordinator.editor_store import get_setting as _get_setting
    from runtime.task_prompts import (
        resolve_initial_user_message_sync,
        resolve_system_prompt,
        resolve_tool_names,
    )
    from runtime.tools.base import load_tools

    if not (req.instruction or "").strip() and not (req.user_message or "").strip():
        raise HTTPException(400, "Provide non-empty instruction or user_message")

    manifest = trigger_router.get_manifest(req.agent_type)
    if not manifest:
        raise HTTPException(400, f"Unknown agent type: {req.agent_type}")

    if req.model:
        manifest = manifest.model_copy()
        manifest.model = req.model

    platform_cfg = PlatformConfig()
    if req.max_iterations is not None:
        _raw_prev = int(req.max_iterations)
    else:
        _raw_prev = int(manifest.max_iterations)
    max_iter = min(max(1, _raw_prev), int(platform_cfg.global_max_iterations))
    if req.max_tokens is not None and req.max_tokens > 0:
        max_tokens_eff = int(req.max_tokens)
    elif manifest.max_tokens is not None:
        try:
            max_tokens_eff = max(1, int(manifest.max_tokens))
        except (TypeError, ValueError):
            max_tokens_eff = 4096
    else:
        max_tokens_eff = 4096
    base_tpl = await _get_setting(db.kb.pool, "base_system_prompt_template")

    task = TaskConfig(
        id="preview",
        agent_type=req.agent_type,
        instruction=req.instruction or "",
        model_override=req.model,
        max_iterations_override=req.max_iterations,
        config={},
    )
    if base_tpl:
        task.config["base_system_prompt_template"] = base_tpl

    extra = _extra_task_config_from_playground(
        CreateTaskRequest(
            agent_type=req.agent_type,
            instruction=req.instruction or "",
            model=req.model,
            system_prompt=req.system_prompt,
            user_message=req.user_message,
            tools=req.tools,
            tools_override=req.tools_override,
            permissions=req.permissions,
            kb_recall=req.kb_recall,
            kb_recall_scopes=req.kb_recall_scopes,
            kb_recall_limit=req.kb_recall_limit,
            effort=req.effort,
            empty_response_nudge=req.empty_response_nudge,
            iteration_limit_reply=req.iteration_limit_reply,
        )
    )
    task.config.update(extra)

    if req.system_prompt and req.system_prompt.strip():
        sp = req.system_prompt.strip()
        task.config["system_prompt"] = sp
        task.config["system_prompt_override"] = sp
        task.system_prompt_override = sp
    else:
        db_prompt = trigger_router.get_prompts(req.agent_type) or None
        if db_prompt:
            task.config["system_suffix"] = db_prompt

    tool_names = resolve_tool_names(task, manifest)
    tools = load_tools(tool_names, web_read_max_chars=manifest.web_read_max_chars)
    system_prompt = resolve_system_prompt(
        task, manifest, tools.schemas(), max_iterations=max_iter,
    )
    if task.config.get("system_suffix"):
        system_prompt = system_prompt.rstrip() + "\n\n" + task.config["system_suffix"]
    user_message = resolve_initial_user_message_sync(task, [])

    if task.config.get("system_prompt") or task.system_prompt_override:
        source = "override"
    elif task.config.get("system_suffix"):
        source = "built-in+db_suffix"
    else:
        source = "built-in"

    return {
        "agent_type": req.agent_type,
        "model": manifest.model,
        "system_prompt": system_prompt,
        "system_prompt_source": source,
        "user_message": user_message,
        "tools": [t["function"]["name"] for t in tools.schemas()],
        "max_iterations_effective": max_iter,
        "max_tokens_effective": max_tokens_eff,
    }


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
async def api_list_reports(limit: int = 50, source_task_id: str | None = None):
    return await list_reports(db.kb.pool, limit, source_task_id=source_task_id)


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
    fetch_tool_groups,
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
    group: str = ""


@app.put("/api/tools/schemas/{name}")
async def api_upsert_schema(name: str, req: UpsertSchemaRequest):
    new_version = await upsert_schema(
        db.kb.pool, name, req.schema, req.schema_version, req.changelog, req.updated_by,
        group=req.group,
    )
    return {"name": name, "version": new_version}


@app.get("/api/tools/groups")
async def api_tool_groups():
    """Return group_name -> [tool_names] map derived from tool schema group assignments."""
    return await fetch_tool_groups(db.kb.pool)


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


@app.get("/api/agents/{name}/effective-prompt")
async def agent_effective_prompt(
    name: str,
    pipeline: bool = False,
    is_last_step: bool = False,
):
    """Return the exact system prompt and tool list the model would receive.

    pipeline=true simulates a pipeline context (adds auto-injected tools).
    Auto-injected for all pipeline steps: scratch_read, scratch_write, finish.
    submit_report must be declared explicitly in the agent's tool list.
    """
    _safe_name(name)
    from runtime.context import build_system_prompt
    from runtime.tools.base import load_tools

    manifest = trigger_router.get_manifest(name)
    if not manifest:
        raise HTTPException(404, f"Agent '{name}' not found or not loaded")

    # Simulate pipeline tool injection if requested
    scratch_scope = "/runs/preview/scratch" if pipeline else None
    tools = load_tools(
        manifest.tools,
        kb_dsn="preview" if pipeline else None,
        scratch_scope=scratch_scope,
        is_last_step=is_last_step,
        web_read_max_chars=manifest.web_read_max_chars,
    )

    db_prompt = trigger_router.get_prompts(name)
    if db_prompt:
        system_prompt = db_prompt
        source = "db"
    else:
        system_prompt = build_system_prompt(manifest, tools.schemas())
        source = "built-in"

    manifest_tools = list(manifest.tools)
    auto_injected = ["scratch_read", "scratch_write", "finish"] if pipeline else []
    if manifest.max_tokens is not None:
        try:
            max_tokens_eff = max(1, int(manifest.max_tokens))
        except (TypeError, ValueError):
            max_tokens_eff = 4096
    else:
        max_tokens_eff = 4096

    return {
        "agent": name,
        "system_prompt": system_prompt,
        "source": source,
        "tools": [t["function"]["name"] for t in tools.schemas()],
        "manifest_tools": manifest_tools,
        "auto_injected_tools": auto_injected,
        "pipeline": pipeline,
        "is_last_step": is_last_step,
        "max_tokens_effective": max_tokens_eff,
    }


@app.put("/api/agents/{name}")
async def save_agent(name: str, payload: AgentPayload):
    _safe_name(name)
    from coordinator.editor_store import save_agent as _save_agent, slugify
    saved = await _save_agent(db.kb.pool, name, payload.manifest, payload.prompts)
    canonical = saved.get("name") or slugify(name)
    trigger_router.register(canonical, payload.manifest, payload.prompts or "")
    return {
        "status": "saved",
        "name": canonical,
        "updated_at": str(saved.get("updated_at")) if saved.get("updated_at") else None,
    }


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


@app.get("/api/workflows/{name}/runs")
async def get_workflow_runs(name: str, limit: int = 20):
    """List recent task executions for a named workflow."""
    _safe_name(name)
    async with db.kb.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agent_tasks WHERE config->>'workflow' = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            name, limit,
        )
    from common.models import TaskRecord
    results = []
    for r in rows:
        results.append({
            "id": str(r["id"]),
            "agent_type": r["agent_type"],
            "status": r["status"],
            "trigger": r["trigger"],
            "config": json.loads(r["config"]) if r["config"] else {},
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "completed_at": str(r["completed_at"]) if r["completed_at"] else None,
        })
    return results


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
# Platform Settings API
# ---------------------------------------------------------------------------


@app.get("/api/settings")
async def list_settings():
    from coordinator.editor_store import list_settings as _list_settings
    return await _list_settings(db.kb.pool)


@app.put("/api/settings/{key}")
async def save_setting(key: str, payload: dict):
    from coordinator.editor_store import save_setting as _save_setting
    try:
        await _save_setting(db.kb.pool, key, payload.get("value", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved", "key": key}


# ---------------------------------------------------------------------------
# KB Explorer API
# ---------------------------------------------------------------------------


class KBUpsertRequest(BaseModel):
    path: str
    content: str
    metadata: dict = {}
    source: str = "ui"
    needs_embedding: bool = True


@app.get("/api/kb/children")
async def kb_children(path: str = "/", since_minutes: int | None = None, limit: int = 500):
    """List immediate children (dirs + entries) under a KB path prefix."""
    children = await db.kb.list_children(path, since_minutes=since_minutes, limit=min(limit, 2000))
    return children


@app.get("/api/kb/count")
async def kb_count(path: str = "/"):
    """Count distinct scopes under a KB path prefix."""
    count = await db.kb.count_by_prefix(path)
    return {"path": path, "count": count}


@app.get("/api/kb/entry")
async def kb_get_entry(path: str):
    """Get the most recent KB record at the exact scope."""
    record = await db.kb.get_by_scope(path)
    if not record:
        raise HTTPException(404, f"No KB entry at {path!r}")
    return record


@app.put("/api/kb/entry")
async def kb_put_entry(req: KBUpsertRequest):
    """Write/replace a KB entry."""
    record_id = await db.kb.upsert_by_scope(
        req.path, req.content, req.source, req.metadata, req.needs_embedding
    )
    return {"status": "ok", "id": record_id, "path": req.path}


@app.delete("/api/kb/entry")
async def kb_delete_entry(path: str):
    """Delete all records at the exact scope."""
    count = await db.kb.delete_by_scope(path)
    if count == 0:
        raise HTTPException(404, f"No KB entry at {path!r}")
    return {"status": "deleted", "path": path, "count": count}


@app.delete("/api/kb/subtree")
async def kb_delete_subtree(path: str):
    """Delete all records under a path prefix."""
    if path in ("/", ""):
        raise HTTPException(400, "Cannot delete root")
    count = await db.kb.delete_by_prefix(path)
    return {"status": "deleted", "path": path, "count": count}


@app.get("/api/kb/task/{task_id}")
async def kb_for_task(task_id: str):
    """Return all KB records associated with a task (direct + written during run)."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    records = await db.kb.list_records_for_task(
        task_id,
        agent_type=task.agent_type,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )
    return {
        "task_id": task_id,
        "agent_type": task.agent_type,
        "started_at": str(task.started_at) if task.started_at else None,
        "completed_at": str(task.completed_at) if task.completed_at else None,
        "records": records,
    }


# ---------------------------------------------------------------------------
# Open WebUI / chat — synchronous tool execution in coordinator (no Argo pods)
# ---------------------------------------------------------------------------

class ToolsRunRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


@app.post("/api/tools/run-tool")
async def tools_run_tool(req: ToolsRunRequest):
    """Execute a tool synchronously in-process. For use by Open WebUI / chat clients."""
    from runtime.tools.web import WebSearch, WebRead, WikiRead
    from runtime.tools.shell import RunCommand

    tool_name = req.tool
    args = req.args

    if tool_name == "web_search":
        tool = WebSearch()
        result = await tool.execute(args)

    elif tool_name == "web_read":
        tool = WebRead()
        result = await tool.execute(args)

    elif tool_name == "wiki_read":
        tool = WikiRead()
        result = await tool.execute(args)

    elif tool_name == "run_command":
        workspace = args.pop("workspace", "/tmp/mycroft-tools-workspace")
        import os
        os.makedirs(workspace, exist_ok=True)
        tool = RunCommand(workspace=workspace)
        result = await tool.execute(args)

    elif tool_name == "kb_search":
        query = args.get("query", "")
        scopes = args.get("scopes", ["/"])
        limit = int(args.get("limit", 5))
        records = await db.kb.recall(query, scopes, limit=limit)
        result = "\n\n".join(
            f"[{r.scope}] (score relevance)\n{r.content}" for r in records
        ) or "(no results)"

    elif tool_name == "kb_write":
        scope = args.get("scope", "")
        content = args.get("content", "")
        if not scope or not content:
            raise HTTPException(400, "scope and content are required")
        record_id = await db.kb.write(
            scope,
            content,
            importance=float(args.get("importance", 0.5)),
            source="tools_api",
        )
        result = f"Written to KB: {scope} (id={record_id})"

    else:
        raise HTTPException(
            400,
            f"Unknown tool: {tool_name!r}. "
            "Available: web_search, web_read, wiki_read, run_command, kb_search, kb_write",
        )

    return {"tool": tool_name, "result": result}


# ---------------------------------------------------------------------------
# Debug UI
# ---------------------------------------------------------------------------

from fastapi.responses import HTMLResponse

@app.get("/debug", response_class=HTMLResponse)
async def debug_page():
    """Simple debug UI for testing agents."""
    return DEBUG_HTML


@app.get("/api/config")
async def get_config():
    """Public runtime config for the frontend."""
    return {
        "argo_ui_url": config.argo_ui_url,
        "argo_namespace": config.argo_namespace,
    }


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
