"""KB scratch tools — shared notepad for all agents in a workflow run."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class ScratchRead:
    name = "scratch_read"
    description = (
        "Read the shared scratch space for this workflow run. "
        "Other agents in this pipeline can see and update it. "
        "Use it to check notes, URLs, or findings left by earlier steps."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, kb_dsn: str, scope: str):
        self._kb_dsn = kb_dsn
        self._scope = scope

    async def execute(self, args: dict[str, Any]) -> str:
        import asyncpg
        conn = await asyncpg.connect(self._kb_dsn)
        try:
            row = await conn.fetchrow(
                """
                SELECT content FROM memory_records
                WHERE scope = $1
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC LIMIT 1
                """,
                self._scope,
            )
            return row["content"] if row else "(scratch is empty)"
        finally:
            await conn.close()


class ScratchWrite:
    name = "scratch_write"
    description = (
        "Write to the shared scratch space for this workflow run. "
        "All agents in this pipeline can read it. "
        "Use it to leave key facts, URLs, or partial findings for later steps. "
        "Set append=true to add to existing notes; omit or set false to overwrite."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Content to write to scratch.",
            },
            "append": {
                "type": "boolean",
                "description": "If true, append to existing scratch content. If false (default), overwrite.",
            },
        },
        "required": ["content"],
    }

    def __init__(self, kb_dsn: str, scope: str):
        self._kb_dsn = kb_dsn
        self._scope = scope

    async def execute(self, args: dict[str, Any]) -> str:
        import uuid
        import asyncpg
        content = args.get("content", "")
        append = args.get("append", False)
        conn = await asyncpg.connect(self._kb_dsn)
        try:
            async with conn.transaction():
                if append:
                    row = await conn.fetchrow(
                        "SELECT content FROM memory_records WHERE scope = $1 "
                        "ORDER BY created_at DESC LIMIT 1",
                        self._scope,
                    )
                    existing = row["content"] if row else ""
                    content = (existing + "\n\n" + content).strip() if existing else content
                await conn.execute(
                    "DELETE FROM memory_records WHERE scope = $1", self._scope
                )
                await conn.execute(
                    """
                    INSERT INTO memory_records
                        (id, content, scope, categories, metadata, importance,
                         source, needs_embedding, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, false,
                            NOW() + INTERVAL '7 days')
                    """,
                    str(uuid.uuid4()), content, self._scope,
                    [], json.dumps({}), 0.5, "agent-scratch",
                )
            log.info("Scratch %s: scope=%s len=%d", "appended" if append else "updated",
                     self._scope, len(content))
            return f"Scratch {'appended' if append else 'updated'} ({len(content)} chars)."
        finally:
            await conn.close()
