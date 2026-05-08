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

    _KEEPALIVE_INTERVAL = 30  # seconds between SELECT 1 pings on listener conn
    _RECONNECT_DELAY = 5      # seconds to wait before reconnecting after failure

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.kb = KBClient(dsn, permissions=None)  # coordinator has full access
        self._listener_conn: asyncpg.Connection | None = None
        self._event_callback: Callable[[dict[str, Any]], Coroutine] | None = None
        self._listener_task: asyncio.Task | None = None

    async def connect(self) -> None:
        await self.kb.connect()
        log.info("Coordinator DB connected")

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._listener_conn:
            await self._listener_conn.close()
        await self.kb.close()

    async def start_listener(
        self,
        callback: Callable[[dict[str, Any]], Coroutine],
    ) -> None:
        """Start a self-healing LISTEN loop with keepalive pings."""
        self._event_callback = callback
        self._listener_task = asyncio.create_task(self._listener_loop())

    async def _listener_loop(self) -> None:
        """Reconnect-on-failure loop for the agent_events LISTEN channel."""
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self.dsn)
                self._listener_conn = conn
                await conn.add_listener("agent_events", self._on_notify)
                log.info("Listening on agent_events channel")
                # Keepalive: ping every 30s; breaks inner loop on failure → reconnects
                while True:
                    await asyncio.sleep(self._KEEPALIVE_INTERVAL)
                    await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("agent_events listener lost (%s), reconnecting in %ds", exc, self._RECONNECT_DELAY)
            finally:
                if conn:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                self._listener_conn = None
            await asyncio.sleep(self._RECONNECT_DELAY)

    def _on_notify(self, conn, pid, channel, payload):
        if self._event_callback is None:
            return
        try:
            event = json.loads(payload)
            loop = asyncio.get_running_loop()
            loop.create_task(self._event_callback(event))
        except Exception:
            log.exception("Error handling notification: %s", payload)
