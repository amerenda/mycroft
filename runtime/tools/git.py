"""Git tools for the agent runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

log = logging.getLogger(__name__)

_git_configured = False


def _configure_git_once() -> None:
    """Set up git identity and credential helper from env vars (once)."""
    global _git_configured
    if _git_configured:
        return
    _git_configured = True

    # Git identity
    subprocess.run(["git", "config", "--global", "user.name", "mycroft-agent"], check=False)
    subprocess.run(["git", "config", "--global", "user.email", "mycroft@amerenda.com"], check=False)

    # GitHub token auth via credential helper
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        subprocess.run(
            ["git", "config", "--global", "credential.helper",
             f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"],
            check=False,
        )
        log.info("Configured git credential helper with GITHUB_TOKEN")

    # Shallow clone push support
    subprocess.run(["git", "config", "--global", "push.autoSetupRemote", "true"], check=False)


async def _run_git(args: list[str], cwd: str) -> str:
    """Run a git command and return output."""
    _configure_git_once()
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        return f"Error (exit {proc.returncode}): {err}\n{output}"
    return output.strip()


def _find_repo_dir(workspace: str) -> str:
    """Find the cloned repo directory inside the workspace."""
    for entry in os.listdir(workspace):
        full = os.path.join(workspace, entry)
        if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):
            return full
    return workspace


class GitClone:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_clone"
    description = "Clone a git repository into the workspace."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in 'owner/name' format or full URL"},
                "branch": {"type": "string", "description": "Branch to checkout (optional)"},
            },
            "required": ["repo"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        repo = args["repo"]
        branch = args.get("branch")

        if "/" in repo and not repo.startswith("http"):
            repo = f"https://github.com/{repo}.git"

        cmd = ["clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.append(repo)

        result = await _run_git(cmd, self.workspace)
        return result or f"Cloned {repo} to {self.workspace}"


class GitCheckoutBranch:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_checkout_branch"
    description = "Create and switch to a new branch, or switch to an existing branch."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch name"},
                "create": {"type": "boolean", "description": "Create the branch if it doesn't exist (default true)"},
            },
            "required": ["branch"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        branch = args["branch"]
        create = args.get("create", True)
        repo_dir = _find_repo_dir(self.workspace)

        if create:
            result = await _run_git(["checkout", "-b", branch], repo_dir)
        else:
            result = await _run_git(["checkout", branch], repo_dir)
        return result or f"Switched to branch {branch}"


class GitAdd:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_add"
    description = "Stage files for commit."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage. Use ['.'] for all changes.",
                },
            },
            "required": ["paths"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        paths = args["paths"]
        repo_dir = _find_repo_dir(self.workspace)
        return await _run_git(["add", *paths], repo_dir)


class GitCommit:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_commit"
    description = "Create a git commit with a message."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["message"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        message = args["message"]
        repo_dir = _find_repo_dir(self.workspace)
        return await _run_git(["commit", "-m", message], repo_dir)


class GitPush:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_push"
    description = "Push the current branch to the remote."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "description": "Remote name (default: origin)"},
                "branch": {"type": "string", "description": "Branch to push (default: current branch)"},
            },
        }

    async def execute(self, args: dict[str, Any]) -> str:
        remote = args.get("remote", "origin")
        branch = args.get("branch", "")
        repo_dir = _find_repo_dir(self.workspace)

        if not branch:
            branch_result = await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
            branch = branch_result.strip()

        return await _run_git(["push", "-u", remote, branch], repo_dir)


class GitDiff:
    def __init__(self, workspace: str):
        self.workspace = workspace

    name = "git_diff"
    description = "Show the current diff (staged and unstaged changes)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Show only staged changes"},
            },
        }

    async def execute(self, args: dict[str, Any]) -> str:
        staged = args.get("staged", False)
        repo_dir = _find_repo_dir(self.workspace)

        cmd = ["diff"]
        if staged:
            cmd.append("--staged")

        result = await _run_git(cmd, repo_dir)
        return result or "(no changes)"
