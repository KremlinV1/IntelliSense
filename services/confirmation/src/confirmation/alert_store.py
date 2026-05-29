"""Recent-alerts store for the operator REST API.

Two implementations, selected at runtime:
  - RecentAlerts: in-memory ring buffer (dev mode, tests, no Postgres).
  - PostgresAlertsAdapter: persistent, queryable via incidents_repo.

Both expose the same narrow `add(alert)` / `recent(limit)` / `search(...)`
surface so the confirmation pipeline doesn't know which it holds. The in-
memory variant's `search` is a best-effort filter over the ring — good
enough for dev but intentionally not feature-complete.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from threading import Lock
from typing import Iterable, Protocol, runtime_checkable

from veilwatch_contracts.events import ConfirmedAlert


@runtime_checkable
class AlertStore(Protocol):
    """Interface the confirmation pipeline and REST API code against."""

    async def add(self, alert: ConfirmedAlert) -> None: ...

    async def recent(self, limit: int = 50) -> list[ConfirmedAlert]: ...

    async def search(
        self,
        *,
        camera_ids: Iterable[str] | None = None,
        dispositions: Iterable[str] | None = None,
        min_score: float | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConfirmedAlert]: ...


class RecentAlerts:
    """In-memory ring buffer. Async methods for protocol compatibility even
    though the ops are synchronous — await is a no-op for the caller."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[ConfirmedAlert] = deque(maxlen=maxlen)
        self._lock = Lock()

    # Back-compat: pre-slice-2 code calls add() synchronously. We keep both
    # sync and async entry points so nothing breaks.
    def add_sync(self, alert: ConfirmedAlert) -> None:
        with self._lock:
            self._buf.appendleft(alert)

    async def add(self, alert: ConfirmedAlert) -> None:
        self.add_sync(alert)

    def recent_sync(self, limit: int = 50) -> list[ConfirmedAlert]:
        with self._lock:
            return list(self._buf)[:limit]

    async def recent(self, limit: int = 50) -> list[ConfirmedAlert]:
        return self.recent_sync(limit)

    async def search(
        self,
        *,
        camera_ids: Iterable[str] | None = None,
        dispositions: Iterable[str] | None = None,
        min_score: float | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConfirmedAlert]:
        """Best-effort in-memory filter. Fine for dev; Postgres does this
        properly with indexes."""
        cam_set = set(camera_ids) if camera_ids else None
        disp_set = set(dispositions) if dispositions else None
        with self._lock:
            rows = list(self._buf)
        out: list[ConfirmedAlert] = []
        for a in rows:
            if cam_set and a.camera.camera_id not in cam_set:
                continue
            if disp_set and a.disposition.value not in disp_set:
                continue
            if min_score is not None and a.threat_score < min_score:
                continue
            if since is not None and a.emitted_at < since:
                continue
            if until is not None and a.emitted_at > until:
                continue
            out.append(a)
        return out[offset : offset + limit]
