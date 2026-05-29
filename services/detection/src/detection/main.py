"""Detection service entrypoint.

Consumes FrameEvents, runs YOLOv8 + FAISS face watchlist + ALPR (on vehicle
crops) + plate watchlist — in parallel per frame — and emits DetectionEvents.

Concurrency model:
  - One aiokafka consumer loop (single-threaded, ordered per camera partition).
  - Within each frame: YOLO runs first (must — we need crops). Then face
    matching on person crops and ALPR on vehicle crops dispatch in parallel
    via asyncio.gather over a thread pool.
  - Frame-level batching across cameras is deferred to Phase 2 / DeepStream.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor

from veilwatch_contracts import (
    BoundingBox,
    Detection,
    DetectionEvent,
    FrameEvent,
    PlateRead,
    Topics,
    WatchlistHit,
)
from veilwatch_contracts.bus import EventBus
from veilwatch_contracts.events import AuditAction, ThreatClass
from veilwatch_contracts.obs import configure_logging, set_correlation

from detection.alpr import AlprEngine, build_backend
from detection.config import DetectionConfig
from detection.frame_loader import load_frame
from detection.reid import ReidEngine, build_reid_backend
from detection.watchlist import PlateWatchlist, Watchlist
from detection.yolo_engine import YoloEngine, crop

SERVICE = "detection"


async def _match_face(
    det_id: str,
    image_crop,
    watchlist: Watchlist,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
) -> WatchlistHit | None:
    match = await loop.run_in_executor(pool, watchlist.match, image_crop)
    if match is None:
        return None
    return WatchlistHit(
        detection_id=det_id,
        identity_id=match.identity.identity_id,
        identity_label=match.identity.label,
        similarity=match.similarity,
        source="face",
    )


async def _embed_person(
    det_id: str,
    image_crop,
    reid: ReidEngine,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
) -> tuple[str, tuple[float, ...] | None]:
    """Returns (detection_id, embedding) so the caller can stitch results
    back to the right detection regardless of gather() ordering."""
    emb = await loop.run_in_executor(pool, reid.embed, image_crop)
    return det_id, emb


async def _read_plate(
    det_id: str,
    image_crop,
    alpr: AlprEngine,
    plate_watchlist: PlateWatchlist,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
) -> tuple[PlateRead | None, WatchlistHit | None]:
    result = await loop.run_in_executor(pool, alpr.read_best, image_crop)
    if result is None:
        return None, None
    plate = PlateRead(
        detection_id=det_id,
        plate_text=result.plate_text,
        raw_text=result.raw_text,
        confidence=result.confidence,
        region=result.region,
    )
    match = plate_watchlist.match(result.plate_text)
    if match is None:
        return plate, None
    # Plate match similarity is 1.0 by construction (exact match). The ALPR
    # engine's own confidence is captured on the PlateRead — keeping the two
    # signals separate is intentional: operators need to see both the
    # "is this plate text reliable?" and "does this plate text match the
    # watchlist?" answers independently.
    hit = WatchlistHit(
        detection_id=det_id,
        identity_id=match.identity.identity_id,
        identity_label=match.identity.label,
        similarity=match.similarity,
        source="plate",
    )
    return plate, hit


async def _handle_frame(
    event: FrameEvent,
    yolo: YoloEngine,
    watchlist: Watchlist,
    alpr: AlprEngine,
    plate_watchlist: PlateWatchlist,
    reid: ReidEngine,
    bus: EventBus,
    pool: ThreadPoolExecutor,
) -> None:
    log = logging.getLogger(SERVICE)
    set_correlation(camera_id=event.camera.camera_id, frame_id=event.frame_id)

    loop = asyncio.get_running_loop()
    try:
        image = await loop.run_in_executor(pool, load_frame, event.frame_uri)
    except Exception:
        log.exception("frame load failed", extra={"frame_uri": event.frame_uri})
        return

    raws = await loop.run_in_executor(pool, yolo.infer, image)

    # Build a provisional Detection list keyed by det_id; we'll rewrite entries
    # with embeddings once the re-ID tasks return. Detections are kept in a
    # dict indexed by det_id so gather() completion order doesn't matter.
    det_index: dict[str, Detection] = {}
    det_order: list[str] = []
    face_tasks: list = []
    plate_tasks: list = []
    reid_tasks: list = []

    for raw in raws:
        det_id = str(uuid.uuid4())
        det_order.append(det_id)
        det_index[det_id] = Detection(
            detection_id=det_id,
            class_name=raw.class_name,
            threat_class=raw.threat_class,
            confidence=raw.confidence,
            bbox=BoundingBox(x1=raw.xyxy[0], y1=raw.xyxy[1], x2=raw.xyxy[2], y2=raw.xyxy[3]),
        )
        if raw.threat_class == ThreatClass.PERSON:
            person_crop = crop(image, raw.xyxy)
            face_tasks.append(_match_face(det_id, person_crop, watchlist, loop, pool))
            reid_tasks.append(_embed_person(det_id, person_crop, reid, loop, pool))
        elif raw.threat_class == ThreatClass.VEHICLE:
            vehicle_crop = crop(image, raw.xyxy)
            plate_tasks.append(_read_plate(det_id, vehicle_crop, alpr, plate_watchlist, loop, pool))

    # All three task groups (face match, plate read, re-ID embed) are
    # independent — dispatch concurrently. Re-ID is the cheapest of the three
    # (pure matrix op, no I/O) so it rarely dominates wall time.
    face_results, plate_results, reid_results = await asyncio.gather(
        asyncio.gather(*face_tasks, return_exceptions=True) if face_tasks else _empty(),
        asyncio.gather(*plate_tasks, return_exceptions=True) if plate_tasks else _empty(),
        asyncio.gather(*reid_tasks, return_exceptions=True) if reid_tasks else _empty(),
    )

    hits: list[WatchlistHit] = []
    for r in face_results:
        if isinstance(r, WatchlistHit):
            hits.append(r)
        elif isinstance(r, BaseException):
            log.exception("face match task failed", exc_info=r)

    plate_reads: list[PlateRead] = []
    for r in plate_results:
        if isinstance(r, BaseException):
            log.exception("plate read task failed", exc_info=r)
            continue
        plate, plate_hit = r
        if plate is not None:
            plate_reads.append(plate)
        if plate_hit is not None:
            hits.append(plate_hit)

    # Stitch embeddings back onto the right Detection entries.
    for r in reid_results:
        if isinstance(r, BaseException):
            log.exception("re-id task failed", exc_info=r)
            continue
        det_id, emb = r
        if emb is None:
            continue
        det_index[det_id] = det_index[det_id].model_copy(update={"embedding": emb})

    detections = [det_index[d] for d in det_order]

    det_event = DetectionEvent(
        event_id=str(uuid.uuid4()),
        camera=event.camera,
        frame_id=event.frame_id,
        frame_uri=event.frame_uri,
        frame_sha256=event.frame_sha256,
        detections=tuple(detections),
        watchlist_hits=tuple(hits),
        plate_reads=tuple(plate_reads),
    )

    await bus.audit(
        action=AuditAction.DETECTION_EMITTED,
        subject_id=event.frame_id,
        correlation_ids={"camera_id": event.camera.camera_id},
        payload={
            "num_detections": len(detections),
            "num_watchlist_hits": len(hits),
            "num_plate_reads": len(plate_reads),
            "num_embeddings": sum(1 for d in detections if d.embedding is not None),
        },
    )
    await bus.publish(Topics.DETECTION_EVENTS, det_event, key=event.camera.camera_id)


async def _empty() -> list:
    """Placeholder awaitable for an empty task group — keeps the gather()
    call symmetrical whether there are face/plate tasks or not."""
    return []


async def run() -> None:
    cfg = DetectionConfig()  # type: ignore[call-arg]
    log = configure_logging(SERVICE)
    log.info("starting detection",
             extra={"weights": cfg.yolo_weights, "device": cfg.yolo_device})

    yolo = YoloEngine(cfg.yolo_weights, cfg.yolo_device, cfg.yolo_conf_threshold)
    watchlist = Watchlist(cfg.watchlist_match_threshold)
    watchlist.load_seed(cfg.watchlist_seed_path)

    alpr_backend = build_backend(cfg.alpr_backend)
    alpr = AlprEngine(alpr_backend, cfg.alpr_min_confidence)
    plate_watchlist = PlateWatchlist()
    plate_watchlist.load_seed(cfg.watchlist_seed_path)

    reid_backend = build_reid_backend(cfg.reid_backend, cfg.reid_onnx_model_path)
    reid = ReidEngine(reid_backend)

    bus = EventBus(SERVICE)
    await bus.start()

    # 4 workers: YOLO + face-embed + 2 concurrent ALPR reads per frame.
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="detection-infer")
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async for event in bus.consume(
            topic=Topics.CAMERA_FRAMES,
            group_id=cfg.consumer_group,
            schema=FrameEvent,
            dlq_topic=Topics.DLQ_CAMERA_FRAMES,
        ):
            if stop.is_set():
                break
            try:
                await _handle_frame(event, yolo, watchlist, alpr, plate_watchlist, reid, bus, pool)
            except Exception:
                log.exception("detection handler failed",
                              extra={"frame_id": event.frame_id})
    finally:
        await bus.stop()
        pool.shutdown(wait=False, cancel_futures=True)
        log.info("detection stopped")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
