"""Pipeline run fetch artifacts — persisted raw web_read bodies for downstream steps."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_MAX_STORE_CHARS = 2_000_000


def run_root_from_scratch_scope(scratch_scope: str | None) -> str | None:
    """`/runs/{uuid}/scratch` -> `/runs/{uuid}`."""
    if not scratch_scope or not isinstance(scratch_scope, str):
        return None
    s = scratch_scope.strip().rstrip("/")
    if s.endswith("/scratch"):
        return s[: -len("/scratch")]
    return None


def _artifact_scope(run_fetch_root: str, artifact_id: str) -> str:
    root = run_fetch_root.rstrip("/")
    return f"{root}/fetch/{artifact_id}"


async def persist_web_fetch(
    kb_dsn: str,
    run_fetch_root: str,
    url: str,
    body: str,
) -> str:
    """Store the full fetched document under /runs/.../fetch/{uuid}. Returns the scope path."""
    import asyncpg

    artifact_id = str(uuid.uuid4())
    scope = _artifact_scope(run_fetch_root, artifact_id)
    stored = body
    truncated_note = ""
    if len(stored) > _MAX_STORE_CHARS:
        stored = stored[:_MAX_STORE_CHARS]
        truncated_note = "\n\n[Body truncated at KB storage cap for this artifact]\n"

    header = (
        f"FETCH_URL: {url}\n"
        f"FETCH_SCOPE: {scope}\n"
        f"FETCH_STORED_AT: {datetime.now(timezone.utc).isoformat()}\n"
        f"FETCH_CHARS: {len(body)}\n"
        f"{truncated_note}\n---\n\n"
    )
    content = header + stored

    conn = await asyncpg.connect(kb_dsn)
    try:
        await conn.execute(
            """
            INSERT INTO memory_records
                (id, content, scope, categories, metadata, importance,
                 source, needs_embedding, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, false,
                    NOW() + INTERVAL '7 days')
            """,
            str(uuid.uuid4()),
            content,
            scope,
            [],
            json.dumps({"url": url, "source_tool": "web_read"}),
            0.5,
            "web-fetch-artifact",
        )
    finally:
        await conn.close()

    log.info("Persisted web_fetch: scope=%s url=%s stored_len=%d", scope, url[:80], len(content))
    return scope


class RunFetchList:
    name = "run_fetch_list"
    description = (
        "List raw web page captures stored for this pipeline run. "
        "Each line is one KB scope path (under /runs/.../fetch/) plus URL metadata. "
        "Use run_fetch_read on a scope to load the full stored text before citing facts."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, kb_dsn: str, run_fetch_root: str):
        self._kb_dsn = kb_dsn
        self._run_fetch_root = run_fetch_root.rstrip("/")

    async def execute(self, args: dict[str, Any]) -> str:
        import asyncpg

        prefix = self._run_fetch_root + "/fetch/"
        conn = await asyncpg.connect(self._kb_dsn)
        try:
            rows = await conn.fetch(
                """
                SELECT scope, metadata, created_at
                FROM memory_records
                WHERE scope LIKE $1 || '%'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at ASC
                """,
                prefix,
            )
        finally:
            await conn.close()

        if not rows:
            return (
                "No fetch artifacts for this run yet. "
                "Earlier steps with web_read store captures under "
                f"{prefix}{{uuid}} when running inside a pipeline."
            )

        lines: list[str] = []
        for r in rows:
            meta = r["metadata"] or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            url = (meta.get("url") or "") if isinstance(meta, dict) else ""
            ts = r["created_at"].isoformat() if r["created_at"] else ""
            lines.append(f"{r['scope']}\t{url}\t{ts}")

        return (
            f"Fetch artifacts ({len(rows)}):\n"
            "scope\turl\tcreated_at\n"
            + "\n".join(lines)
        )


class RunFetchRead:
    name = "run_fetch_read"
    description = (
        "Load the full text of one stored web_fetch artifact by exact KB scope path "
        "(returned by web_read as KB_FETCH_SCOPE or listed by run_fetch_list)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "Exact scope path, e.g. /runs/{run_id}/fetch/{uuid}",
            },
        },
        "required": ["scope"],
    }

    def __init__(self, kb_dsn: str, run_fetch_root: str):
        self._kb_dsn = kb_dsn
        self._prefix = run_fetch_root.rstrip("/") + "/fetch/"

    async def execute(self, args: dict[str, Any]) -> str:
        import asyncpg

        scope = str(args.get("scope", "")).strip()
        if not scope:
            return "Error: scope is required"
        if not scope.startswith(self._prefix):
            return (
                f"Error: scope must start with this run's fetch prefix ({self._prefix!r}). "
                f"Got: {scope!r}"
            )

        conn = await asyncpg.connect(self._kb_dsn)
        try:
            row = await conn.fetchrow(
                """
                SELECT content FROM memory_records
                WHERE scope = $1
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC
                LIMIT 1
                """,
                scope,
            )
        finally:
            await conn.close()

        if not row:
            return f"Error: no artifact at {scope}"
        return row["content"]
