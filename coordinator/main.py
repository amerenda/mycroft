"""Coordinator — FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
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
    )

    # Tool schema versioning
    from common.tool_schemas import ensure_schema_table, seed_default_schemas
    await ensure_schema_table(db.kb.pool)
    await seed_default_schemas(db.kb.pool)

    # Reports table
    from common.reports import ensure_reports_table
    await ensure_reports_table(db.kb.pool)

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
    await db.close()
    log.info("Coordinator stopped")


app = FastAPI(title="Mycroft Coordinator", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_engineering_task(
    instruction: str, agent_type: str, repo: str,
    model_override: str | None = None, system_prompt_override: str | None = None,
    max_tokens: int | None = None, temperature: float | None = None,
    max_iterations: int | None = None,
    tools_override: list[str] | None = None,
    extra_config: dict[str, Any] | None = None,
) -> str:
    """Handle a task from the API (create KB record + submit Argo)."""
    manifest = trigger_router.get_manifest(agent_type)
    if not manifest:
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Check concurrency
    if not await task_manager.can_launch(agent_type, manifest.max_concurrent):
        raise ValueError(f"Max concurrent tasks reached for {agent_type}")

    # Build task config
    task_config: dict[str, Any] = {}
    if model_override:
        task_config["model_override"] = model_override
    if system_prompt_override:
        sp = system_prompt_override.strip()
        task_config["system_prompt"] = sp
        task_config["system_prompt_override"] = sp
    if max_tokens is not None:
        task_config["max_tokens"] = max_tokens
    if temperature is not None:
        task_config["temperature"] = temperature
    if max_iterations is not None:
        task_config["max_iterations_override"] = max_iterations
    if tools_override:
        task_config["tools_override"] = tools_override
    if extra_config:
        for k, v in extra_config.items():
            if v is not None:
                task_config[k] = v

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
                on_update=_on_workflow_update,
            )
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
        log.info("Task %s completed: %s", task_id[:8], message)


async def _on_agent_event(event: dict[str, Any]) -> None:
    """Handle PG NOTIFY from agent completion (optional Sazed export)."""
    scope = event.get("scope", "")
    source = event.get("source", "")

    if "/results/" in scope:
        record = await db.kb.get(scope)
        if not record:
            return

        if config.sazed_url:
            await _post_to_sazed(record, source)
        else:
            log.info(
                "Agent result: %s — %d chars",
                source,
                len(record.content or ""),
            )

    elif scope.startswith("/notifications/alex/"):
        record = await db.kb.get(scope)
        if record:
            log.info("Agent notification: %s", record.content[:200])


async def _post_to_sazed(record, source: str) -> None:
    """Post an agent result to Sazed when SAZED_URL is configured."""
    import httpx

    content = record.content or ""

    title = "Agent output"
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            title = line.removeprefix("# ").strip()
            break
        elif line and not line.startswith("#"):
            title = line[:80]
            break

    # Summary: first paragraph or first 300 chars
    summary = ""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## summary"):
            # Grab text after the ## Summary heading
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

    task_id = source.split("/")[-1] if "/" in source else ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{config.sazed_url}/api/reports",
                json={
                    "title": title,
                    "content": content,
                    "summary": summary,
                    "tags": [],
                    "source_task_id": task_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            report_slug = data.get("slug", data.get("id", ""))
            report_url = f"{config.sazed_url}/r/{report_slug}"

        log.info("Report posted to Sazed: %s", report_url)

    except Exception as e:
        log.error("Failed to post report to Sazed: %s", e)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    agent_type: str
    instruction: str = ""
    repo: str = ""
    trigger: str = "manual"
    model: str | None = None
    system_prompt: str | None = None
    user_message: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    max_iterations: int | None = None
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
        if not (req.instruction or "").strip() and not (req.user_message or "").strip():
            raise HTTPException(400, "Provide non-empty instruction or user_message")
        extra = _extra_task_config_from_playground(req)
        tools_for_handle = None if req.tools is not None else req.tools_override
        task_id = await _handle_engineering_task(
            instruction=req.instruction,
            agent_type=req.agent_type,
            repo=req.repo,
            model_override=req.model,
            system_prompt_override=req.system_prompt,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            max_iterations=req.max_iterations,
            tools_override=tools_for_handle,
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


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": "Cancelled by user"})
    return {"status": "cancelled"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
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
    from runtime.task_prompts import (
        resolve_initial_user_message_sync,
        resolve_system_prompt,
        resolve_tool_names,
    )
    from runtime.tools.base import load_tools

    manifest = trigger_router.get_manifest(req.agent_type)
    if not manifest:
        raise HTTPException(400, f"Unknown agent type: {req.agent_type}")

    if req.model:
        manifest = manifest.model_copy()
        manifest.model = req.model

    task = TaskConfig(
        id="preview",
        agent_type=req.agent_type,
        instruction=req.instruction,
        model_override=req.model,
        config={},
    )
    extra = _extra_task_config_from_playground(
        CreateTaskRequest(
            agent_type=req.agent_type,
            instruction=req.instruction,
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
    if req.system_prompt:
        task.config["system_prompt"] = req.system_prompt.strip()
        task.config["system_prompt_override"] = req.system_prompt.strip()
        task.system_prompt_override = req.system_prompt.strip()

    tool_names = resolve_tool_names(task, manifest)
    tools = load_tools(tool_names)
    system_prompt = resolve_system_prompt(task, manifest, tools.schemas())
    user_message = resolve_initial_user_message_sync(task, [])

    return {
        "agent_type": req.agent_type,
        "model": manifest.model,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "tools": [t["function"]["name"] for t in tools.schemas()],
    }


# ---------------------------------------------------------------------------
# Reports API
# ---------------------------------------------------------------------------

from common.reports import (
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

from common.tool_schemas import (
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
    content: str


@app.get("/api/agents")
async def list_agents():
    agents = []
    for d in sorted(_AGENTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        manifest_path = d / "manifest.yaml"
        prompts_path = d / "prompts.py"
        agents.append({
            "name": d.name,
            "manifest": manifest_path.read_text() if manifest_path.exists() else "",
            "prompts": prompts_path.read_text() if prompts_path.exists() else "",
        })
    return agents


@app.get("/api/agents/{name}")
async def get_agent(name: str):
    _safe_name(name)
    d = _AGENTS_DIR / name
    if not d.is_dir():
        raise HTTPException(404, f"Agent '{name}' not found")
    manifest_path = d / "manifest.yaml"
    prompts_path = d / "prompts.py"
    return {
        "name": name,
        "manifest": manifest_path.read_text() if manifest_path.exists() else "",
        "prompts": prompts_path.read_text() if prompts_path.exists() else "",
    }


@app.put("/api/agents/{name}")
async def save_agent(name: str, payload: AgentPayload):
    _safe_name(name)
    d = _AGENTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.yaml").write_text(payload.manifest)
    if payload.prompts:
        (d / "prompts.py").write_text(payload.prompts)
    return {"status": "saved", "name": name}


@app.delete("/api/agents/{name}")
async def delete_agent(name: str):
    _safe_name(name)
    d = _AGENTS_DIR / name
    if not d.is_dir():
        raise HTTPException(404, f"Agent '{name}' not found")
    import shutil
    shutil.rmtree(d)
    return {"status": "deleted", "name": name}


@app.get("/api/workflows")
async def list_workflows():
    _WORKFLOWS_DIR.mkdir(exist_ok=True)
    workflows = []
    for f in sorted(_WORKFLOWS_DIR.glob("*.yaml")):
        workflows.append({"name": f.stem, "content": f.read_text()})
    return workflows


@app.get("/api/workflows/{name}")
async def get_workflow(name: str):
    _safe_name(name)
    f = _WORKFLOWS_DIR / f"{name}.yaml"
    if not f.exists():
        raise HTTPException(404, f"Workflow '{name}' not found")
    return {"name": name, "content": f.read_text()}


@app.put("/api/workflows/{name}")
async def save_workflow(name: str, payload: WorkflowPayload):
    _safe_name(name)
    _WORKFLOWS_DIR.mkdir(exist_ok=True)
    (_WORKFLOWS_DIR / f"{name}.yaml").write_text(payload.content)
    return {"status": "saved", "name": name}


@app.delete("/api/workflows/{name}")
async def delete_workflow(name: str):
    _safe_name(name)
    f = _WORKFLOWS_DIR / f"{name}.yaml"
    if not f.exists():
        raise HTTPException(404, f"Workflow '{name}' not found")
    f.unlink()
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
