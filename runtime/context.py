"""Context building for the agent runtime."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from common.models import AgentManifest, MemoryRecord

log = logging.getLogger(__name__)


def _load_supplement(agent_name: str) -> str:
    """Load the SYSTEM_SUPPLEMENT from an agent's prompts module, if it exists."""
    try:
        mod = importlib.import_module(f"agents.{agent_name}.prompts")
        return getattr(mod, "SYSTEM_SUPPLEMENT", "")
    except (ModuleNotFoundError, ImportError):
        return ""


def build_system_prompt(manifest: AgentManifest, tool_schemas: list[dict[str, Any]]) -> str:
    """Build the system prompt from manifest and available tools."""
    tool_list = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tool_schemas
    )

    supplement = _load_supplement(manifest.name)
    if supplement:
        log.info("Loaded system supplement for agent '%s'", manifest.name)

    return f"""You are {manifest.role}.

Your goal: {manifest.goal}

## Tools

{tool_list}

### Using run_command

`run_command` is your primary tool for interacting with files and the filesystem. Use it for:
- **Reading files:** `cat path/to/file`
- **Listing directories:** `ls -la path/` or `find . -name '*.py'`
- **Searching code:** `grep -rn 'pattern' path/`
- **Editing files:** `sed -i 's/old/new/' file` or write with `cat > file << 'EOF'`
- **Running tests:** `pytest`, `npm test`, `make test`, etc.

## Rules

1. **Always use tools.** Never describe what you would do — do it by calling a tool.
2. **Read before writing.** Never guess what a file contains. Always `cat` it first.
3. **Explore before coding.** After cloning, run `ls` and `find` to understand the project structure. Read relevant files before making changes.
4. **One tool call at a time.** After each tool result, decide your next action.
5. **Keep going.** After each tool result, either call another tool OR give a final summary. Do not stop in the middle of a task.
6. **Work efficiently.** You have a limited number of iterations. Plan your approach, then execute.
7. **If something fails,** read the error, diagnose, and fix — do not give up after one failure.
""" + supplement


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
