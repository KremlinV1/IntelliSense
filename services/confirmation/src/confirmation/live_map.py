"""Live-map WebSocket broadcaster for the operator dashboard.

Fans out two streams to any connected operator client:
  - ConfirmedAlert (from the confirmation pipeline, in-process)
  - CrossCameraMatch (consumed from `tracks.handoffs.v1`)

Clients connect to /ws/live-map. Every connected client gets every message
from the moment of connection onwards. We do NOT replay history — the
`/alerts/recent` REST endpoint is the right tool for that; the WebSocket is
strictly live state.

Back-pressure: each client has a bounded per-client queue. If a client is
too slow and the queue fills up, we drop the client rather than block the
broadcaster (which would block the confirmation pipeline). This is the
right trade-off: the dashboard should reconnect + refresh via REST.

Message envelope:
  {"type": "alert" | "handoff", "data": {...}}

Serialisation: pydantic model_dump(mode="json") — cheap, well-tested,
matches the REST API's shape so the dashboard can share code.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

_CLIENT_QUEUE_MAXSIZE = 64  # drop client at 64 undelivered messages


@dataclass(eq=False)
class _Client:
    """eq=False → hash by identity so instances can live in a set. Two
    connections from the same IP are distinct clients."""
    queue: asyncio.Queue
    remote_addr: str


class LiveMapBroadcaster:
    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self._lock = asyncio.Lock()

    async def register(self, remote_addr: str) -> _Client:
        client = _Client(
            queue=asyncio.Queue(maxsize=_CLIENT_QUEUE_MAXSIZE),
            remote_addr=remote_addr,
        )
        async with self._lock:
            self._clients.add(client)
        log.info("live-map client connected",
                 extra={"remote": remote_addr, "total_clients": len(self._clients)})
        return client

    async def unregister(self, client: _Client) -> None:
        async with self._lock:
            self._clients.discard(client)
        log.info("live-map client disconnected",
                 extra={"remote": client.remote_addr, "total_clients": len(self._clients)})

    async def publish(self, message_type: str, payload: BaseModel) -> None:
        """Non-blocking fan-out. Full-queue clients are dropped."""
        envelope = json.dumps({
            "type": message_type,
            "data": payload.model_dump(mode="json"),
        })
        dead: list[_Client] = []
        async with self._lock:
            clients_snapshot = list(self._clients)
        for client in clients_snapshot:
            try:
                client.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                log.warning("live-map client queue full; dropping",
                            extra={"remote": client.remote_addr})
                dead.append(client)
        for client in dead:
            await self.unregister(client)

    def client_count(self) -> int:
        return len(self._clients)


# Module-level singleton so the Kafka consumer loop and the FastAPI WebSocket
# handler can share the same broadcaster without a complex DI setup.
_broadcaster: LiveMapBroadcaster | None = None


def get_broadcaster() -> LiveMapBroadcaster:
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = LiveMapBroadcaster()
    return _broadcaster
