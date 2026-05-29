"""Camera registry repository.

CRUD over the `cameras` table. Consumed by both the operator dashboard's
admin UI and the capture service's config reload endpoint (Slice 3).

Design decisions:
  - No soft-delete yet: DELETE on a camera with incidents is blocked by the
    foreign key (ondelete=RESTRICT). Operators must first archive or
    reassign related incidents. This is intentional — deleting a camera
    that has evidence tied to it would break audit.
  - `active=False` is the right knob for "retire this camera without
    destroying its history".
  - `rtsp_url` is stored but never returned in the list API response unless
    the caller passes include_sensitive=True. Stream URLs are secrets.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from confirmation.db.models import Camera


def _camera_to_dict(cam: Camera, *, include_sensitive: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "camera_id": cam.camera_id,
        "label": cam.label,
        "environment": cam.environment,
        "latitude": float(cam.latitude),
        "longitude": float(cam.longitude),
        "active": cam.active,
        "notes": cam.notes,
        "created_at": cam.created_at.isoformat(),
        "updated_at": cam.updated_at.isoformat(),
    }
    if include_sensitive:
        out["rtsp_url"] = cam.rtsp_url
    return out


class PostgresCameraRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def list_all(
        self,
        *,
        active_only: bool = False,
        include_sensitive: bool = False,
    ) -> list[dict[str, Any]]:
        """List cameras. `include_sensitive=True` exposes `rtsp_url` and must
        only be used by trusted internal callers (the capture service). The
        public operator UI always uses the default."""
        async with self._sm() as session:
            stmt = select(Camera).order_by(Camera.camera_id)
            if active_only:
                stmt = stmt.where(Camera.active.is_(True))
            result = await session.execute(stmt)
            return [
                _camera_to_dict(c, include_sensitive=include_sensitive)
                for c in result.scalars()
            ]

    async def get(self, camera_id: str) -> dict[str, Any] | None:
        async with self._sm() as session:
            cam = await session.get(Camera, camera_id)
            return _camera_to_dict(cam) if cam else None

    async def upsert(
        self,
        *,
        camera_id: str,
        latitude: float,
        longitude: float,
        environment: str = "public",
        label: str | None = None,
        rtsp_url: str | None = None,
        notes: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        async with self._sm() as session:
            existing = await session.get(Camera, camera_id)
            if existing is None:
                cam = Camera(
                    camera_id=camera_id,
                    latitude=latitude,
                    longitude=longitude,
                    environment=environment,
                    label=label,
                    rtsp_url=rtsp_url,
                    notes=notes,
                    active=active,
                    created_at=now,
                    updated_at=now,
                )
                session.add(cam)
            else:
                existing.latitude = latitude
                existing.longitude = longitude
                existing.environment = environment
                existing.label = label
                existing.rtsp_url = rtsp_url
                existing.notes = notes
                existing.active = active
                existing.updated_at = now
                cam = existing
            await session.commit()
            await session.refresh(cam)
            return _camera_to_dict(cam)

    async def set_active(self, camera_id: str, active: bool) -> bool:
        async with self._sm() as session:
            cam = await session.get(Camera, camera_id)
            if cam is None:
                return False
            cam.active = active
            cam.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return True
