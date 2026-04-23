"""Persistent storage for agent and workflow definitions.

Definitions are stored in the DB so edits survive pod restarts.
The filesystem (agents/, workflows/) is used only as a seed source on startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg
import yaml

log = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """Normalize an agent/workflow name to a clean backend identifier.

    Lowercases, collapses any run of non-alphanumeric characters to a single
    hyphen, and strips leading/trailing hyphens.
    Examples: "Web Search Agent" → "web-search-agent"
              "web_search"       → "web-search"
              "My-Agent!!"       → "my-agent"
    """
    import re
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


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
                name          TEXT PRIMARY KEY,
                content       TEXT NOT NULL DEFAULT '',
                pipeline_json JSONB DEFAULT NULL,
                created_at    TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migrate existing table
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'workflow_definitions' AND column_name = 'pipeline_json'"
        )
        if not exists:
            try:
                await conn.execute("ALTER TABLE workflow_definitions ADD COLUMN pipeline_json JSONB DEFAULT NULL")
                log.info("Added workflow_definitions.pipeline_json column")
            except Exception as e:
                log.warning("Could not add pipeline_json column: %s", e)
    log.info("Editor tables ready")


async def seed_from_filesystem(pool: asyncpg.Pool, agents_dir: Path, workflows_dir: Path) -> None:
    """Insert filesystem definitions that are not yet in the DB (never overwrites)."""
    async with pool.acquire() as conn:
        # Agents — use manifest's name field as the DB key, not the directory name
        for d in sorted(agents_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            manifest_file = d / "manifest.yaml"
            if not manifest_file.exists():
                continue
            manifest_text = manifest_file.read_text()
            data = yaml.safe_load(manifest_text) or {}
            agent_name = data.get("name") or d.name
            exists = await conn.fetchval(
                "SELECT 1 FROM agent_definitions WHERE name = $1", agent_name
            )
            if not exists:
                prompts = (d / "prompts.py").read_text() if (d / "prompts.py").exists() else ""
                await conn.execute(
                    "INSERT INTO agent_definitions (name, manifest, prompts) VALUES ($1, $2, $3)",
                    agent_name, manifest_text, prompts,
                )
                log.info("Seeded agent '%s' from filesystem (dir: %s)", agent_name, d.name)

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


async def save_agent(pool: asyncpg.Pool, name: str, manifest: str, prompts: str) -> str:
    """Save agent definition. Returns the canonical (slugified) name."""
    canonical = slugify(name)
    await pool.execute(
        """
        INSERT INTO agent_definitions (name, manifest, prompts, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (name) DO UPDATE SET manifest = $2, prompts = $3, updated_at = NOW()
        """,
        canonical, manifest, prompts,
    )
    return canonical


async def delete_agent(pool: asyncpg.Pool, name: str) -> bool:
    result = await pool.execute("DELETE FROM agent_definitions WHERE name = $1", name)
    return result.split()[-1] != "0"


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

async def list_workflows(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT name, content, pipeline_json FROM workflow_definitions ORDER BY name"
    )
    return [_wf_row(r) for r in rows]


async def get_workflow(pool: asyncpg.Pool, name: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT name, content, pipeline_json FROM workflow_definitions WHERE name = $1", name
    )
    return _wf_row(row) if row else None


async def save_workflow(
    pool: asyncpg.Pool,
    name: str,
    content: str,
    pipeline_json: dict | None = None,
) -> None:
    import json
    await pool.execute(
        """
        INSERT INTO workflow_definitions (name, content, pipeline_json, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (name) DO UPDATE SET content = $2, pipeline_json = $3, updated_at = NOW()
        """,
        name, content, json.dumps(pipeline_json) if pipeline_json is not None else None,
    )


def _wf_row(row) -> dict:
    d = dict(row)
    if isinstance(d.get("pipeline_json"), str):
        import json
        try:
            d["pipeline_json"] = json.loads(d["pipeline_json"])
        except Exception:
            d["pipeline_json"] = None
    return d


async def delete_workflow(pool: asyncpg.Pool, name: str) -> bool:
    result = await pool.execute("DELETE FROM workflow_definitions WHERE name = $1", name)
    return result.split()[-1] != "0"
