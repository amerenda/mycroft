"""The thin agent loop — ~250 lines, the entire agent framework."""

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
        self.llm.set_metrics_callback(llm_metrics_callback)
        self.kb = KBClient(platform.kb_dsn, manifest.permissions, use_embeddings=True)

        # Tools: task config can override the manifest tool list (for pipeline phases)
        tools_override = task.config.get("tools_override")
        scratch_scope = task.config.get("scratch_scope")
        self.tools = load_tools(
            tools_override or manifest.tools,
            kb_dsn=platform.kb_dsn if scratch_scope else None,
            scratch_scope=scratch_scope,
        )

        # LLM call params from task config (overridable via API/UI)
        self._max_tokens = task.config.get("max_tokens", 4096)
        self._temperature = task.config.get("temperature")
        self._effort = task.config.get("effort")  # light, regular, heavy

        # Report enforcement: researcher at regular/deep MUST write report.md
        # BUT: pipeline phases handle their own enforcement (gather has no write_file)
        phase = task.config.get("phase", "")
        is_pipeline_phase = phase in ("gather", "write") or phase.startswith("pipeline-")
        self._requires_report = (
            manifest.name == "researcher"
            and self._effort in ("regular", "deep", None)
            and not is_pipeline_phase
        )
        self._has_written_report = False

        # Writer model: a second model that synthesizes research into a report.
        # The research model (e.g. qwen3.5:9b) gathers info, then the writer
        # model (e.g. llama3.1:8b) writes the structured report.
        self._writer_model = task.config.get("writer_model") or getattr(manifest, "writer_model", "") or None
        self._switched_to_writer = False

        self.messages: list[dict[str, Any]] = []
        self.iteration = 0
        self._consecutive_empty = 0
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

            # Write result before marking completed — the coordinator polls for
            # completed status and immediately reads the result, so it must exist first.
            await self.kb.write(
                scope=f"/agents/{self.manifest.name}/results/{self.task.id}",
                content=result,
                metadata=self.task.config,
                source=f"{self.manifest.name}/{self.task.id}",
            )

            await self.kb.update_task(
                self.task.id,
                status=TaskStatus.completed,
                completed_at=datetime.now(timezone.utc),
                result={"summary": result[:1000]},
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

            system_prompt = self.task.system_prompt_override or build_system_prompt(
                self.manifest, self.tools.schemas(), effort=self._effort)
            system_suffix = self.task.config.get("system_suffix")
            if system_suffix:
                system_prompt = system_prompt.rstrip() + "\n\n" + system_suffix
            self.messages.append({
                "role": "system",
                "content": system_prompt,
            })

            # Inject pipeline context (original brief + previous step outputs).
            # Scopes are coordinator-written /runs/ paths; read without permission checks.
            original_brief: str | None = None
            prior_sections: list[str] = []
            for scope in self.task.context_injection:
                record = await self.kb.get_unchecked(scope)
                if record:
                    if scope.endswith("/original"):
                        original_brief = record.content
                    else:
                        step_label = scope.rstrip("/").rsplit("/", 1)[-1]
                        prior_sections.append(f"[CONTEXT: {step_label.upper()}]\n{record.content}")

            user_content = build_user_message(self.task.instruction, context)
            if self.task.context_injection:
                workflow_name = self.task.config.get("workflow", "")
                step_desc = self.task.config.get("step_description", "")

                header = "You are one step in a multi-step pipeline."
                if workflow_name:
                    header += f" Workflow: {workflow_name}."
                if step_desc:
                    header += f"\nYour role in this step: {step_desc}"

                parts = [header]
                if original_brief:
                    parts.append(
                        "The original user request — stay aligned with this throughout:\n"
                        f"{original_brief}"
                    )
                parts.extend(prior_sections)
                parts.append(user_content)
                user_content = "\n\n---\n\n".join(parts)

            self.messages.append({"role": "user", "content": user_content})

        while self.iteration < self.max_iterations:
            model_name = self.llm.model
            phase = "writer" if self._switched_to_writer else "research"
            log.info("Iteration %d/%d [%s] model=%s report_written=%s",
                     self.iteration + 1, self.max_iterations, phase, model_name, self._has_written_report)
            agent_iterations_total.labels(agent_type=self.manifest.name).inc()

            # Budget warning: tell the model when it's running low on iterations
            remaining = self.max_iterations - self.iteration
            if self._requires_report and not self._has_written_report and not self._switched_to_writer:
                if remaining == int(self.max_iterations * 0.4):
                    self.messages.append({
                        "role": "user",
                        "content": (
                            f"BUDGET WARNING: You have {remaining} iterations left. "
                            f"Start writing the report NOW using write_file. "
                            f"You can continue researching after the report is saved."
                        ),
                    })
                elif remaining == 2:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            f"FINAL WARNING: Only {remaining} iterations left. "
                            f"Write the report to /workspace/report.md IMMEDIATELY or your work will be lost."
                        ),
                    })

            # Call LLM
            response = await self.llm.chat(
                self.messages, tools=self.tools.schemas(),
                max_tokens=self._max_tokens, temperature=self._temperature,
            )

            # Log raw response for debugging
            log.info("LLM response: content=%r tool_calls=%d queue_wait=%.1fs inference=%.1fs tokens=%d+%d",
                     (response.content or "")[:100], len(response.tool_calls),
                     response.queue_wait_seconds, response.inference_seconds,
                     response.prompt_tokens, response.completion_tokens)

            # If LLM wants to use tools
            if response.tool_calls:
                self._consecutive_empty = 0  # reset empty counter

                # Add assistant message with tool calls
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

                    # Track report writes
                    if tc.name == "write_file" and "report" in tc.arguments.lower():
                        self._has_written_report = True
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

                # Persist conversation for restart safety
                await self._persist_conversation()
                continue

            # No tool calls — either the agent is done or it got confused
            if not response.content or not response.content.strip():
                self._consecutive_empty += 1
                if self._consecutive_empty >= 3:
                    # Model is stuck — stop wasting iterations
                    log.warning("Model returned %d consecutive empty responses, giving up",
                                self._consecutive_empty)
                    break

                # Nudge but do NOT increment iteration — empty responses are
                # not progress (often caused by thinking tokens consuming output)
                log.warning("Empty response %d/3, nudging model to continue",
                            self._consecutive_empty)
                self.messages.append({
                    "role": "user",
                    "content": "You returned an empty response. You must call a tool to continue. What is the next step? Call the appropriate tool now.",
                })
                continue

            # Non-empty text with no tool calls — agent wants to finish.
            # But some agents MUST complete certain actions before they can finish.
            if self._requires_report and not self._has_written_report:
                # Switch to writer model if available and not already switched
                if self._writer_model and not self._switched_to_writer:
                    log.info("Switching to writer model: %s", self._writer_model)
                    self._switched_to_writer = True
                    self.llm = LLMClient(
                        self.platform.llm_manager_url,
                        self.platform.llm_manager_api_key,
                        self._writer_model,
                    )
                    self.llm.set_metrics_callback(llm_metrics_callback)
                    # Tell the writer model to synthesize everything into a report
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "The research phase is complete. Now write the report.\n\n"
                            "Based on ALL the information gathered above, write a structured "
                            "research report to /workspace/report.md using write_file.\n\n"
                            "The report MUST include:\n"
                            "- A # heading with the research topic\n"
                            "- ## Summary (2-3 opinionated sentences)\n"
                            "- ## Findings (key facts with source URLs)\n"
                            "- ## Recommendation (what to do)\n"
                            "- ## Sources (URLs used)\n\n"
                            "Write the FULL report content to the file. Do it now."
                        ),
                    })
                    self.iteration += 1
                    continue
                else:
                    # Already switched or no writer model — bounce
                    log.warning("Agent tried to finish without writing report, forcing continuation")
                    self.messages.append({
                        "role": "user",
                        "content": "You MUST write the report to /workspace/report.md using write_file before you can finish. Do that now.",
                    })
                    self.iteration += 1
                    continue

            self.messages.append({"role": "assistant", "content": response.content})
            log.info("Agent finished: %s", response.content[:200])
            return response.content

        # Hit iteration limit — last chance: switch to writer model for report
        if self._requires_report and not self._has_written_report and self._writer_model and not self._switched_to_writer:
            log.info("Iteration limit reached, switching to writer model for final report")
            self._switched_to_writer = True
            self.llm = LLMClient(
                self.platform.llm_manager_url,
                self.platform.llm_manager_api_key,
                self._writer_model,
            )
            self.llm.set_metrics_callback(llm_metrics_callback)
            self.messages.append({
                "role": "user",
                "content": (
                    "The research phase is complete. Now write the report.\n\n"
                    "Based on ALL the information gathered above, write a structured "
                    "research report to /workspace/report.md using write_file. Include "
                    "a summary, findings with sources, and a recommendation. Do it now."
                ),
            })
            # Give the writer a few iterations
            for _ in range(5):
                response = await self.llm.chat(
                    self.messages, tools=self.tools.schemas(),
                    max_tokens=self._max_tokens, temperature=self._temperature,
                )
                if response.tool_calls:
                    for tc in response.tool_calls:
                        if tc.name == "write_file" and "report" in tc.arguments.lower():
                            self._has_written_report = True
                        result = await self.tools.execute(tc.name, tc.arguments)
                        self.messages.append({"role": "assistant", "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                        ]})
                        self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    if self._has_written_report:
                        await self._persist_conversation()
                        return response.content or "Report written."
                elif response.content:
                    return response.content

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
