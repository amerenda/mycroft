"""Task lifecycle management — wraps KB operations."""

from __future__ import annotations

import json
import logging
from typing import Any

from common.kb import KBClient
from common.models import TaskRecord, TaskStatus

log = logging.getLogger(__name__)


class TaskManager:
    """Create, track, and manage agent tasks."""

    def __init__(self, kb: KBClient):
        self.kb = kb

    async def create_task(
        self,
        agent_type: str,
        instruction: str,
        *,
        trigger: str = "manual",
        trigger_ref: str = "",
        repo: str = "",
        config: dict[str, Any] | None = None,
    ) -> str:
        """Create a task and write the instruction to the agent's inbox."""
        merged_config = config or {}
        merged_config["instruction"] = instruction
        if repo:
            merged_config["repo"] = repo

        # Create task record
        task_id = await self.kb.create_task(
            agent_type=agent_type,
            trigger=trigger,
            trigger_ref=trigger_ref,
            config=merged_config,
        )

        # Write instruction to agent inbox
        await self.kb.write(
            scope=f"/agents/{agent_type}/inbox/{task_id}",
            content=instruction,
            metadata=merged_config,
            source=f"coordinator/{trigger}",
            needs_embedding=False,
        )

        log.info("Task %s created: type=%s trigger=%s", task_id[:8], agent_type, trigger)
        return task_id

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self.kb.get_task(task_id)

    async def list_tasks(
        self,
        agent_type: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        return await self.kb.list_tasks(
            agent_type=agent_type,
            status=status,
            limit=limit,
        )

    async def can_launch(self, agent_type: str, max_concurrent: int) -> bool:
        """Check if we can launch another task of this type."""
        running = await self.kb.count_running_tasks(agent_type)
        return running < max_concurrent
