"""Resolve system/user prompts and tool lists: DB/UI (task.config) overrides code defaults."""

from __future__ import annotations

from typing import Any

from common.kb import KBClient
from common.models import AgentManifest, AgentPermissions, MemoryRecord, TaskConfig
from runtime.context import build_system_prompt, build_user_message


def _strip_nonempty(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def resolve_system_prompt(
    task: TaskConfig,
    manifest: AgentManifest,
    tool_schemas: list[dict[str, Any]],
) -> str:
    """Prefer task.config / TaskConfig overrides; else build_system_prompt from manifest."""
    for key in ("system_prompt", "system_prompt_override"):
        got = _strip_nonempty(task.config.get(key))
        if got:
            return got
    got = _strip_nonempty(task.system_prompt_override)
    if got:
        return got
    effort = task.config.get("effort")
    return build_system_prompt(manifest, tool_schemas, effort=effort)


def resolve_tool_names(task: TaskConfig, manifest: AgentManifest) -> list[str]:
    if task.config.get("tools") is not None:
        return list(task.config["tools"])
    if task.config.get("tools_override") is not None:
        return list(task.config["tools_override"])
    return list(manifest.tools)


def resolve_permissions(task: TaskConfig, manifest: AgentManifest) -> AgentPermissions:
    raw = task.config.get("permissions")
    if isinstance(raw, dict) and (raw.get("read") is not None or raw.get("write") is not None):
        return AgentPermissions(
            read=list(raw.get("read") or []),
            write=list(raw.get("write") or []),
        )
    return manifest.permissions


def resolve_initial_user_message_sync(
    task: TaskConfig,
    context_records: list[MemoryRecord],
) -> str:
    """Build first user message when KB recall results are already known (e.g. API preview)."""
    got = _strip_nonempty(task.config.get("user_message"))
    if got:
        return got
    instruction = task.instruction or ""
    if task.config.get("kb_recall", True) is False:
        return instruction
    return build_user_message(instruction, context_records)


async def resolve_initial_user_message(
    task: TaskConfig,
    manifest: AgentManifest,
    kb: KBClient,
) -> str:
    """Build first user message; optional KB recall when using defaults."""
    got = _strip_nonempty(task.config.get("user_message"))
    if got:
        return got
    instruction = task.instruction or ""
    if task.config.get("kb_recall", True) is False:
        return instruction
    scopes = task.config.get("kb_recall_scopes") or manifest.permissions.read
    limit = int(task.config.get("kb_recall_limit", 5))
    context = await kb.recall(instruction, scopes=scopes, limit=limit)
    return build_user_message(instruction, context)
