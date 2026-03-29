"""Sandboxed shell command execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class RunCommand:
    """Run a shell command in the workspace."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def description(self) -> str:
        return "Run a shell command in the workspace directory. Use for running tests, linters, or other CLI tools."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (relative to workspace). Defaults to workspace root.",
                },
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        command = args["command"]
        cwd = args.get("cwd", self.workspace)
        if not cwd.startswith("/"):
            cwd = f"{self.workspace}/{cwd}"

        log.info("Shell: %s (cwd=%s)", command, cwd)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            return "Error: command timed out after 120 seconds"

        output = ""
        if stdout:
            output += stdout.decode(errors="replace")
        if stderr:
            output += "\nSTDERR:\n" + stderr.decode(errors="replace")

        if proc.returncode != 0:
            output = f"Exit code {proc.returncode}\n{output}"

        # Truncate very long output
        if len(output) > 10000:
            output = output[:5000] + "\n... (truncated) ...\n" + output[-3000:]

        return output.strip() or "(no output)"
