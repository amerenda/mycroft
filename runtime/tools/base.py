"""Tool protocol and registry for the agent runtime."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


class SubmitReport:
    """Pipeline tool: submit final output and end the agent loop immediately."""

    @property
    def name(self) -> str:
        return "submit_report"

    @property
    def description(self) -> str:
        return (
            "Submit your final output and end your turn. Call this exactly once with the complete "
            "content. The pipeline will stop immediately after this — do not call any other tool."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The complete output (Markdown, plain text, etc.)",
                }
            },
            "required": ["content"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        return "Submitted."


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


_BUILTIN_GROUPS = {
    "files": ["read_file", "write_file", "patch_file", "search_files", "list_files"],
    "web": ["web_read", "web_search", "wiki_read"],
    "git": ["git_clone", "git_checkout_branch", "git_add", "git_commit", "git_push", "git_diff"],
    "github": ["gh_create_pr", "gh_comment"],
    "shell": ["run_command"],
    "todo": ["todo_list_projects", "todo_get_tasks", "todo_create_task", "todo_update_task"],
}


def load_tools(
    tool_names: list[str],
    workspace: str = "/workspace",
    kb_dsn: str | None = None,
    scratch_scope: str | None = None,
    extra_groups: dict[str, list[str]] | None = None,
    is_last_step: bool = False,
) -> ToolRegistry:
    """Load tools by name and return a registry.

    tool_names may include bare group names ("web"), @-prefixed group names ("@web"),
    or individual tool names ("web_search"). extra_groups overrides/extends the
    built-in group map with DB-defined groups.

    kb_dsn + scratch_scope: when both are provided, pipeline tools are auto-injected.
    Last step (is_last_step=True) gets only submit_report — scratch tools are for
    intermediate steps that need to pass notes forward.
    Non-last steps get scratch_read, scratch_write, and submit_report.
    """
    groups = {**_BUILTIN_GROUPS, **(extra_groups or {})}

    selected: set[str] = set()
    for name in tool_names:
        key = name.lstrip("@")   # strip @ prefix for group lookup
        if key in groups:
            selected.update(groups[key])
        else:
            selected.add(name)   # individual tool name, use as-is

    all_tools: dict[str, Tool] = {}

    if selected & {"read_file", "write_file", "patch_file", "search_files", "list_files"}:
        from runtime.tools.files import ReadFile, WriteFile, PatchFile, SearchFiles, ListFiles
        all_tools.update({
            "read_file": ReadFile(workspace),
            "write_file": WriteFile(workspace),
            "patch_file": PatchFile(workspace),
            "search_files": SearchFiles(workspace),
            "list_files": ListFiles(workspace),
        })

    if selected & {"git_clone", "git_checkout_branch", "git_add", "git_commit", "git_push", "git_diff"}:
        from runtime.tools.git import GitClone, GitCheckoutBranch, GitAdd, GitCommit, GitPush, GitDiff
        all_tools.update({
            "git_clone": GitClone(workspace),
            "git_checkout_branch": GitCheckoutBranch(workspace),
            "git_add": GitAdd(workspace),
            "git_commit": GitCommit(workspace),
            "git_push": GitPush(workspace),
            "git_diff": GitDiff(workspace),
        })

    if selected & {"gh_create_pr", "gh_comment"}:
        from runtime.tools.github import GhCreatePr, GhComment
        all_tools.update({
            "gh_create_pr": GhCreatePr(workspace),
            "gh_comment": GhComment(workspace),
        })

    if "run_command" in selected:
        from runtime.tools.shell import RunCommand
        all_tools["run_command"] = RunCommand(workspace)

    if selected & {"web_read", "web_search", "wiki_read"}:
        from runtime.tools.web import WebRead, WebSearch, WikiRead
        all_tools.update({
            "web_read": WebRead(),
            "web_search": WebSearch(),
            "wiki_read": WikiRead(),
        })

    if selected & {"todo_list_projects", "todo_get_tasks", "todo_create_task", "todo_update_task"}:
        from runtime.tools.vikunja import TodoListProjects, TodoGetTasks, TodoCreateTask, TodoUpdateTask
        all_tools.update({
            "todo_list_projects": TodoListProjects(),
            "todo_get_tasks": TodoGetTasks(),
            "todo_create_task": TodoCreateTask(),
            "todo_update_task": TodoUpdateTask(),
        })

    tools = [all_tools[n] for n in selected if n in all_tools]

    if kb_dsn and scratch_scope:
        from runtime.tools.kb import ScratchRead, ScratchWrite
        if is_last_step:
            tools += [SubmitReport()]
        else:
            tools += [ScratchRead(kb_dsn, scratch_scope), ScratchWrite(kb_dsn, scratch_scope), SubmitReport()]

    log.info("Loaded %d tools: %s", len(tools), [t.name for t in tools])
    return ToolRegistry(tools)
