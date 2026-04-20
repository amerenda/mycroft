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
"I'll start by cloning the repository and exploring the code."

RIGHT — this keeps you going:
Call git_clone with repo="owner/repo"

WRONG — this ends your session:
"The file contains a bug on line 5. I should fix it."

RIGHT — this keeps you going:
Call run_command with command="sed -i '5s/old/new/' file.py"

# Tools

{tool_list}

# How to use run_command

run_command is how you interact with files. Examples:
- Read a file: run_command command="cat src/main.py"
- List files: run_command command="ls -la && find . -name '*.py' | head -40"
- Search code: run_command command="grep -rn 'pattern' src/"
- Edit a file: run_command command="sed -i 's/old/new/g' file.py"
- Write a new file: run_command command="cat > file.py << 'PYEOF'\ncontents here\nPYEOF"
- Run tests: run_command command="pytest" or run_command command="npm test"
- Combine commands to save iterations: run_command command="ls -la && cat README.md && cat src/main.py"

# Rules

1. ALWAYS call a tool. Never describe what you would do. Do it.
2. Read files before editing them. Use run_command with cat.
3. Explore before coding. After cloning, list files and read relevant code.
4. You can combine multiple shell commands in one run_command call with && to save iterations.
5. If a command fails, read the error and fix it. Do not give up.
6. Only respond without a tool call when the ENTIRE task is complete.
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
