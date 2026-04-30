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
        perms = resolve_permissions(task, manifest)
        self.kb = KBClient(platform.kb_dsn, perms, use_embeddings=True)

        # Tools: task.config["tools"] / tools_override or manifest list (pipeline phases)
        tool_names = resolve_tool_names(task, manifest)
        scratch_scope = task.config.get("scratch_scope")
        extra_groups = task.config.get("_tool_groups")  # DB-resolved groups from entrypoint
        is_last_step = bool(task.config.get("is_last_step", False))
        self.tools = load_tools(
            tool_names,
            kb_dsn=platform.kb_dsn if scratch_scope else None,
            scratch_scope=scratch_scope,
            extra_groups=extra_groups,
            is_last_step=is_last_step,
            web_read_max_chars=manifest.web_read_max_chars,
        )

        # LLM call params from task config (overridable via API/UI)
        self._max_tokens = task.config.get("max_tokens") or 4096
        self._temperature = task.config.get("temperature")
        self._effort = task.config.get("effort")

        self.messages: list[dict[str, Any]] = []
        self.iteration = 0
        self._consecutive_empty = 0
        self._consecutive_text_exit = 0
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
            system_prompt = resolve_system_prompt(
                self.task,
                self.manifest,
                self.tools.schemas(),
                max_iterations=self.max_iterations,
            )
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
                        prior_sections.append(f"[PIPELINE INPUT]\n{record.content}")

            user_content = await resolve_initial_user_message(
                self.task, self.manifest, self.kb,
            )
            if self.task.context_injection:
                workflow_name = self.task.config.get("workflow", "")

                header = "You are one step in a multi-step pipeline."
                if workflow_name:
                    header += f" Workflow: {workflow_name}."

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

        _warn_at = max(2, round(self.max_iterations * 0.25))
        _warning_template = self.task.config.get("iteration_warning_message") or ""

        while self.iteration < self.max_iterations:
            model_name = self.llm.model
            log.info("Iteration %d/%d model=%s",
                     self.iteration + 1, self.max_iterations, model_name)
            agent_iterations_total.labels(agent_type=self.manifest.name).inc()

            remaining = self.max_iterations - self.iteration
            if remaining == _warn_at and _warning_template:
                self.messages.append({
                    "role": "user",
                    "content": _warning_template.format(remaining=remaining),
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

                    agent_tool_calls_total.labels(
                        agent_type=self.manifest.name, tool=tc.name).inc()
                    t_tool = time.monotonic()
                    result = await self.tools.execute(tc.name, tc.arguments)
                    agent_tool_call_seconds.labels(tool=tc.name).observe(
                        time.monotonic() - t_tool)

                    if tc.name in ("finish", "submit_report"):
                        log.info("Step terminated via %s (%d chars)", tc.name, len(result))
                        return result

                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

                self.iteration += 1

                # Persist conversation for restart safety
                await self._persist_conversation()
                continue

            # No API tool calls — check if model wrote a tool call as JSON text
            if not response.tool_calls and response.content:
                parsed = self._parse_text_tool_call(response.content)
                if parsed:
                    name, arguments = parsed
                    log.info("Parsed text tool call: %s(%s)", name, arguments[:100])
                    import uuid as _uuid
                    fake_id = f"text-{_uuid.uuid4().hex[:8]}"
                    self.messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [{
                            "id": fake_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }],
                    })
                    agent_tool_calls_total.labels(
                        agent_type=self.manifest.name, tool=name).inc()
                    result = await self.tools.execute(name, arguments)
                    if name in ("finish", "submit_report"):
                        log.info("Text tool call terminated via %s (%d chars)", name, len(result))
                        return result
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": fake_id,
                        "content": result,
                    })
                    self.iteration += 1
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
                    "content": self.task.config.get(
                        "empty_response_nudge", DEFAULT_EMPTY_RESPONSE_NUDGE
                    ),
                })
                continue

            # If require_tool_exit is set and a terminal tool is available,
            # nudge the model to call it rather than accepting the text response.
            terminal_tools = [
                t["function"]["name"] for t in self.tools.schemas()
                if t["function"]["name"] in ("finish", "submit_report")
            ]
            if self.manifest.require_tool_exit and terminal_tools:
                self._consecutive_text_exit += 1
                self.messages.append({"role": "assistant", "content": response.content})
                if self._consecutive_text_exit >= 3:
                    log.warning("Model ignored tool-exit nudge %d times — force-finishing",
                                self._consecutive_text_exit)
                    return response.content
                exit_options = " or ".join(f"`{t}`" for t in terminal_tools)
                self.messages.append({
                    "role": "user",
                    "content": (
                        f"You must call {exit_options} to deliver your output. "
                        "A text-only response is not accepted. "
                        f"Call the tool now with your full output in the `content` argument."
                    ),
                })
                self.iteration += 1
                await self._persist_conversation()
                continue

            self._consecutive_text_exit = 0
            self.messages.append({"role": "assistant", "content": response.content})
            log.info("Agent finished: %s", response.content[:200])
            return response.content

        # Force-finish: prefer last assistant message with text; if the model only
        # emitted tool calls (empty content), assemble recent tool outputs so the
        # run does not return a blank result to KB/UI.
        last_content = self._force_finish_fallback_text()
        log.warning("Max iterations (%d) reached without finish call — force-finishing (%d chars)",
                    self.max_iterations, len(last_content))

        preview = (self.task.instruction or "").strip() or str(
            self.task.config.get("user_message") or ""
        )
        limit_note = self.task.config.get("iteration_limit_reply")
        if not limit_note:
            limit_note = default_iteration_limit_message(
                self.max_iterations, preview or "(no instruction)"
            )
        await self.kb.write(
            scope=f"/notifications/alex/{self.task.id}",
            content=(
                f"Agent {self.manifest.name} hit iteration limit ({self.max_iterations}) "
                f"without calling finish. Force-finished. {limit_note}"
            ),
            needs_embedding=False,
            source=f"{self.manifest.name}/{self.task.id}",
        )

        return last_content

    def _force_finish_fallback_text(self) -> str:
        """Best-effort final text when the loop hits max iterations without finish/submit_report."""
        last_assistant = next(
            (m["content"] for m in reversed(self.messages)
             if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()),
            "",
        )
        if last_assistant.strip():
            return last_assistant

        chunks: list[str] = []
        for m in reversed(self.messages):
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if not isinstance(c, str) or not c.strip():
                continue
            piece = c.strip()
            if len(piece) > 12_000:
                piece = piece[:12_000] + "\n...[truncated]"
            chunks.append(piece)
            if len(chunks) >= 8:
                break
        if not chunks:
            return ""
        chunks.reverse()
        header = (
            "[Iteration limit reached without calling the exit tool; "
            "below is text recovered from the most recent tool outputs.]\n\n"
        )
        body = "\n\n--- tool output ---\n\n".join(chunks)
        out = header + body
        max_out = 150_000
        if len(out) > max_out:
            out = out[:max_out] + "\n...[truncated]"
        log.warning("Force-finish fallback: assembled %d tool snippets (%d chars)", len(chunks), len(out))
        return out

    def _parse_text_tool_call(self, text: str) -> tuple[str, str] | None:
        """Detect tool calls written as JSON text instead of API tool calls.

        Some models output {"name": "finish", "parameters": {...}} in a code
        block rather than making a real API tool call. Parse and execute these.
        Returns (tool_name, arguments_json) or None.
        """
        import re as _re
        known = {t["function"]["name"] for t in self.tools.schemas()}

        def _try(raw: str) -> tuple[str, str] | None:
            try:
                obj = json.loads(raw.strip())
            except (json.JSONDecodeError, ValueError):
                return None
            if not isinstance(obj, dict):
                return None
            name = obj.get("name") or obj.get("function") or obj.get("tool")
            params = obj.get("parameters") or obj.get("arguments") or obj.get("input") or {}
            if isinstance(name, str) and name in known:
                return name, json.dumps(params)
            return None

        # Prefer finish/submit_report hits first, then any tool
        candidates: list[tuple[str, str]] = []
        for block in _re.findall(r"```(?:json)?\s*(.*?)\s*```", text, _re.DOTALL):
            r = _try(block)
            if r:
                candidates.append(r)
        if not candidates:
            r = _try(text)
            if r:
                candidates.append(r)

        for name, args in candidates:
            if name in ("finish", "submit_report"):
                return name, args
        return candidates[0] if candidates else None

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
