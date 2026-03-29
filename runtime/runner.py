"""The thin agent loop — ~250 lines, the entire agent framework."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from common.config import PlatformConfig
from common.kb import KBClient
from common.llm import LLMClient
from common.models import AgentManifest, TaskConfig, TaskStatus
from runtime.context import build_system_prompt, build_user_message, count_tool_rounds
from runtime.tools.base import ToolRegistry, load_tools

log = logging.getLogger(__name__)


class AgentRunner:
    """The thin agent loop. Handles one task end-to-end."""

    def __init__(
        self,
        manifest: AgentManifest,
        task: TaskConfig,
        platform: PlatformConfig,
    ):
        self.manifest = manifest
        self.task = task
        self.platform = platform

        model = task.model_override or manifest.model
        self.llm = LLMClient(platform.llm_manager_url, platform.llm_manager_api_key, model)
        self.kb = KBClient(platform.kb_dsn, manifest.permissions, use_embeddings=True)
        self.tools = load_tools(manifest.tools)

        self.messages: list[dict[str, Any]] = []
        self.iteration = 0
        self.max_iterations = min(
            task.max_iterations_override or manifest.max_iterations,
            platform.global_max_iterations,
        )

    async def run(self) -> str:
        """Execute the agent loop. Returns the final result text."""
        await self.kb.connect()

        try:
            # Mark task as running
            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.running,
                started_at=datetime.now(timezone.utc),
            )

            result = await self._loop()

            # Mark task as completed
            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.completed,
                completed_at=datetime.now(timezone.utc),
                result={"summary": result[:1000]},
            )

            # Write result to agent results scope
            await self.kb.write(
                scope=f"/agents/{self.manifest.name}/results/{self.task.id}",
                content=result,
                metadata=self.task.config,
                source=f"{self.manifest.name}/{self.task.id}",
            )

            return result

        except Exception as e:
            log.exception("Agent loop failed")
            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.failed,
                completed_at=datetime.now(timezone.utc),
                result={"error": str(e)},
            )
            # Write failure notification
            await self.kb.write(
                scope=f"/notifications/alex/{self.task.id}",
                content=f"Task {self.task.id[:8]} ({self.manifest.name}) failed: {e}",
                needs_embedding=False,
                source=f"{self.manifest.name}/{self.task.id}",
            )
            raise
        finally:
            await self.kb.close()
            await self.llm.close()

    async def _loop(self) -> str:
        """The core LLM conversation loop."""

        # Resume from conversation history if restarting
        existing = await self.kb.get(f"/tasks/{self.task.id}/conversation")
        if existing:
            self.messages = json.loads(existing.content)
            self.iteration = count_tool_rounds(self.messages)
            log.info("Resumed conversation: %d iterations completed", self.iteration)

        # Build initial prompt (only if fresh start)
        if not self.messages:
            context = await self.kb.recall(
                self.task.instruction,
                scopes=self.manifest.permissions.read,
                limit=5,
            )

            self.messages.append({
                "role": "system",
                "content": build_system_prompt(self.manifest, self.tools.schemas()),
            })
            self.messages.append({
                "role": "user",
                "content": build_user_message(self.task.instruction, context),
            })

        while self.iteration < self.max_iterations:
            log.info("Iteration %d/%d", self.iteration + 1, self.max_iterations)

            # Call LLM
            response = await self.llm.chat(self.messages, tools=self.tools.schemas())

            # Add assistant message
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if response.content:
                assistant_msg["content"] = response.content
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in response.tool_calls
                ]
            self.messages.append(assistant_msg)

            # If LLM wants to use tools
            if response.tool_calls:
                for tc in response.tool_calls:
                    log.info("Tool call: %s(%s)", tc.name, tc.arguments[:100])
                    result = await self.tools.execute(tc.name, tc.arguments)

                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

                self.iteration += 1

                # Persist conversation for restart safety
                await self._persist_conversation()
                continue

            # LLM is done (no tool calls, just text response)
            log.info("Agent finished: %s", response.content[:200])
            return response.content

        # Hit iteration limit
        limit_msg = (
            f"Hit iteration limit ({self.max_iterations}). "
            f"Task: {self.task.instruction[:100]}"
        )
        log.warning(limit_msg)

        # Notify Alex
        await self.kb.write(
            scope=f"/notifications/alex/{self.task.id}",
            content=limit_msg,
            needs_embedding=False,
            source=f"{self.manifest.name}/{self.task.id}",
        )

        return limit_msg

    async def _persist_conversation(self) -> None:
        """Save conversation history to KB for restart safety."""
        try:
            await self.kb.write(
                scope=f"/tasks/{self.task.id}/conversation",
                content=json.dumps(self.messages),
                needs_embedding=False,
                source=f"{self.manifest.name}/{self.task.id}",
            )
        except Exception:
            log.warning("Failed to persist conversation", exc_info=True)
