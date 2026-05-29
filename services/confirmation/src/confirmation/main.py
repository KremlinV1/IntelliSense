"""Confirmation service entrypoint.

Runs FOUR concurrent tasks:
  - Detection consumer: DetectionEvent → ConfirmationPipeline.handle(...)
  - Track-update consumer (Slice 2): TrackUpdate → GlobalIdentityView.observe(...)
  - Track GC loop: evict stale per-camera tracks
  - Uvicorn HTTP server: operator REST API

The detection and track-update consumers run on the SAME EventBus but
different consumer groups, so Kafka delivers both streams independently
with their own offsets. Ordering between the two streams is NOT preserved
— the pipeline is designed to tolerate that race (see scoring.signal_corroboration).

Shutdown is coordinated via a shared asyncio.Event so SIGTERM drains cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import uvicorn

from veilwatch_contracts import CrossCameraMatch, DetectionEvent, Topics, TrackUpdate
from veilwatch_contracts.bus import EventBus
from veilwatch_contracts.obs import configure_logging, set_correlation

from confirmation.alert_store import AlertStore, RecentAlerts
from confirmation.api import build_app
from confirmation.bus_deps import set_bus
from confirmation.config import ConfirmationConfig
from confirmation.identity_view import GlobalIdentityView
from confirmation.live_map import get_broadcaster
from confirmation.pipeline import ConfirmationPipeline
from confirmation.track_store import InMemoryTrackStore

SERVICE = "confirmation"


async def _consume_loop(
    bus: EventBus,
    pipeline: ConfirmationPipeline,
    cfg: ConfirmationConfig,
    stop: asyncio.Event,
) -> None:
    log = logging.getLogger(SERVICE)
    async for event in bus.consume(
        topic=Topics.DETECTION_EVENTS,
        group_id=cfg.consumer_group,
        schema=DetectionEvent,
        dlq_topic=Topics.DLQ_DETECTION_EVENTS,
    ):
        if stop.is_set():
            break
        set_correlation(
            camera_id=event.camera.camera_id,
            frame_id=event.frame_id,
        )
        try:
            await pipeline.handle(event)
        except Exception:
            log.exception("confirmation handler failed",
                          extra={"frame_id": event.frame_id})


async def _track_update_loop(
    bus: EventBus,
    identity_view: GlobalIdentityView,
    cfg: ConfirmationConfig,
    stop: asyncio.Event,
) -> None:
    """Keeps the GlobalIdentityView in sync with the tracker's output.

    Uses a DISTINCT consumer group from the detection consumer so Kafka
    delivers the tracks.updates stream independently. The two streams race
    by design — see scoring.signal_corroboration for the fallback logic.

    A distinct group id per service instance would also be valid here
    (broadcast semantics for the identity view), but a shared group id
    matches the horizontal-scale story we want in Slice 3: each confirmation
    instance sees a slice of the track updates, and GlobalIdentityView will
    move to Redis so the combined view is global.
    """
    log = logging.getLogger(SERVICE)
    async for update in bus.consume(
        topic=Topics.TRACKS_UPDATES,
        group_id=f"{cfg.consumer_group}-tracks",
        schema=TrackUpdate,
        dlq_topic=Topics.DLQ_TRACKS_UPDATES,
    ):
        if stop.is_set():
            break
        try:
            identity_view.observe(
                global_track_id=update.global_track_id,
                camera_id=update.camera.camera_id,
                detection_id=update.detection_id,
            )
        except Exception:
            log.exception("identity_view.observe failed",
                          extra={"global_track_id": update.global_track_id})


async def _handoff_broadcast_loop(
    bus: EventBus,
    cfg: ConfirmationConfig,
    stop: asyncio.Event,
) -> None:
    """Mirrors CrossCameraMatch events from Kafka to the live-map WebSocket
    broadcaster. Separate consumer from track-updates because (a) the volume
    is much lower (only handoffs, not every person observation) and (b) the
    dashboard should be able to subscribe to this topic independently."""
    log = logging.getLogger(SERVICE)
    broadcaster = get_broadcaster()
    async for match in bus.consume(
        topic=Topics.CROSS_CAMERA_MATCHES,
        group_id=f"{cfg.consumer_group}-handoffs",
        schema=CrossCameraMatch,
    ):
        if stop.is_set():
            break
        try:
            await broadcaster.publish("handoff", match)
        except Exception:
            log.exception("failed to broadcast handoff",
                          extra={"global_track_id": match.global_track_id})


async def _gc_loop(tracks: InMemoryTrackStore, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            tracks.gc()


async def _wire_persistence(log: logging.Logger) -> tuple[AlertStore, object | None]:
    """Wire Postgres-backed stores if POSTGRES_HOST (or POSTGRES_URL) is set;
    otherwise fall back to the in-memory ring buffer. Production docker-compose
    sets these vars, so production boots Postgres-backed. Tests and ad-hoc runs
    without a DB stay on the in-memory path.
    """
    from confirmation.db import DatabaseConfig, Base, get_engine, get_sessionmaker

    cfg = DatabaseConfig.from_env(dict(os.environ))
    if cfg is None:
        log.warning("POSTGRES_HOST unset — confirmation running with "
                    "in-memory alert store; incidents will NOT survive restart")
        return RecentAlerts(), None

    engine = get_engine(cfg)
    # POC bootstrap — fine for Slice 2. Slice 3 swaps this for Alembic so
    # schema changes are versioned and reviewable.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = get_sessionmaker()

    from confirmation.db.cameras_repo import PostgresCameraRepo
    from confirmation.db.incidents_repo import PostgresIncidentsRepo

    log.info("Postgres-backed incident store online",
             extra={"dsn": cfg.url.split("@")[-1]})  # host/db only, no creds
    return PostgresIncidentsRepo(sm), PostgresCameraRepo(sm)


async def run() -> None:
    cfg = ConfirmationConfig()  # type: ignore[call-arg]
    log = configure_logging(SERVICE)
    log.info("starting confirmation",
             extra={"autoconfirm": cfg.autoconfirm_threshold,
                    "review": cfg.operator_review_threshold})

    bus = EventBus(SERVICE)
    await bus.start()
    set_bus(bus)

    tracks = InMemoryTrackStore(cfg.track_window_seconds)
    alerts, cameras_repo = await _wire_persistence(log)
    identity_view = GlobalIdentityView(recency_seconds=cfg.identity_recency_seconds)
    pipeline = ConfirmationPipeline(cfg, bus, tracks, alerts, identity_view=identity_view)

    app = build_app(alerts, cameras_repo=cameras_repo)
    uv_config = uvicorn.Config(
        app, host="0.0.0.0", port=cfg.http_port, log_config=None, access_log=False
    )
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    consumer_task = asyncio.create_task(_consume_loop(bus, pipeline, cfg, stop), name="consume")
    track_task = asyncio.create_task(
        _track_update_loop(bus, identity_view, cfg, stop), name="track-updates"
    )
    handoff_task = asyncio.create_task(
        _handoff_broadcast_loop(bus, cfg, stop), name="handoff-broadcast"
    )
    gc_task = asyncio.create_task(_gc_loop(tracks, stop), name="track-gc")
    server_task = asyncio.create_task(server.serve(), name="http")

    # When stop fires, ask uvicorn to exit and wait for the consume loop to drain.
    await stop.wait()
    log.info("shutdown signal received; draining")
    server.should_exit = True
    await asyncio.gather(
        server_task, consumer_task, track_task, handoff_task, gc_task,
        return_exceptions=True,
    )
    await bus.stop()
    # Dispose the DB pool if one was opened. No-op when we fell back to
    # in-memory, so this is safe to call unconditionally.
    from confirmation.db import shutdown_engine
    await shutdown_engine()
    log.info("confirmation stopped")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
