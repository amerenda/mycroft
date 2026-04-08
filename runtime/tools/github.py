"""GitHub CLI tools for the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from runtime.tools.github_auth import get_installation_token

log = logging.getLogger(__name__)


def _find_repo_dir(workspace: str) -> str:
    for entry in os.listdir(workspace):
        full = os.path.join(workspace, entry)
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):
            return full
    return workspace


async def _run_gh(args: list[str], cwd: str) -> str:
    # gh CLI uses GH_TOKEN env var for auth
    env = os.environ.copy()
    token = get_installation_token()
    if token:
        env["GH_TOKEN"] = token

    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        return f"Error (exit {proc.returncode}): {err}\n{output}"
    return output.strip()


class GhCreatePr:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "gh_create_pr"
    description = "Create a GitHub pull request for the current branch."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description"},
                "base": {"type": "string", "description": "Base branch (default: main)"},
                "draft": {"type": "boolean", "description": "Create as draft PR (default: false)"},
            },
            "required": ["title", "body"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        title = args["title"]
        body = args["body"]
        base = args.get("base", "main")
        draft = args.get("draft", False)
        repo_dir = _find_repo_dir(self.workspace)

        cmd = ["pr", "create", "--title", title, "--body", body, "--base", base]
        if draft:
            cmd.append("--draft")

        return await _run_gh(cmd, repo_dir)


class GhComment:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "gh_comment"
    description = "Add a comment to a GitHub pull request or issue."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "PR or issue number"},
                "body": {"type": "string", "description": "Comment body"},
            },
            "required": ["pr_number", "body"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        pr_number = args["pr_number"]
        body = args["body"]
        repo_dir = _find_repo_dir(self.workspace)

        return await _run_gh(
            ["pr", "comment", str(pr_number), "--body", body],
            repo_dir,
        )
