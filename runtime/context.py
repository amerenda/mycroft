"""Context building for the agent runtime."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from common.models import AgentManifest, MemoryRecord

log = logging.getLogger(__name__)


def _load_supplement(agent_name: str, effort: str | None = None) -> str:
    """Load the system supplement from an agent's prompts module.

    If the module exports EFFORT_SUPPLEMENTS dict and effort is specified,
    uses the effort-specific supplement. Otherwise falls back to SYSTEM_SUPPLEMENT.
    """
    try:
        mod = importlib.import_module(f"agents.{agent_name}.prompts")
    except (ModuleNotFoundError, ImportError):
        return ""

    # Try effort-specific supplement first
    if effort:
        effort_map = getattr(mod, "EFFORT_SUPPLEMENTS", {})
        if effort in effort_map:
            return effort_map[effort]

    return getattr(mod, "SYSTEM_SUPPLEMENT", "")


def build_system_prompt(
    manifest: AgentManifest,
    tool_schemas: list[dict[str, Any]],
    effort: str | None = None,
) -> str:
    """Build the system prompt from manifest and available tools."""
    tool_list = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tool_schemas
    )

    supplement = _load_supplement(manifest.name, effort)
    if supplement:
        log.info("Loaded system supplement for agent '%s' (effort=%s)", manifest.name, effort or "default")

    base = f"""You are {manifest.role}.
Your goal: {manifest.goal}

# CRITICAL RULE

You MUST call a tool in every response. The ONLY time you respond without a tool call is when the entire task is finished and you are giving your final summary. A response without a tool call ends your session immediately.

WRONG — this ends your session:
"I'll start by looking into this."

RIGHT — this keeps you going:
Call one of your tools to take action.

# Available Tools

{tool_list}

# Rules

1. ALWAYS call a tool. Never describe what you would do — do it.
2. If a tool call fails, read the error and try a different approach. Do not give up.
3. Only respond without a tool call when the ENTIRE task is complete.
"""

    return base + supplement


def build_user_message(instruction: str, context_records: list[MemoryRecord]) -> str:
    """Build the initial user message with task instruction and KB context."""
    parts = [instruction]

    if context_records:
        context_block = "\n".join(
            f"- [{r.scope}] {r.content[:300]}" for r in context_records
        )
        parts.append(f"\nRelevant context from knowledge base:\n{context_block}")

    return "\n".join(parts)


# Defaults for loop edge cases — UI may override via task.config keys of the same name.
DEFAULT_EMPTY_RESPONSE_NUDGE = (
    "You returned an empty response. You must call a tool to continue. "
    "What is the next step? Call the appropriate tool now."
)


def default_iteration_limit_message(max_iterations: int, instruction_preview: str) -> str:
    return (
        f"Hit iteration limit ({max_iterations}). "
        f"Task: {instruction_preview[:100]}"
    )


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
