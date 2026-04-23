"""Knowledge Base client — PostgreSQL + pgvector with scoped access control."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import asyncpg

from common.models import AgentPermissions, MemoryRecord, TaskRecord, TaskStatus

log = logging.getLogger(__name__)


def _str(val) -> str:
    """Convert UUID or other types to string."""
    return str(val) if val is not None else ""


def _json(val) -> dict:
    """Parse JSONB — asyncpg may return str or dict."""
    if val is None:
        return {}
    if isinstance(val, str):
        return json.loads(val)
    return dict(val)

# ---------------------------------------------------------------------------
# Embeddings — sentence-transformers, loaded lazily on first use
# ---------------------------------------------------------------------------

_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Loaded embedding model: all-MiniLM-L6-v2 (384-dim)")
    return _embed_model


def embed(text: str) -> list[float]:
    model = _get_embed_model()
    return model.encode(text).tolist()


# ---------------------------------------------------------------------------
# KB Client
# ---------------------------------------------------------------------------


class KBClient:
    """Async KB client with scope-based access control and vector search."""

    def __init__(
        self,
        dsn: str,
        permissions: AgentPermissions | None = None,
        use_embeddings: bool = True,
    ):
        self.dsn = dsn
        self.permissions = permissions  # None = coordinator (full access)
        self.use_embeddings = use_embeddings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        log.info("KB connected to %s", self.dsn.split("@")[-1])

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        assert self._pool is not None, "KBClient not connected — call connect() first"
        return self._pool

    # -----------------------------------------------------------------------
    # Scope enforcement
    # -----------------------------------------------------------------------

    def _check_permission(self, scope: str, mode: str) -> None:
        """Raise if the agent doesn't have permission for this scope."""
        if self.permissions is None:
            return  # coordinator has full access
        allowed = self.permissions.read if mode == "read" else self.permissions.write
        for prefix in allowed:
            if scope.startswith(prefix):
                return
        raise PermissionError(
            f"Agent lacks {mode} permission for scope '{scope}'. "
            f"Allowed: {allowed}"
        )

    # -----------------------------------------------------------------------
    # Memory records
    # -----------------------------------------------------------------------

    async def write(
        self,
        scope: str,
        content: str,
        *,
        categories: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        importance: float = 0.5,
        source: str | None = None,
        needs_embedding: bool = True,
    ) -> str:
        """Write a record to the KB. Returns the record ID."""
        self._check_permission(scope, "write")

        record_id = str(uuid.uuid4())
        embedding = None
        if needs_embedding and self.use_embeddings:
            embedding = embed(content)

        await self.pool.execute(
            """
            INSERT INTO memory_records
                (id, content, embedding, scope, categories, metadata, importance, source, needs_embedding)
            VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9)
            """,
            record_id,
            content,
            str(embedding) if embedding else None,
            scope,
            categories or [],
            json.dumps(metadata or {}),
            importance,
            source,
            needs_embedding,
        )

        # Notify listeners
        event = json.dumps({"scope": scope, "record_id": record_id, "source": source})
        await self.pool.execute("SELECT pg_notify('agent_events', $1)", event)

        log.debug("KB write: scope=%s id=%s embed=%s", scope, record_id[:8], embedding is not None)
        return record_id

    async def get(self, scope: str) -> MemoryRecord | None:
        """Get a single record by exact scope match (most recent)."""
        self._check_permission(scope, "read")

        row = await self.pool.fetchrow(
            """
            SELECT id, content, scope, categories, metadata, importance, source,
                   needs_embedding, created_at
            FROM memory_records
            WHERE scope = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            scope,
        )
        if not row:
            return None

        # Update last_accessed
        await self.pool.execute(
            "UPDATE memory_records SET last_accessed = now() WHERE id = $1",
            row["id"],
        )

        return MemoryRecord(
            id=_str(row["id"]),
            content=row["content"],
            scope=row["scope"],
            categories=row["categories"],
            metadata=_json(row["metadata"]),
            importance=row["importance"],
            source=row["source"],
            needs_embedding=row["needs_embedding"],
            created_at=row["created_at"],
        )

    async def recall(
        self,
        query: str,
        scopes: list[str],
        *,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        """Semantic search across permitted scopes.

        Scoring: 0.5 * similarity + 0.3 * recency + 0.2 * importance
        """
        # Enforce read permissions on all requested scopes
        for scope in scopes:
            self._check_permission(scope, "read")

        if not self.use_embeddings:
            return []

        query_embedding = embed(query)

        # Build scope filter (prefix matching)
        scope_conditions = " OR ".join(
            f"scope LIKE '{s}%'" for s in scopes
        )

        rows = await self.pool.fetch(
            f"""
            WITH candidates AS (
                SELECT
                    id, content, scope, categories, metadata, importance, source,
                    needs_embedding, created_at,
                    1 - (embedding <=> $1::vector) AS similarity,
                    -- Recency: 1.0 for now, decays to 0.0 over 30 days
                    GREATEST(0, 1 - EXTRACT(EPOCH FROM (now() - created_at)) / (30 * 86400)) AS recency
                FROM memory_records
                WHERE embedding IS NOT NULL
                  AND ({scope_conditions})
            )
            SELECT *,
                   (0.5 * similarity + 0.3 * recency + 0.2 * importance) AS score
            FROM candidates
            ORDER BY score DESC
            LIMIT $2
            """,
            str(query_embedding),
            limit,
        )

        results = []
        for row in rows:
            results.append(MemoryRecord(
                id=_str(row["id"]),
                content=row["content"],
                scope=row["scope"],
                categories=row["categories"],
                metadata=_json(row["metadata"]),
                importance=row["importance"],
                source=row["source"],
                needs_embedding=row["needs_embedding"],
                created_at=row["created_at"],
            ))

        log.debug("KB recall: query='%s...' scopes=%s results=%d", query[:40], scopes, len(results))
        return results

    # -----------------------------------------------------------------------
    # Task records (coordinator operations)
    # -----------------------------------------------------------------------

    async def ensure_tasks_table(self) -> None:
        """Create agent_tasks if absent and add any missing columns (safe to call repeatedly)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id UUID PRIMARY KEY,
                    agent_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    trigger_ref TEXT DEFAULT '',
                    config JSONB DEFAULT '{}',
                    result JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
            """)
            col_exists = await conn.fetchval("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agent_tasks' AND column_name = 'argo_workflow_name'
            """)
            if not col_exists:
                try:
                    await conn.execute(
                        "ALTER TABLE agent_tasks ADD COLUMN argo_workflow_name TEXT"
                    )
                except Exception as e:
                    log.warning("Could not add argo_workflow_name column: %s", e)
        log.info("agent_tasks table ready")

    async def create_task(
        self,
        agent_type: str,
        trigger: str = "manual",
        trigger_ref: str = "",
        config: dict[str, Any] | None = None,
    ) -> str:
        """Create a new task. Returns task ID."""
        task_id = str(uuid.uuid4())
        await self.pool.execute(
            """
            INSERT INTO agent_tasks (id, agent_type, status, trigger, trigger_ref, config)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            task_id,
            agent_type,
            TaskStatus.pending.value,
            trigger,
            trigger_ref,
            json.dumps(config or {}),
        )
        log.info("Task created: id=%s type=%s trigger=%s", task_id[:8], agent_type, trigger)
        return task_id

    async def update_task(self, task_id: str, **fields: Any) -> None:
        """Update task fields. Supports: status, result, started_at, completed_at, argo_workflow_name."""
        set_parts = []
        values = [task_id]
        idx = 2

        for key, value in fields.items():
            if key == "status":
                set_parts.append(f"status = ${idx}")
                values.append(value.value if isinstance(value, TaskStatus) else value)
            elif key == "result":
                set_parts.append(f"result = ${idx}")
                values.append(json.dumps(value))
            elif key in ("started_at", "completed_at", "argo_workflow_name"):
                set_parts.append(f"{key} = ${idx}")
                values.append(value)
            else:
                continue
            idx += 1

        if not set_parts:
            return

        await self.pool.execute(
            f"UPDATE agent_tasks SET {', '.join(set_parts)} WHERE id = $1",
            *values,
        )

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM agent_tasks WHERE id = $1", task_id
        )
        if not row:
            return None
        return TaskRecord(
            id=_str(row["id"]),
            agent_type=row["agent_type"],
            status=TaskStatus(row["status"]),
            trigger=row["trigger"],
            trigger_ref=row["trigger_ref"] or "",
            config=_json(row["config"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result=_json(row["result"]) if row["result"] else None,
            argo_workflow_name=row["argo_workflow_name"] if "argo_workflow_name" in row.keys() else None,
        )

    async def list_tasks(
        self,
        *,
        agent_type: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        conditions = []
        values: list[Any] = []
        idx = 1

        if agent_type:
            conditions.append(f"agent_type = ${idx}")
            values.append(agent_type)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            values.append(status.value)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = await self.pool.fetch(
            f"SELECT * FROM agent_tasks {where} ORDER BY created_at DESC LIMIT ${idx}",
            *values, limit,
        )
        return [
            TaskRecord(
                id=_str(r["id"]),
                agent_type=r["agent_type"],
                status=TaskStatus(r["status"]),
                trigger=r["trigger"],
                trigger_ref=r["trigger_ref"] or "",
                config=_json(r["config"]),
                created_at=r["created_at"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
                result=_json(r["result"]) if r["result"] else None,
                argo_workflow_name=r["argo_workflow_name"] if "argo_workflow_name" in r.keys() else None,
            )
            for r in rows
        ]

    async def delete_task(self, task_id: str) -> None:
        """Delete a task by ID."""
        await self.pool.execute("DELETE FROM agent_tasks WHERE id = $1", task_id)
        log.info("Task deleted: id=%s", task_id[:8])

    async def delete_all_tasks(self) -> int:
        """Delete all tasks. Returns the number deleted."""
        result = await self.pool.execute("DELETE FROM agent_tasks")
        count = int(result.split()[-1])
        log.info("Deleted %d tasks", count)
        return count

    async def count_running_tasks(self, agent_type: str) -> int:
        row = await self.pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM agent_tasks WHERE agent_type = $1 AND status = $2",
            agent_type, TaskStatus.running.value,
        )
        return row["cnt"] if row else 0

    # -----------------------------------------------------------------------
    # LISTEN/NOTIFY
    # -----------------------------------------------------------------------

    async def listen(
        self,
        channel: str,
        callback: Callable[[str], Coroutine],
    ) -> asyncpg.Connection:
        """Subscribe to PG notifications. Returns the connection (caller manages lifecycle)."""
        conn = await asyncpg.connect(self.dsn)
        await conn.add_listener(channel, lambda *args: _notify_adapter(callback, args))
        log.info("Listening on PG channel: %s", channel)
        return conn


def _notify_adapter(callback, args):
    """Bridge asyncpg listener (sync) to async callback."""
    import asyncio
    # args = (connection, pid, channel, payload)
    payload = args[3]
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(callback(payload))
    except RuntimeError:
        pass
