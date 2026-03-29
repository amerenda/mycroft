"""Database pool and LISTEN/NOTIFY management for the coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import asyncpg

from common.kb import KBClient

log = logging.getLogger(__name__)


class CoordinatorDB:
    """Manages the coordinator's DB pool and event listener."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.kb = KBClient(dsn, permissions=None)  # coordinator has full access
        self._listener_conn: asyncpg.Connection | None = None
        self._event_callback: Callable[[dict[str, Any]], Coroutine] | None = None

    async def connect(self) -> None:
        await self.kb.connect()
        log.info("Coordinator DB connected")

    async def close(self) -> None:
        if self._listener_conn:
            await self._listener_conn.close()
        await self.kb.close()

    async def start_listener(
        self,
        callback: Callable[[dict[str, Any]], Coroutine],
    ) -> None:
        """Start listening for agent_events notifications."""
        self._event_callback = callback
        self._listener_conn = await asyncpg.connect(self.dsn)
        await self._listener_conn.add_listener("agent_events", self._on_notify)
        log.info("Listening on agent_events channel")

    def _on_notify(self, conn, pid, channel, payload):
        if self._event_callback is None:
            return
        try:
            event = json.loads(payload)
            loop = asyncio.get_running_loop()
            loop.create_task(self._event_callback(event))
        except Exception:
            log.exception("Error handling notification: %s", payload)
