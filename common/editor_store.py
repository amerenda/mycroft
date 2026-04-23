"""Persistent storage for agent and workflow definitions.

Definitions are stored in the DB so edits survive pod restarts.
The filesystem (agents/, workflows/) is used only as a seed source on startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)


async def ensure_editor_tables(pool: asyncpg.Pool) -> None:
    """Create agent_definitions and workflow_definitions tables if absent."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_definitions (
                name        TEXT PRIMARY KEY,
                manifest    TEXT NOT NULL DEFAULT '',
                prompts     TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                name        TEXT PRIMARY KEY,
                content     TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    log.info("Editor tables ready")


async def seed_from_filesystem(pool: asyncpg.Pool, agents_dir: Path, workflows_dir: Path) -> None:
    """Insert filesystem definitions that are not yet in the DB (never overwrites)."""
    async with pool.acquire() as conn:
        # Agents
        for d in sorted(agents_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            exists = await conn.fetchval(
                "SELECT 1 FROM agent_definitions WHERE name = $1", d.name
            )
            if not exists:
                manifest = (d / "manifest.yaml").read_text() if (d / "manifest.yaml").exists() else ""
                prompts = (d / "prompts.py").read_text() if (d / "prompts.py").exists() else ""
                await conn.execute(
                    "INSERT INTO agent_definitions (name, manifest, prompts) VALUES ($1, $2, $3)",
                    d.name, manifest, prompts,
                )
                log.info("Seeded agent '%s' from filesystem", d.name)

        # Workflows
        if workflows_dir.exists():
            for f in sorted(workflows_dir.glob("*.yaml")):
                exists = await conn.fetchval(
                    "SELECT 1 FROM workflow_definitions WHERE name = $1", f.stem
                )
                if not exists:
                    await conn.execute(
                        "INSERT INTO workflow_definitions (name, content) VALUES ($1, $2)",
                        f.stem, f.read_text(),
                    )
                    log.info("Seeded workflow '%s' from filesystem", f.stem)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def list_agents(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT name, manifest, prompts FROM agent_definitions ORDER BY name"
    )
    return [dict(r) for r in rows]


async def get_agent(pool: asyncpg.Pool, name: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT name, manifest, prompts FROM agent_definitions WHERE name = $1", name
    )
    return dict(row) if row else None


async def save_agent(pool: asyncpg.Pool, name: str, manifest: str, prompts: str) -> None:
    await pool.execute(
        """
        INSERT INTO agent_definitions (name, manifest, prompts, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (name) DO UPDATE SET manifest = $2, prompts = $3, updated_at = NOW()
        """,
        name, manifest, prompts,
    )


async def delete_agent(pool: asyncpg.Pool, name: str) -> bool:
    result = await pool.execute("DELETE FROM agent_definitions WHERE name = $1", name)
    return result.split()[-1] != "0"


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

async def list_workflows(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT name, content FROM workflow_definitions ORDER BY name"
    )
    return [dict(r) for r in rows]


async def get_workflow(pool: asyncpg.Pool, name: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT name, content FROM workflow_definitions WHERE name = $1", name
    )
    return dict(row) if row else None


async def save_workflow(pool: asyncpg.Pool, name: str, content: str) -> None:
    await pool.execute(
        """
        INSERT INTO workflow_definitions (name, content, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (name) DO UPDATE SET content = $2, updated_at = NOW()
        """,
        name, content,
    )


async def delete_workflow(pool: asyncpg.Pool, name: str) -> bool:
    result = await pool.execute("DELETE FROM workflow_definitions WHERE name = $1", name)
    return result.split()[-1] != "0"
