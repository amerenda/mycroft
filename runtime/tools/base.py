"""Tool protocol and registry for the agent runtime."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Tool(Protocol):
    """Interface that all agent tools must implement."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for the tool's parameters."""
        ...

    async def execute(self, args: dict[str, Any]) -> str:
        """Execute the tool and return a string result."""
        ...


class ToolRegistry:
    """Loads tools by name and dispatches calls."""

    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible tool schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, arguments: str) -> str:
        """Execute a tool by name with JSON argument string."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"

        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON arguments: {e}"

        try:
            result = await tool.execute(args)
            log.info("Tool %s executed successfully", name)
            return result
        except Exception as e:
            log.error("Tool %s failed: %s", name, e)
            return f"Error executing {name}: {e}"


def load_tools(tool_names: list[str], workspace: str = "/workspace") -> ToolRegistry:
    """Load tools by name and return a registry."""
    from runtime.tools.git import GitClone, GitCheckoutBranch, GitAdd, GitCommit, GitPush, GitDiff
    from runtime.tools.github import GhCreatePr, GhComment
    from runtime.tools.shell import RunCommand

    all_tools: dict[str, Tool] = {
        "git_clone": GitClone(workspace),
        "git_checkout_branch": GitCheckoutBranch(workspace),
        "git_add": GitAdd(workspace),
        "git_commit": GitCommit(workspace),
        "git_push": GitPush(workspace),
        "git_diff": GitDiff(workspace),
        "gh_create_pr": GhCreatePr(workspace),
        "gh_comment": GhComment(workspace),
        "run_command": RunCommand(workspace),
    }

    # Map manifest tool groups to individual tools
    tool_groups = {
        "git": ["git_clone", "git_checkout_branch", "git_add", "git_commit", "git_push", "git_diff"],
        "github": ["gh_create_pr", "gh_comment"],
        "shell": ["run_command"],
    }

    selected = set()
    for name in tool_names:
        if name in tool_groups:
            selected.update(tool_groups[name])
        elif name in all_tools:
            selected.add(name)

    tools = [all_tools[n] for n in selected if n in all_tools]
    log.info("Loaded %d tools: %s", len(tools), [t.name for t in tools])
    return ToolRegistry(tools)
