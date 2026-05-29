"""Postgres-backed incident repository.

Implements the same narrow surface as the in-memory RecentAlerts ring buffer
(add, recent) plus a richer search() for the operator history view. Both
backends share a common Protocol in alert_store.py so the confirmation
pipeline doesn't need to know which one it's holding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from veilwatch_contracts.events import (
    AlertDisposition,
    BoundingBox,
    CameraRef,
    ConfirmedAlert,
    Detection,
    ThreatClass,
    WatchlistHit,
)

from confirmation.db.models import Incident


def _alert_to_row(alert: ConfirmedAlert) -> dict[str, Any]:
    """Flatten a ConfirmedAlert into the incidents table row shape."""
    return {
        "incident_id": alert.incident_id,
        "camera_id": alert.camera.camera_id,
        "disposition": alert.disposition.value,
        "threat_score": alert.threat_score,
        "primary_class_name": alert.primary_detection.class_name,
        "primary_threat_class": alert.primary_detection.threat_class.value,
        "primary_confidence": alert.primary_detection.confidence,
        "signal_breakdown": dict(alert.signal_breakdown),
        "watchlist_hits": [
            {
                "detection_id": h.detection_id,
                "identity_id": h.identity_id,
                "identity_label": h.identity_label,
                "similarity": h.similarity,
            }
            for h in alert.watchlist_hits
        ],
        "frame_uri": alert.frame_uri,
        "frame_sha256": alert.frame_sha256,
        "confirming_operator_id": alert.confirming_operator_id,
        "emitted_at": alert.emitted_at,
        "stored_at": datetime.now(timezone.utc),
    }


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite can't store timezone info — a DateTime(timezone=True) column
    round-trips as a naive datetime. We only ever store UTC (pipeline
    stamps tz-aware UTC emitted_at), so re-attach it on the way out.

    On Postgres, timestamptz preserves the tz already and this is a no-op.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_alert(row: Incident, *, camera: CameraRef | None = None) -> ConfirmedAlert:
    """Rehydrate a persisted row into the full ConfirmedAlert contract.

    Note: we lose the original Detection.bbox (we don't persist it — the
    frame_uri is enough for forensic replay). We synthesise a placeholder
    bbox so the ConfirmedAlert validates. This is documented in the schema
    comment.
    """
    # Caller may already have a CameraRef from the cameras table; if not,
    # synthesise a minimal one from the camera_id so the event validates.
    # The dashboard gets full CameraRefs by joining against the cameras
    # table in the search endpoint.
    cam = camera or CameraRef(
        camera_id=row.camera_id, latitude=0.0, longitude=0.0
    )
    detection = Detection(
        detection_id=f"rehydrated-{row.incident_id}",
        class_name=row.primary_class_name,
        threat_class=ThreatClass(row.primary_threat_class),
        confidence=float(row.primary_confidence),
        bbox=BoundingBox(x1=0, y1=0, x2=1, y2=1),  # placeholder — frame is source of truth
        embedding=None,
    )
    hits = tuple(
        WatchlistHit(
            detection_id=h["detection_id"],
            identity_id=h["identity_id"],
            identity_label=h["identity_label"],
            similarity=h["similarity"],
        )
        for h in (row.watchlist_hits or [])
    )
    return ConfirmedAlert(
        event_id=row.incident_id,  # best-effort reuse; audit records the original
        incident_id=row.incident_id,
        camera=cam,
        frame_id=row.incident_id,
        frame_uri=row.frame_uri,
        frame_sha256=row.frame_sha256,
        threat_score=float(row.threat_score),
        disposition=AlertDisposition(row.disposition),
        primary_detection=detection,
        watchlist_hits=hits,
        signal_breakdown=dict(row.signal_breakdown),
        confirming_operator_id=row.confirming_operator_id,
        emitted_at=_ensure_utc(row.emitted_at),
    )


class PostgresIncidentsRepo:
    """Postgres-backed replacement for RecentAlerts.

    Same add()/recent() interface + search() for the history UI. Upsert is
    used for add() so the confirmation pipeline can safely retry an event
    after a crash without duplicating rows (incident_id is the idempotency
    key).
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def add(self, alert: ConfirmedAlert) -> None:
        row = _alert_to_row(alert)
        async with self._sm() as session:
            # ON CONFLICT DO UPDATE — re-emitting an incident (e.g. operator
            # confirm promoting review → confirmed) should overwrite, not
            # error. SQLite in tests takes the same DO UPDATE path via
            # sqlite's compatible INSERT OR REPLACE variant.
            if session.bind.dialect.name == "postgresql":
                stmt = pg_insert(Incident).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["incident_id"],
                    set_={k: stmt.excluded[k] for k in row if k != "incident_id"},
                )
                await session.execute(stmt)
            else:
                # SQLite path for tests. Do a manual delete-then-insert.
                existing = await session.get(Incident, row["incident_id"])
                if existing is not None:
                    await session.delete(existing)
                    await session.flush()
                session.add(Incident(**row))
            await session.commit()

    async def recent(self, limit: int = 50) -> list[ConfirmedAlert]:
        async with self._sm() as session:
            result = await session.execute(
                select(Incident)
                .order_by(Incident.emitted_at.desc())
                .limit(limit)
            )
            return [_row_to_alert(r) for r in result.scalars()]

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
        """Operator history search. All filters are optional and combined
        with AND. Results always ordered newest-first."""
        filters = []
        if camera_ids:
            filters.append(Incident.camera_id.in_(list(camera_ids)))
        if dispositions:
            filters.append(Incident.disposition.in_(list(dispositions)))
        if min_score is not None:
            filters.append(Incident.threat_score >= min_score)
        if since is not None:
            filters.append(Incident.emitted_at >= since)
        if until is not None:
            filters.append(Incident.emitted_at <= until)

        stmt = select(Incident).order_by(Incident.emitted_at.desc())
        if filters:
            stmt = stmt.where(and_(*filters))
        stmt = stmt.limit(min(limit, 500)).offset(max(offset, 0))

        async with self._sm() as session:
            result = await session.execute(stmt)
            return [_row_to_alert(r) for r in result.scalars()]
