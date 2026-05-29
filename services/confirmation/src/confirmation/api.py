"""Operator-facing REST API.

Minimal in Phase 1 — enough for the Vue dashboard skeleton (built in Phase 3) to
render a live alert feed and for an ops engineer to curl the service.

Auth is deliberately absent here; the real build puts this service behind an
authenticating gateway (Kong/Envoy + OIDC). Never expose this to the public
internet without that gateway.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from veilwatch_contracts.events import AlertDisposition, AuditAction
from veilwatch_contracts.scoring import format_score

from confirmation.alert_store import AlertStore, RecentAlerts
from confirmation.bus_deps import get_bus
from confirmation.live_map import get_broadcaster

# 1x1 transparent PNG returned for cameras that don't have a preview snapshot
# on disk yet. Inline constant so we don't have to ship a file asset with the
# package. Browsers render this as an invisible pixel.
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c636000010000000500010d0a2db40000000049454e"
    "44ae426082"
)


class CameraUpsertBody(BaseModel):
    """Admin payload for POST/PUT /cameras. Validated at the edge so we can
    reject malformed requests before they hit the DB layer."""
    camera_id: str = Field(..., min_length=1, max_length=64)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    environment: str = "public"
    label: str | None = Field(None, max_length=256)
    rtsp_url: str | None = Field(None, max_length=512)
    notes: str | None = None
    active: bool = True


def build_app(
    alerts: AlertStore,
    *,
    cameras_repo: Any = None,
) -> FastAPI:
    """Build the operator FastAPI app.

    `alerts` is any AlertStore — the in-memory RecentAlerts for dev mode,
    or PostgresIncidentsRepo when a database is configured. `cameras_repo`
    is optional; when None the camera admin endpoints return 503. This lets
    the dev_server start without provisioning Postgres.
    """
    # DEV_MODE=1 enables synthetic demo events so the dashboard has something
    # to render without Kafka/Redis. NOT for production.
    dev_mode = os.getenv("DEV_MODE", "").strip().lower() in ("1", "true", "yes")
    demo_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal demo_task
        if dev_mode:
            import logging
            log = logging.getLogger("confirmation.api")
            log.info("DEV_MODE enabled — starting demo event loop")
            from confirmation.demo_events import run_demo_loop
            broadcaster = get_broadcaster()
            demo_task = asyncio.create_task(
                run_demo_loop(alerts, broadcaster, interval_seconds=4.0)
            )
        yield
        if demo_task is not None:
            demo_task.cancel()
            try:
                await demo_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="VeilWatch Confirmation",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS is OFF by default. The Phase 3 dashboard dev flow uses a Vite
    # reverse proxy so the browser sees same-origin requests. Enable only
    # when the dashboard is served from a separate origin in a deployment
    # (e.g. CDN for the SPA + a different hostname for the API). We avoid
    # a permissive wildcard on purpose — this API carries operator-confirm
    # actions and must not be reachable from arbitrary origins.
    cors_origins = os.getenv("CONFIRMATION_CORS_ORIGINS", "").strip()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cors_origins.split(",") if o.strip()],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def _serialise_alert(a: Any) -> dict[str, Any]:
        """Shared serialiser between /alerts/recent and /alerts/search so
        they return an identical shape to the dashboard."""
        return {
            "incident_id": a.incident_id,
            "camera_id": a.camera.camera_id,
            "disposition": a.disposition.value,
            "threat_score": a.threat_score,
            "threat_score_display": format_score(a.threat_score),
            "signal_breakdown": a.signal_breakdown,
            "emitted_at": a.emitted_at.isoformat(),
            "frame_uri": a.frame_uri,
            "primary_detection": {
                "class_name": a.primary_detection.class_name,
                "threat_class": a.primary_detection.threat_class.value,
                "confidence": a.primary_detection.confidence,
            },
            "watchlist_hits": [
                {
                    "identity_id": h.identity_id,
                    "identity_label": h.identity_label,
                    "similarity": h.similarity,
                }
                for h in a.watchlist_hits
            ],
        }

    @app.get("/alerts/recent")
    async def recent_alerts(limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be in [1, 500]")
        return [_serialise_alert(a) for a in await alerts.recent(limit=limit)]

    @app.get("/alerts/search")
    async def search_alerts(
        camera_id: list[str] | None = Query(default=None),
        disposition: list[str] | None = Query(default=None),
        min_score: float | None = Query(default=None),
        since: datetime | None = Query(default=None),
        until: datetime | None = Query(default=None),
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Historical incident search.

        Repeated camera_id / disposition query params combine via IN. The
        response envelope includes the applied filters so the UI can echo
        them back (useful for bookmarkable links)."""
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be in [1, 500]")
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise HTTPException(400, "min_score must be in [0, 1]")
        if since and until and since > until:
            raise HTTPException(400, "since must be <= until")

        rows = await alerts.search(
            camera_ids=camera_id,
            dispositions=disposition,
            min_score=min_score,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return {
            "filters": {
                "camera_id": camera_id,
                "disposition": disposition,
                "min_score": min_score,
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
            },
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "results": [_serialise_alert(a) for a in rows],
        }

    # ---------- Camera registry ------------------------------------------------
    # All camera endpoints return 503 if no Postgres-backed repo was wired in,
    # so the dev_server can boot without a database. The dashboard detects the
    # 503 and falls back to the static demo camera set.

    def _require_cameras() -> Any:
        if cameras_repo is None:
            raise HTTPException(
                503, "camera registry requires Postgres; start with POSTGRES_HOST set"
            )
        return cameras_repo

    @app.get("/cameras")
    async def list_cameras(active_only: bool = False) -> list[dict[str, Any]]:
        repo = _require_cameras()
        return await repo.list_all(active_only=active_only)

    @app.get("/cameras/{camera_id}")
    async def get_camera(camera_id: str) -> dict[str, Any]:
        repo = _require_cameras()
        cam = await repo.get(camera_id)
        if cam is None:
            raise HTTPException(404, f"camera {camera_id} not found")
        return cam

    @app.post("/cameras", status_code=201)
    @app.put("/cameras/{camera_id}")
    async def upsert_camera(body: CameraUpsertBody, camera_id: str | None = None) -> dict[str, Any]:
        """Create-or-update a camera. POST uses the body's camera_id; PUT
        uses the URL path arg (and ignores the body id if they differ —
        URL wins for PUT semantics)."""
        repo = _require_cameras()
        if body.environment not in ("road", "retail", "hospitality", "residential", "public"):
            raise HTTPException(400, "invalid environment")
        cid = camera_id or body.camera_id
        return await repo.upsert(
            camera_id=cid,
            latitude=body.latitude,
            longitude=body.longitude,
            environment=body.environment,
            label=body.label,
            rtsp_url=body.rtsp_url,
            notes=body.notes,
            active=body.active,
        )

    @app.post("/cameras/{camera_id}/deactivate")
    async def deactivate_camera(camera_id: str) -> dict[str, str]:
        repo = _require_cameras()
        if not await repo.set_active(camera_id, False):
            raise HTTPException(404, f"camera {camera_id} not found")
        return {"camera_id": camera_id, "status": "deactivated"}

    @app.post("/cameras/{camera_id}/activate")
    async def activate_camera(camera_id: str) -> dict[str, str]:
        repo = _require_cameras()
        if not await repo.set_active(camera_id, True):
            raise HTTPException(404, f"camera {camera_id} not found")
        return {"camera_id": camera_id, "status": "activated"}

    # ---------- Internal endpoints (trusted callers only) ---------------------
    # In production these live behind an internal network boundary / mTLS gate.
    # For dev we trust localhost; production MUST enforce this at the gateway.

    def _check_internal(authorization: str | None) -> None:
        # Simple shared-secret gate for dev. Production replaces with mTLS
        # or OIDC service-to-service tokens enforced at the ingress.
        expected = os.getenv("INTERNAL_API_TOKEN", "").strip()
        if not expected:
            return  # no token set → trust same-host callers (dev default)
        if authorization != f"Bearer {expected}":
            raise HTTPException(401, "invalid internal token")

    @app.get("/internal/cameras/sources")
    async def internal_camera_sources(
        active_only: bool = True,
        authorization: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        """Return cameras with their rtsp_url. Trusted internal callers only
        (capture service). The public /cameras endpoint REDACTS rtsp_url
        because stream URLs are effectively credentials."""
        _check_internal(authorization)
        repo = _require_cameras()
        return await repo.list_all(active_only=active_only, include_sensitive=True)

    # ---------- Frame preview (dev) -------------------------------------------
    # The capture dev_preview runner writes latest.jpg to
    # PREVIEW_DIR/{camera_id}/latest.jpg. We serve it back here so the UI
    # can show live thumbnails without running the full Kafka pipeline.

    from pathlib import Path as _Path
    from fastapi.responses import FileResponse, Response as _FastAPIResponse

    preview_root = _Path(os.getenv("PREVIEW_DIR", "./frames-dev")).resolve()

    @app.get("/cameras/{camera_id}/preview.jpg")
    async def camera_preview(camera_id: str) -> Any:
        # Guard against path traversal — camera_id is user-controlled.
        if "/" in camera_id or ".." in camera_id:
            raise HTTPException(400, "invalid camera_id")
        path = preview_root / camera_id / "latest.jpg"
        if not path.exists():
            # Return a 1x1 transparent PNG so <img> tags don't show broken
            # icons while the runner is still warming up.
            return _FastAPIResponse(
                content=_PLACEHOLDER_PNG,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/alerts/{incident_id}/operator-confirm")
    async def operator_confirm(incident_id: str, operator_id: str) -> dict[str, str]:
        """Human operator gate. Promotes an OPERATOR_REVIEW alert to confirmed.

        Constraint #2 from the charter: no automated alert triggers a dispatch
        without a logged confirmation event — this endpoint is that logged
        event for the sub-autoconfirm branch."""
        target = next(
            (a for a in await alerts.recent(limit=500) if a.incident_id == incident_id),
            None,
        )
        if target is None:
            raise HTTPException(404, "incident not found in recent window")
        if target.disposition != AlertDisposition.OPERATOR_REVIEW:
            raise HTTPException(409, f"incident is {target.disposition.value}, not operator_review")

        bus = get_bus()
        await bus.audit(
            action=AuditAction.OPERATOR_CONFIRMED,
            subject_id=incident_id,
            correlation_ids={"camera_id": target.camera.camera_id},
            actor_id=operator_id,
            payload={"threat_score": target.threat_score},
        )
        # Re-emit as AUTO_CONFIRMED-equivalent on the alerts topic so downstream
        # dispatch consumers see a single canonical "this is live" signal.
        confirmed = target.model_copy(update={
            "disposition": AlertDisposition.AUTO_CONFIRMED,
            "confirming_operator_id": operator_id,
        })
        from veilwatch_contracts import Topics
        await bus.publish(Topics.ALERTS_CONFIRMED, confirmed, key=target.camera.camera_id)
        # Re-persist so GET /alerts/recent reflects the new disposition. For
        # the in-memory store this just prepends a new copy; for Postgres it
        # upserts on incident_id so we end up with exactly one row per
        # incident across its lifecycle.
        await alerts.add(confirmed)
        return {"incident_id": incident_id, "status": "confirmed"}

    @app.websocket("/ws/live-map")
    async def live_map_ws(websocket: WebSocket) -> None:
        """Operator dashboard live-state feed. Streams alerts + cross-camera
        handoffs in real time. Clients disconnect automatically if their read
        rate can't keep up — see LiveMapBroadcaster's back-pressure policy."""
        await websocket.accept()
        broadcaster = get_broadcaster()
        remote = (
            f"{websocket.client.host}:{websocket.client.port}"
            if websocket.client else "unknown"
        )
        client = await broadcaster.register(remote)
        try:
            while True:
                # Pull from this client's per-connection queue, then forward
                # to the socket. If the socket's own write buffer is full,
                # uvicorn will raise — we treat that the same as a disconnect.
                envelope = await client.queue.get()
                await websocket.send_text(envelope)
        except WebSocketDisconnect:
            pass
        finally:
            await broadcaster.unregister(client)

    return app
