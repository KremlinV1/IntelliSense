"""Capture service entrypoint.

Pipeline:
    RTSP → decode (OpenCV) → JPEG encode → SHA-256 → local blob store → Kafka

Back-pressure: if Kafka is slow, aiokafka's internal queue applies pressure. We
intentionally do NOT buffer frames in-process — dropping a frame is strictly
preferable to serving a 10-second-old frame to the detector.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2

from veilwatch_contracts import (
    CameraRef,
    FrameEvent,
    Topics,
)
from veilwatch_contracts.bus import EventBus
from veilwatch_contracts.events import AuditAction
from veilwatch_contracts.obs import configure_logging, set_correlation

from capture.config import CaptureConfig
from capture.frame_store import build_frame_store, sha256_hex
from capture.rtsp_reader import RtspReader

SERVICE = "capture"


def _encode_jpeg(image, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


async def run() -> None:
    cfg = CaptureConfig()  # type: ignore[call-arg]
    log = configure_logging(SERVICE)
    log.info("starting capture", extra={"camera_id": cfg.camera_id, "rtsp": cfg.rtsp_url})

    camera_ref = CameraRef(
        camera_id=cfg.camera_id,
        latitude=cfg.camera_lat,
        longitude=cfg.camera_lon,
        environment=cfg.camera_environment,  # type: ignore[arg-type]
    )
    store = build_frame_store(cfg.frame_store_backend, cfg.frame_store_local_path)
    reader = RtspReader(
        url=cfg.rtsp_url,
        target_fps=cfg.target_fps,
        reconnect_min_s=cfg.reconnect_min_s,
        reconnect_max_s=cfg.reconnect_max_s,
    )
    bus = EventBus(SERVICE)
    await bus.start()

    # JPEG encode + fsync are sync CPU/IO — push to a pool, don't block the loop.
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture-io")
    loop = asyncio.get_running_loop()

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async for raw in reader.frames():
            if stop.is_set():
                break
            frame_id = str(uuid.uuid4())
            set_correlation(camera_id=cfg.camera_id, frame_id=frame_id)

            try:
                jpeg = await loop.run_in_executor(
                    pool, _encode_jpeg, raw.image, cfg.jpeg_quality
                )
                frame_sha = sha256_hex(jpeg)
                frame_uri = await loop.run_in_executor(pool, store.put, frame_id, jpeg)
            except Exception:
                logging.getLogger(SERVICE).exception("frame persist failed; skipping")
                continue

            event = FrameEvent(
                event_id=str(uuid.uuid4()),
                camera=camera_ref,
                frame_id=frame_id,
                frame_uri=frame_uri,
                frame_sha256=frame_sha,
                width=raw.width,
                height=raw.height,
                captured_at=raw.captured_at,
            )

            # Audit BEFORE publishing the frame event — the audit record is the
            # canonical "this frame existed" artefact for chain-of-custody.
            await bus.audit(
                action=AuditAction.FRAME_CAPTURED,
                subject_id=frame_id,
                correlation_ids={"camera_id": cfg.camera_id},
                payload={"frame_sha256": frame_sha, "frame_uri": frame_uri},
            )
            await bus.publish(Topics.CAMERA_FRAMES, event, key=cfg.camera_id)
    finally:
        await bus.stop()
        pool.shutdown(wait=False, cancel_futures=True)
        log.info("capture stopped")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
