"""Tool schema versioning — stores and retrieves versioned tool definitions from the KB.

Tool schemas are stored in the memory_records table with scope '/tools/schemas/{tool_name}'.
Metadata contains: version (int), schema_version (semver string), updated_by, changelog.
Content is the JSON-serialized OpenAI function-calling schema.

This allows:
- Updating tool schemas without redeploying agent images
- A/B testing schema changes (agents can pin a version)
- Tracking who changed what and when
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_SCOPE_PREFIX = "/tools/schemas/"


async def ensure_schema_table(pool: asyncpg.Pool) -> None:
    """Create the tool_schemas table if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_schemas (
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                schema_version TEXT NOT NULL DEFAULT '1.0.0',
                schema JSONB NOT NULL,
                changelog TEXT DEFAULT '',
                updated_by TEXT DEFAULT 'system',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (name, version)
            )
        """)
        # Index for latest version lookup
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tool_schemas_name
            ON tool_schemas (name, version DESC)
        """)


async def get_schema(pool: asyncpg.Pool, name: str, version: int | None = None) -> Optional[dict]:
    """Get a tool schema by name. Returns latest version if version not specified."""
    async with pool.acquire() as conn:
        if version is not None:
            row = await conn.fetchrow(
                "SELECT * FROM tool_schemas WHERE name = $1 AND version = $2",
                name, version,
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM tool_schemas WHERE name = $1 ORDER BY version DESC LIMIT 1",
                name,
            )
    if not row:
        return None
    schema = row["schema"]
    if isinstance(schema, str):
        schema = json.loads(schema)
    return {
        "name": row["name"],
        "version": row["version"],
        "schema_version": row["schema_version"],
        "schema": schema,
        "changelog": row["changelog"],
        "updated_by": row["updated_by"],
        "created_at": str(row["created_at"]),
    }


async def list_schemas(pool: asyncpg.Pool) -> list[dict]:
    """List all tool schemas (latest version of each)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (name) name, version, schema_version, changelog, updated_by, created_at
            FROM tool_schemas ORDER BY name, version DESC
        """)
    return [
        {
            "name": r["name"],
            "version": r["version"],
            "schema_version": r["schema_version"],
            "changelog": r["changelog"],
            "updated_by": r["updated_by"],
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def get_schema_history(pool: asyncpg.Pool, name: str) -> list[dict]:
    """Get all versions of a tool schema."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT version, schema_version, changelog, updated_by, created_at FROM tool_schemas WHERE name = $1 ORDER BY version DESC",
            name,
        )
    return [
        {
            "version": r["version"],
            "schema_version": r["schema_version"],
            "changelog": r["changelog"],
            "updated_by": r["updated_by"],
            "created_at": str(r["created_at"]),
        }
        for r in rows
    ]


async def upsert_schema(
    pool: asyncpg.Pool,
    name: str,
    schema: dict[str, Any],
    schema_version: str = "1.0.0",
    changelog: str = "",
    updated_by: str = "system",
) -> int:
    """Insert or update a tool schema. Auto-increments version. Returns new version number."""
    async with pool.acquire() as conn:
        # Get current max version
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(version), 0) as max_v FROM tool_schemas WHERE name = $1",
            name,
        )
        new_version = row["max_v"] + 1

        await conn.execute(
            """INSERT INTO tool_schemas (name, version, schema_version, schema, changelog, updated_by)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6)""",
            name, new_version, schema_version, json.dumps(schema), changelog, updated_by,
        )

    log.info("Tool schema %s updated to version %d (by %s)", name, new_version, updated_by)
    return new_version


async def delete_schema(pool: asyncpg.Pool, name: str) -> bool:
    """Delete all versions of a tool schema."""
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM tool_schemas WHERE name = $1", name)
    return result != "DELETE 0"


async def seed_default_schemas(pool: asyncpg.Pool) -> None:
    """Seed tool schemas from the current runtime tool definitions.
    Only seeds if the table is empty (won't overwrite manual edits).
    """
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM tool_schemas")
    if count > 0:
        log.info("Tool schemas already seeded (%d entries), skipping", count)
        return

    # Import all tools and seed their schemas
    from runtime.tools.base import load_tools
    registry = load_tools(["files", "git", "github", "shell"])

    for tool_def in registry.schemas():
        fn = tool_def["function"]
        await upsert_schema(
            pool,
            name=fn["name"],
            schema=tool_def,
            schema_version="1.0.0",
            changelog="Initial schema from Mycroft runtime",
            updated_by="seed",
        )

    log.info("Seeded %d tool schemas", len(registry.schemas()))
