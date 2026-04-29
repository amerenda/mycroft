"""The thin agent loop — tool rounds until final text or iteration cap."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from common.config import PlatformConfig
from common.kb import KBClient
from common.llm import LLMClient
from common.metrics import (
    agent_iterations_total, agent_tool_calls_total,
    agent_tool_call_seconds, llm_metrics_callback,
)
from common.models import AgentManifest, TaskConfig, TaskStatus
from runtime.context import (
    DEFAULT_EMPTY_RESPONSE_NUDGE,
    count_tool_rounds,
    default_iteration_limit_message,
)
from runtime.task_prompts import (
    resolve_initial_user_message,
    resolve_permissions,
    resolve_system_prompt,
    resolve_tool_names,
)
from runtime.tools.base import load_tools

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
        self.llm.set_metrics_callback(llm_metrics_callback)

        perms = resolve_permissions(task, manifest)
        self.kb = KBClient(platform.kb_dsn, perms, use_embeddings=True)

        self.tools = load_tools(resolve_tool_names(task, manifest))

        self._max_tokens = task.config.get("max_tokens", 4096)
        self._temperature = task.config.get("temperature")

        self.messages: list[dict[str, Any]] = []
        self.iteration = 0
        self._consecutive_empty = 0
        self.max_iterations = min(
            task.max_iterations_override or manifest.max_iterations,
            platform.global_max_iterations,
        )

    def _empty_response_nudge(self) -> str:
        return str(
            self.task.config.get("empty_response_nudge", DEFAULT_EMPTY_RESPONSE_NUDGE)
        )

    def _iteration_limit_reply(self) -> str:
        custom = self.task.config.get("iteration_limit_reply")
        if custom is not None and str(custom).strip():
            return str(custom)
        return default_iteration_limit_message(
            self.max_iterations,
            self.task.instruction or "",
        )

    async def run(self) -> str:
        """Execute the agent loop. Returns the final result text."""
        await self.kb.connect()

        try:
            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.running,
                started_at=datetime.now(timezone.utc),
            )

            result = await self._loop()

            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.completed,
                completed_at=datetime.now(timezone.utc),
                result={"summary": result[:1000]},
            )

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
        existing = await self.kb.get(f"/tasks/{self.task.id}/conversation")
        if existing:
            self.messages = json.loads(existing.content)
            self.iteration = count_tool_rounds(self.messages)
            log.info("Resumed conversation: %d iterations completed", self.iteration)

        if not self.messages:
            system_prompt = resolve_system_prompt(
                self.task, self.manifest, self.tools.schemas())
            user_content = await resolve_initial_user_message(
                self.task, self.manifest, self.kb)
            self.messages.append({
                "role": "system",
                "content": system_prompt,
            })
            self.messages.append({
                "role": "user",
                "content": user_content,
            })

        tool_schemas = self.tools.schemas()
        tool_payload = tool_schemas if tool_schemas else None

        while self.iteration < self.max_iterations:
            log.info(
                "Iteration %d/%d model=%s",
                self.iteration + 1, self.max_iterations, self.llm.model,
            )
            agent_iterations_total.labels(agent_type=self.manifest.name).inc()

            response = await self.llm.chat(
                self.messages,
                tools=tool_payload,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            log.info(
                "LLM response: content=%r tool_calls=%d queue_wait=%.1fs inference=%.1fs tokens=%d+%d",
                (response.content or "")[:100], len(response.tool_calls),
                response.queue_wait_seconds, response.inference_seconds,
                response.prompt_tokens, response.completion_tokens,
            )

            if response.tool_calls:
                self._consecutive_empty = 0

                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if response.content:
                    assistant_msg["content"] = response.content
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

                for tc in response.tool_calls:
                    log.info("Tool call: %s(%s)", tc.name, tc.arguments[:100])
                    agent_tool_calls_total.labels(
                        agent_type=self.manifest.name, tool=tc.name).inc()
                    t_tool = time.monotonic()
                    result = await self.tools.execute(tc.name, tc.arguments)
                    agent_tool_call_seconds.labels(tool=tc.name).observe(
                        time.monotonic() - t_tool)

                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

                self.iteration += 1
                await self._persist_conversation()
                continue

            if not response.content or not response.content.strip():
                self._consecutive_empty += 1
                if self._consecutive_empty >= 3:
                    log.warning("Model returned %d consecutive empty responses, giving up",
                                self._consecutive_empty)
                    break

                log.warning("Empty response %d/3, nudging model to continue",
                            self._consecutive_empty)
                self.messages.append({
                    "role": "user",
                    "content": self._empty_response_nudge(),
                })
                continue

            self.messages.append({"role": "assistant", "content": response.content})
            log.info("Agent finished: %s", response.content[:200])
            return response.content

        limit_msg = self._iteration_limit_reply()
        log.warning(limit_msg)

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
