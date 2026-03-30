"""Context building for the agent runtime."""

from __future__ import annotations

from typing import Any

from common.models import AgentManifest, MemoryRecord


def build_system_prompt(manifest: AgentManifest, tool_schemas: list[dict[str, Any]]) -> str:
    """Build the system prompt from manifest and available tools."""
    tool_list = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tool_schemas
    )

    return f"""You are {manifest.role}.

Your goal: {manifest.goal}

You have access to these tools:
{tool_list}

IMPORTANT: You MUST use tools to complete the task. Do NOT describe what you would do — actually do it by calling tools. After each tool result, decide if the task is complete. If not, call the next tool. Keep going until the ENTIRE task is finished.

Rules:
- Always call tools to take action. Never respond with just text when a tool call is needed.
- Call tools one at a time when they depend on each other.
- After receiving a tool result, either call another tool to continue OR provide a final summary if the task is fully complete.
- Push to a draft git branch early and often — git is the durable store.
- When the task is fully done, provide a clear summary of what you did and any PR links.
- If you cannot complete the task, explain what went wrong.
- Do not exceed your iteration limit — work efficiently.
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
