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
        ttl_days: int | None = None,
    ) -> str:
        """Write a record to the KB. Returns the record ID.

        ttl_days: if set, the record expires after this many days (short-term memory).
        """
        self._check_permission(scope, "write")

        record_id = str(uuid.uuid4())
        embedding = None
        if needs_embedding and self.use_embeddings:
            embedding = embed(content)

        expires_at = None
        if ttl_days is not None:
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

        await self.pool.execute(
            """
            INSERT INTO memory_records
                (id, content, embedding, scope, categories, metadata, importance, source,
                 needs_embedding, expires_at)
            VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10)
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
            expires_at,
        )

        # Notify listeners
        event = json.dumps({"scope": scope, "record_id": record_id, "source": source})
        await self.pool.execute("SELECT pg_notify('agent_events', $1)", event)

        log.debug("KB write: scope=%s id=%s embed=%s ttl=%s", scope, record_id[:8],
                  embedding is not None, ttl_days)
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
              AND (expires_at IS NULL OR expires_at > NOW())
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

    async def ensure_schema(self) -> None:
        """Ensure all schema extensions are applied (safe to call repeatedly)."""
        async with self.pool.acquire() as conn:
            # expires_at for short-term memory (TTL records)
            col_exists = await conn.fetchval("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'memory_records' AND column_name = 'expires_at'
            """)
            if not col_exists:
                await conn.execute(
                    "ALTER TABLE memory_records ADD COLUMN expires_at TIMESTAMPTZ"
                )
                await conn.execute(
                    "CREATE INDEX memory_records_expires_at ON memory_records(expires_at) "
                    "WHERE expires_at IS NOT NULL"
                )
                log.info("Added expires_at column to memory_records")

    async def cleanup_expired(self) -> int:
        """Delete records past their TTL. Returns count deleted."""
        result = await self.pool.execute(
            "DELETE FROM memory_records WHERE expires_at IS NOT NULL AND expires_at < NOW()"
        )
        count = int(result.split()[-1])
        if count:
            log.info("Cleaned up %d expired KB records", count)
        return count

    async def get_unchecked(self, scope: str) -> MemoryRecord | None:
        """Get a record by scope, bypassing permission checks (for coordinator-injected context)."""
        row = await self.pool.fetchrow(
            """
            SELECT id, content, scope, categories, metadata, importance, source,
                   needs_embedding, created_at
            FROM memory_records
            WHERE scope = $1
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
            LIMIT 1
            """,
            scope,
        )
        if not row:
            return None
        return MemoryRecord(
            id=_str(row["id"]),
            content=row["content"],
            scope=row["scope"],
            categories=list(row["categories"] or []),
            metadata=_json(row["metadata"]),
            importance=row["importance"],
            source=row["source"],
        )

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

        try:
            await self.pool.execute(
                f"UPDATE agent_tasks SET {', '.join(set_parts)} WHERE id = $1",
                *values,
            )
        except asyncpg.exceptions.UndefinedColumnError as e:
            if "argo_workflow_name" in str(e) and "argo_workflow_name" in fields:
                # Column not yet migrated — retry without it
                log.warning("argo_workflow_name column missing, skipping (run manual migration)")
                await self.update_task(task_id, **{k: v for k, v in fields.items() if k != "argo_workflow_name"})
            else:
                raise

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


    # -----------------------------------------------------------------------
    # KB Explorer — browse / CRUD
    # -----------------------------------------------------------------------

    async def list_children(
        self,
        prefix: str,
        since_minutes: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """List immediate children (dirs + leaf entries) under a path prefix.

        Returns items sorted dirs-first then entries, both alphabetically.
        Each item: {type, name, path, count, last_updated, source?, description?}
        """
        norm = prefix.rstrip("/") + "/"
        time_clause = (
            f"AND created_at > NOW() - INTERVAL '{int(since_minutes)} minutes'"
            if since_minutes is not None
            else ""
        )

        rows = await self.pool.fetch(
            f"""
            SELECT DISTINCT ON (scope)
                scope, source, created_at, metadata, needs_embedding
            FROM memory_records
            WHERE scope LIKE $1
            {time_clause}
            ORDER BY scope, created_at DESC
            LIMIT $2
            """,
            norm + "%",
            limit,
        )

        children: dict[str, dict] = {}
        prefix_len = len(norm)

        for row in rows:
            scope = row["scope"]
            relative = scope[prefix_len:]
            if not relative:
                continue
            parts = relative.split("/")
            child_name = parts[0]
            is_leaf = len(parts) == 1
            child_path = norm + child_name

            if child_name not in children:
                children[child_name] = {
                    "type": "entry" if is_leaf else "dir",
                    "name": child_name,
                    "path": child_path,
                    "count": 0,
                    "last_updated": None,
                    "source": None,
                    "needs_embedding": None,
                    "description": "",
                }

            if not is_leaf:
                children[child_name]["type"] = "dir"

            children[child_name]["count"] += 1

            row_ts = str(row["created_at"]) if row["created_at"] else None
            last = children[child_name]["last_updated"]
            if row_ts and (not last or row_ts > last):
                children[child_name]["last_updated"] = row_ts

            if is_leaf and children[child_name]["source"] is None:
                children[child_name]["source"] = row["source"]
                children[child_name]["needs_embedding"] = row["needs_embedding"]
                meta = _json(row["metadata"])
                children[child_name]["description"] = meta.get("description", "")

        return sorted(
            children.values(),
            key=lambda x: (x["type"] == "entry", x["name"]),
        )

    async def get_by_scope(self, scope: str) -> dict | None:
        """Get the most recent KB record at the exact scope. Returns raw dict."""
        row = await self.pool.fetchrow(
            """
            SELECT id, content, scope, source, created_at, metadata, needs_embedding
            FROM memory_records
            WHERE scope = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            scope,
        )
        if not row:
            return None
        return {
            "id": _str(row["id"]),
            "scope": row["scope"],
            "content": row["content"],
            "source": row["source"],
            "created_at": str(row["created_at"]) if row["created_at"] else None,
            "metadata": _json(row["metadata"]),
            "needs_embedding": row["needs_embedding"],
        }

    async def delete_by_scope(self, scope: str) -> int:
        """Delete all records at the exact scope. Returns count deleted."""
        result = await self.pool.execute(
            "DELETE FROM memory_records WHERE scope = $1", scope
        )
        return int(result.split()[-1])

    async def delete_by_prefix(self, prefix: str) -> int:
        """Delete all records under a path prefix. Returns count deleted."""
        if prefix in ("/", ""):
            raise ValueError("Cannot delete root prefix")
        norm = prefix.rstrip("/") + "/"
        result = await self.pool.execute(
            "DELETE FROM memory_records WHERE scope LIKE $1", norm + "%"
        )
        return int(result.split()[-1])

    async def count_by_prefix(self, prefix: str) -> int:
        """Count distinct scopes under a path prefix."""
        norm = prefix.rstrip("/") + "/"
        row = await self.pool.fetchrow(
            "SELECT COUNT(DISTINCT scope) FROM memory_records WHERE scope LIKE $1",
            norm + "%",
        )
        return row[0] if row else 0

    async def upsert_by_scope(
        self,
        scope: str,
        content: str,
        source: str = "ui",
        metadata: dict[str, Any] | None = None,
        needs_embedding: bool = True,
    ) -> str:
        """Write/replace content at the given scope. Returns new record ID."""
        record_id = str(uuid.uuid4())
        await self.pool.execute("DELETE FROM memory_records WHERE scope = $1", scope)
        await self.pool.execute(
            """
            INSERT INTO memory_records
                (id, content, scope, categories, metadata, importance,
                 source, needs_embedding, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL)
            """,
            record_id,
            content,
            scope,
            [],
            json.dumps(metadata or {}),
            0.5,
            source,
            needs_embedding,
        )
        log.debug("KB upsert: scope=%s id=%s", scope, record_id[:8])
        return record_id

    async def list_records_for_task(
        self,
        task_id: str,
        agent_type: str | None = None,
        started_at=None,
        completed_at=None,
    ) -> list[dict]:
        """Return KB records associated with a task.

        Two groups:
        - "direct": scope contains the task_id (inbox, results, conversation)
        - "during": records written while the task was running under the agent's paths
        """
        def _row_dict(row, group: str) -> dict:
            return {
                "scope": row["scope"],
                "source": row["source"],
                "created_at": str(row["created_at"]) if row["created_at"] else None,
                "needs_embedding": row["needs_embedding"],
                "content_preview": "",
                "group": group,
            }

        # Direct — scope contains task_id
        direct_rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (scope)
                scope, source, created_at, needs_embedding
            FROM memory_records
            WHERE scope LIKE $1
            ORDER BY scope, created_at DESC
            """,
            f"%{task_id}%",
        )
        results = [_row_dict(r, "direct") for r in direct_rows]
        direct_scopes = {r["scope"] for r in results}

        # During — records written while the task ran
        if started_at and completed_at and agent_type:
            agent_prefix = f"/agents/{agent_type}/%"
            tasks_prefix = f"/tasks/%"
            during_rows = await self.pool.fetch(
                """
                SELECT DISTINCT ON (scope)
                    scope, source, created_at, needs_embedding
                FROM memory_records
                WHERE (scope LIKE $1 OR scope LIKE $2)
                  AND created_at >= $3
                  AND created_at <= $4
                ORDER BY scope, created_at DESC
                """,
                agent_prefix,
                tasks_prefix,
                started_at,
                completed_at,
            )
            for row in during_rows:
                if row["scope"] not in direct_scopes:
                    results.append(_row_dict(row, "during"))

        results.sort(key=lambda x: x["created_at"] or "")
        return results


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
