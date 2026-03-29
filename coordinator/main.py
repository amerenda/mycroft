"""Coordinator — FastAPI application."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from common.config import PlatformConfig
from common.llm import LLMClient
from common.models import IntentType, TaskStatus
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

    # LISTEN/NOTIFY for agent completion events
    await db.start_listener(_on_agent_event)

    log.info("Coordinator started")
    yield

    # Shutdown
    if config.telegram_bot_token:
        await telegram_bot.stop_polling()
    await db.close()
    await llm.close()
    log.info("Coordinator stopped")


app = FastAPI(title="Mycroft Coordinator", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_engineering_task(instruction: str, agent_type: str, repo: str) -> str:
    """Handle an engineering task from Telegram or API."""
    manifest = trigger_router.get_manifest(agent_type)
    if not manifest:
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Check concurrency
    if not await task_manager.can_launch(agent_type, manifest.max_concurrent):
        raise ValueError(f"Max concurrent tasks reached for {agent_type}")

    # Create task
    task_id = await task_manager.create_task(
        agent_type=agent_type,
        instruction=instruction,
        trigger="telegram",
        repo=repo,
    )

    # Submit Argo Workflow
    try:
        wf_name = await argo.submit(
            agent_type=agent_type,
            task_id=task_id,
            params={"instruction": instruction, "repo": repo},
        )
        log.info("Workflow %s submitted for task %s", wf_name, task_id[:8])
    except Exception as e:
        log.error("Failed to submit Argo Workflow: %s", e)
        # Task is created but workflow failed — update status
        await db.kb.update_task(task_id, status=TaskStatus.failed, result={"error": f"Workflow submission failed: {e}"})
        raise

    return task_id


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
    """Handle PG NOTIFY from agent completion."""
    scope = event.get("scope", "")
    source = event.get("source", "")

    # Check if this is an agent result
    if "/results/" in scope:
        record = await db.kb.get(scope)
        if record and telegram_bot:
            # Extract PR URL if present
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

    # Check if this is a notification
    elif scope.startswith("/notifications/alex/"):
        record = await db.kb.get(scope)
        if record and telegram_bot:
            try:
                await telegram_bot.send(record.content)
            except Exception:
                log.warning("Failed to send Telegram notification", exc_info=True)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    agent_type: str
    instruction: str
    repo: str = ""
    trigger: str = "manual"


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    try:
        task_id = await _handle_engineering_task(
            instruction=req.instruction,
            agent_type=req.agent_type,
            repo=req.repo,
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


@app.post("/webhooks/telegram")
async def telegram_webhook(request: Request):
    """Telegram webhook endpoint."""
    if not telegram_bot.app:
        raise HTTPException(503, "Telegram bot not configured")

    from telegram import Update
    data = await request.json()
    update = Update.de_json(data, telegram_bot.app.bot)
    await telegram_bot.app.process_update(update)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
