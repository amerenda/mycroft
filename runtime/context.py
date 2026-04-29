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

    return thinking_prefix + f"""You are {manifest.role}.
Your goal: {manifest.goal}

─── RULES ──────────────────────────────────────────────────

You have {budget} tool-call rounds to complete your task.
Each round where you call one or more tools uses one round.

1. Call a tool in every response. Never describe what you will do — do it.
2. Use finish (or submit_report) to deliver your output. That is the ONLY
   valid exit — responding with text alone does nothing.
3. Pace yourself. Don't spend all rounds on research and leave no rounds to
   deliver output. If you're running low, wrap up with what you have.
4. If a tool call fails, read the error and try a different approach.

─── AVAILABLE TOOLS ────────────────────────────────────────

{tool_list}
"""


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
