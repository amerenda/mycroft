"""Reports storage — lightweight reports table in agent-kb.

Reports are experimental. The table and all data can be dropped
with delete_all_reports() or DROP TABLE reports. Nothing else
depends on this table.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)


async def ensure_reports_table(pool: asyncpg.Pool) -> None:
    """Create the reports table if it doesn't exist, and add missing columns."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT DEFAULT '',
                tags TEXT[] DEFAULT '{}',
                source_task_id TEXT DEFAULT '',
                effort TEXT DEFAULT 'regular',
                workflow TEXT DEFAULT '',
                models_used JSONB DEFAULT '{}',
                commit_sha TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migrate existing tables missing the newer columns
        for col_name, col_def in [
            ("workflow", "TEXT DEFAULT ''"),
            ("models_used", "JSONB DEFAULT '{}'"),
            ("commit_sha", "TEXT DEFAULT ''"),
        ]:
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'reports' AND column_name = $1", col_name)
            if not exists:
                try:
                    await conn.execute(f"ALTER TABLE reports ADD COLUMN {col_name} {col_def}")
                    log.info("Added reports column: %s", col_name)
                except Exception as e:
                    log.warning("Could not add reports column %s: %s", col_name, e)


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug[:80].strip('-')


async def create_report(
    pool: asyncpg.Pool,
    title: str,
    content: str,
    summary: str = "",
    tags: list[str] | None = None,
    source_task_id: str = "",
    effort: str = "regular",
    workflow: str = "",
    models_used: dict | None = None,
    commit_sha: str = "",
) -> str:
    """Create a report. Returns the report ID (slug)."""
    report_id = _slugify(title)

    # Handle duplicates by appending a suffix
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM reports WHERE id = $1", report_id)
        if existing:
            import uuid
            report_id = f"{report_id}-{uuid.uuid4().hex[:6]}"

        await conn.execute(
            """INSERT INTO reports
               (id, title, content, summary, tags, source_task_id, effort, workflow, models_used, commit_sha)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            report_id, title, content, summary, tags or [], source_task_id, effort,
            workflow, json.dumps(models_used or {}), commit_sha,
        )

    log.info("Report created: %s", report_id)
    return report_id


async def get_report(pool: asyncpg.Pool, report_id: str) -> Optional[dict]:
    """Get a report by ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM reports WHERE id = $1", report_id)
    if not row:
        return None
    return _row_to_dict(row)


async def list_reports(pool: asyncpg.Pool, limit: int = 50) -> list[dict]:
    """List all reports, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, summary, tags, effort, workflow, models_used, commit_sha, "
            "source_task_id, created_at, updated_at "
            "FROM reports ORDER BY created_at DESC LIMIT $1", limit)
    return [_row_to_dict(r) for r in rows]


async def update_report(
    pool: asyncpg.Pool,
    report_id: str,
    content: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
) -> bool:
    """Update a report. Returns False if not found."""
    sets = ["updated_at = NOW()"]
    vals: list[Any] = []
    idx = 1

    if content is not None:
        idx += 1
        sets.append(f"content = ${idx}")
        vals.append(content)
    if summary is not None:
        idx += 1
        sets.append(f"summary = ${idx}")
        vals.append(summary)
    if tags is not None:
        idx += 1
        sets.append(f"tags = ${idx}")
        vals.append(tags)

    query = f"UPDATE reports SET {', '.join(sets)} WHERE id = $1"
    async with pool.acquire() as conn:
        result = await conn.execute(query, report_id, *vals)
    return result != "UPDATE 0"


async def delete_report(pool: asyncpg.Pool, report_id: str) -> bool:
    """Delete a single report."""
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM reports WHERE id = $1", report_id)
    return result != "DELETE 0"


async def delete_all_reports(pool: asyncpg.Pool) -> int:
    """Delete ALL reports. For cleanup during experimentation."""
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM reports")
    count = int(result.split()[-1])
    log.info("Deleted all reports (%d)", count)
    return count


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("created_at", "updated_at"):
        if k in d and d[k]:
            d[k] = str(d[k])
    for k in ("models_used",):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = {}
    return d
