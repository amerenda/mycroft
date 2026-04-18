"""File operation tools — read, write, patch, search.

Schemas adapted from Forge (antinomyhq/forge, Apache-2.0).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Max file size to read (10 MB)
MAX_READ_BYTES = 10 * 1024 * 1024
# Max output length returned to the model
MAX_OUTPUT_CHARS = 30000


def _resolve(workspace: str, path: str) -> str:
    """Resolve a path relative to workspace. Prevents directory traversal."""
    if os.path.isabs(path):
        return path
    resolved = os.path.normpath(os.path.join(workspace, path))
    if not resolved.startswith(workspace):
        raise ValueError(f"Path {path} escapes workspace")
    return resolved


class ReadFile:
    """Read a file from the workspace."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read a file from the filesystem. Returns the file content with line numbers. "
            "Use start_line/end_line to read a specific range for large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to workspace or absolute)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-based). Only needed for large files.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Line number to stop reading at (inclusive). Only needed for large files.",
                },
            },
            "required": ["path"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        path = _resolve(self.workspace, args["path"])
        start = args.get("start_line")
        end = args.get("end_line")

        if not os.path.exists(path):
            return f"Error: file not found: {path}"

        try:
            size = os.path.getsize(path)
            if size > MAX_READ_BYTES:
                return f"Error: file too large ({size} bytes, max {MAX_READ_BYTES})"

            with open(path, "r", errors="replace") as f:
                lines = f.readlines()

            if start or end:
                s = max((start or 1) - 1, 0)
                e = min(end or len(lines), len(lines))
                lines = lines[s:e]
                offset = s
            else:
                offset = 0

            numbered = [f"{i + offset + 1}\t{line}" for i, line in enumerate(lines)]
            output = "".join(numbered)

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(lines)} total lines)"

            return output or "(empty file)"
        except Exception as e:
            return f"Error reading {path}: {e}"


class WriteFile:
    """Write content to a file."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file if it doesn't exist. "
            "Overwrites existing content. Creates parent directories if needed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to workspace or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        path = _resolve(self.workspace, args["path"])
        content = args["content"]

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            lines = content.count("\n") + 1
            return f"Wrote {len(content)} bytes ({lines} lines) to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"


class PatchFile:
    """Apply exact string replacements to a file."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "patch_file"

    @property
    def description(self) -> str:
        return (
            "Performs exact string replacements in a file. The old_string must match "
            "exactly (including whitespace and indentation). Use read_file first to see "
            "the current content before patching."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with (must be different from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false, replaces first only)",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        path = _resolve(self.workspace, args["path"])
        old = args["old_string"]
        new = args["new_string"]
        replace_all = args.get("replace_all", False)

        if not os.path.exists(path):
            return f"Error: file not found: {path}"

        if old == new:
            return "Error: old_string and new_string are identical"

        try:
            with open(path, "r", errors="replace") as f:
                content = f.read()

            if old not in content:
                return f"Error: old_string not found in {path}. Use read_file to check the current content."

            count = content.count(old)
            if count > 1 and not replace_all:
                return (
                    f"Error: old_string found {count} times in {path}. "
                    f"Provide more context to make it unique, or set replace_all=true."
                )

            if replace_all:
                new_content = content.replace(old, new)
            else:
                new_content = content.replace(old, new, 1)

            with open(path, "w") as f:
                f.write(new_content)

            replacements = count if replace_all else 1
            return f"Patched {path}: {replacements} replacement(s) made"
        except Exception as e:
            return f"Error patching {path}: {e}"


class SearchFiles:
    """Search file contents using grep/ripgrep patterns."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return (
            "Search for a pattern in files using regular expressions. "
            "Returns matching file paths by default, or matching lines with content mode."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default: workspace root)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')",
                },
                "include_content": {
                    "type": "boolean",
                    "description": "Show matching lines with context (default: false, shows file paths only)",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        pattern = args["pattern"]
        search_path = _resolve(self.workspace, args.get("path", "."))
        glob_pattern = args.get("glob")
        include_content = args.get("include_content", False)

        cmd = ["grep", "-r", "--include=" + glob_pattern if glob_pattern else None,
               "-n" if include_content else "-l",
               "-E", pattern, search_path]
        cmd = [c for c in cmd if c is not None]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode(errors="replace").strip()

            if not output:
                return f"No matches found for pattern: {pattern}"

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"

            return output
        except asyncio.TimeoutError:
            return "Error: search timed out after 30 seconds"
        except Exception as e:
            return f"Error searching: {e}"


class ListFiles:
    """List files and directories."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return "List files and directories at a given path. Useful for understanding project structure."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list (default: workspace root)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively (default: false)",
                },
            },
            "required": [],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        path = _resolve(self.workspace, args.get("path", "."))
        recursive = args.get("recursive", False)

        if not os.path.exists(path):
            return f"Error: path not found: {path}"

        try:
            entries = []
            if recursive:
                for root, dirs, files in os.walk(path):
                    # Skip hidden dirs
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    rel = os.path.relpath(root, path)
                    for f in sorted(files):
                        if not f.startswith("."):
                            entries.append(os.path.join(rel, f) if rel != "." else f)
                    if len(entries) > 500:
                        entries.append("... (truncated at 500 files)")
                        break
            else:
                for entry in sorted(os.listdir(path)):
                    if entry.startswith("."):
                        continue
                    full = os.path.join(path, entry)
                    suffix = "/" if os.path.isdir(full) else ""
                    entries.append(entry + suffix)

            return "\n".join(entries) or "(empty directory)"
        except Exception as e:
            return f"Error listing {path}: {e}"
