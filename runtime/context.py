"""Context building for the agent runtime."""

from __future__ import annotations

import logging
from typing import Any

from common.models import AgentManifest, MemoryRecord

log = logging.getLogger(__name__)


def build_system_prompt(
    manifest: AgentManifest,
    tool_schemas: list[dict[str, Any]],
    effort: str | None = None,
    max_iterations: int | None = None,
    template: str | None = None,
) -> str:
    """Build the default system prompt from manifest and available tools."""
    # Prepend thinking control token for models that support it (e.g. qwen3).
    # Only injected when explicitly set in the manifest — absent = model default.
    thinking_prefix = ""
    if manifest.thinking is True:
        thinking_prefix = "/think\n\n"
    elif manifest.thinking is False:
        thinking_prefix = "/no_think\n\n"

    tool_list = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tool_schemas
    )
    budget = max_iterations or manifest.max_iterations

    terminal = [t["function"]["name"] for t in tool_schemas
                if t["function"]["name"] in ("finish", "submit_report")]
    exit_tool = " or ".join(terminal) if terminal else "finish"

    if not template or not template.strip():
        log.warning("base_system_prompt_template is empty — set it in the Settings UI")
        return thinking_prefix

    body = template.format(
        role=manifest.role,
        goal=manifest.goal,
        max_iterations=budget,
        tool_list=tool_list,
        exit_tool=exit_tool,
    )
    return thinking_prefix + body


def build_user_message(instruction: str, context_records: list[MemoryRecord]) -> str:
    """Build the initial user message with task instruction and KB context."""
    parts = [instruction]

    if context_records:
        context_block = "\n".join(
            f"- [{r.scope}] {r.content[:300]}" for r in context_records
        )
        parts.append(f"\nRelevant context from knowledge base:\n{context_block}")

    return "\n".join(parts)


def count_tool_rounds(messages: list[dict[str, Any]]) -> int:
    """Count the number of completed tool execution rounds in a conversation."""
    rounds = 0
    for msg in messages:
        if msg.get("role") == "tool":
            # Count unique tool_call_ids to avoid double-counting multi-tool rounds
            pass
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            rounds += 1
    return rounds
