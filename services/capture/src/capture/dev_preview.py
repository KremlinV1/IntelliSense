"""Multi-camera RTSP snapshot runner for dev.

Reads the camera registry from the confirmation service's internal endpoint,
opens an RTSP session to each active camera with an rtsp_url, and writes a
single `latest.jpg` per camera every few seconds. The confirmation API serves
those files back at /cameras/{id}/preview.jpg so the dashboard can show live
thumbnails.

This is explicitly NOT the production capture path — production uses one pod
per camera (services/capture/src/capture/main.py), publishes FrameEvents to
Kafka, and writes full frame history to S3/MinIO with WORM retention.

Dev shortcuts taken:
  - No Kafka publish. Frames are dropped after the JPEG write.
  - No chain-of-custody audit. The ~4-line audit trail in main.py is the
    thing you must not skip in prod; here we're just previewing pixels.
  - Naive supervision: one task per camera, restart on exception after backoff.
  - Poll-based reconciliation (every 30s) rather than DB NOTIFY / WebSocket.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import httpx

SERVICE = "capture-dev-preview"

API_URL = os.getenv("CONFIRMATION_API", "http://127.0.0.1:8080").rstrip("/")
PREVIEW_DIR = Path(os.getenv("PREVIEW_DIR", "./frames-dev")).resolve()
INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "").strip()

RECONCILE_INTERVAL_S = float(os.getenv("PREVIEW_RECONCILE_S", "30"))
SNAPSHOT_INTERVAL_S = float(os.getenv("PREVIEW_SNAPSHOT_S", "2"))
OPEN_TIMEOUT_S = float(os.getenv("PREVIEW_OPEN_TIMEOUT_S", "15"))
JPEG_QUALITY = int(os.getenv("PREVIEW_JPEG_QUALITY", "75"))


@dataclass
class CameraSource:
    camera_id: str
    rtsp_url: str
    label: str | None

    def key(self) -> tuple[str, str]:
        """Identity for the reconciler. If rtsp_url changes on an existing
        camera, the old task is stopped and a new one opens the new URL."""
        return (self.camera_id, self.rtsp_url)


async def fetch_sources(client: httpx.AsyncClient) -> list[CameraSource]:
    """Pull the registry via the internal sources endpoint. 503 = no Postgres
    (expected in plain dev mode); propagate as empty list so the runner
    idles until someone wires the DB."""
    headers = {}
    if INTERNAL_TOKEN:
        headers["Authorization"] = f"Bearer {INTERNAL_TOKEN}"
    r = await client.get(
        f"{API_URL}/internal/cameras/sources",
        params={"active_only": "true"},
        headers=headers,
        timeout=5.0,
    )
    if r.status_code == 503:
        return []
    r.raise_for_status()
    rows = r.json()
    out: list[CameraSource] = []
    for row in rows:
        url = (row.get("rtsp_url") or "").strip()
        if not url:
            continue
        out.append(
            CameraSource(
                camera_id=row["camera_id"],
                rtsp_url=url,
                label=row.get("label"),
            )
        )
    return out


def _open_capture(url: str) -> cv2.VideoCapture | None:
    """Open an RTSP session with a bounded open timeout.

    OpenCV's VideoCapture.open() can hang for minutes on an unreachable host.
    We set the OPENCV_FFMPEG_CAPTURE_OPTIONS env var on process start to cap
    the ffmpeg-side timeout; if the connection still fails, isOpened() returns
    False and we bail.
    """
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return None
    # Force TCP transport where possible for stability over flaky networks.
    # OpenCV honors this via the env var set at process start.
    return cap


async def _camera_loop(src: CameraSource, stop: asyncio.Event) -> None:
    """Per-camera task: keep the stream open, write latest.jpg every N sec."""
    log = logging.getLogger(f"{SERVICE}.{src.camera_id}")
    out_dir = PREVIEW_DIR / src.camera_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "latest.jpg"
    tmp_path = out_dir / "latest.jpg.tmp"

    loop = asyncio.get_running_loop()
    backoff = 1.0

    while not stop.is_set():
        log.info("connecting", extra={"rtsp": src.rtsp_url})
        cap = await loop.run_in_executor(None, _open_capture, src.rtsp_url)
        if cap is None:
            log.warning("connect failed; backing off", extra={"backoff_s": backoff})
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
                return  # stop signalled
            except asyncio.TimeoutError:
                pass
            backoff = min(30.0, backoff * 2)
            continue

        log.info("connected")
        backoff = 1.0

        try:
            while not stop.is_set():
                ok, frame = await loop.run_in_executor(None, cap.read)
                if not ok or frame is None:
                    log.warning("read failed; will reconnect")
                    break
                ok, buf = cv2.imencode(
                    ".jpg", frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
                )
                if not ok:
                    continue
                # atomic write: tmp + rename so /preview.jpg never serves a
                # half-written JPEG
                tmp_path.write_bytes(buf.tobytes())
                tmp_path.replace(out_path)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=SNAPSHOT_INTERVAL_S)
                    return  # stop signalled during sleep
                except asyncio.TimeoutError:
                    pass
        finally:
            await loop.run_in_executor(None, cap.release)


async def _supervisor(stop: asyncio.Event) -> None:
    """Reconcile desired cameras (registry) with running tasks every N sec.

    Adds tasks for new cameras, cancels tasks for removed/changed cameras.
    Each task restarts itself on transient errors; the supervisor just
    restarts the task wholesale if it crashes out of its own retry loop."""
    log = logging.getLogger(SERVICE)
    tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                desired = await fetch_sources(client)
            except Exception as exc:
                log.warning("failed to fetch camera registry: %s", exc)
                desired = []

            desired_keys = {c.key() for c in desired}

            # Cancel tasks whose camera is gone or whose URL changed.
            for key in list(tasks.keys()):
                if key not in desired_keys or tasks[key].done():
                    t = tasks.pop(key)
                    if not t.done():
                        t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            # Start tasks for new cameras.
            for cam in desired:
                if cam.key() not in tasks:
                    log.info("starting preview task",
                             extra={"camera_id": cam.camera_id})
                    tasks[cam.key()] = asyncio.create_task(
                        _camera_loop(cam, stop),
                        name=f"preview-{cam.camera_id}",
                    )

            try:
                await asyncio.wait_for(stop.wait(), timeout=RECONCILE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

        # Stop requested — cancel everything.
        for t in tasks.values():
            t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def run() -> None:
    # Cap ffmpeg timeouts so an unreachable RTSP host doesn't hang our tasks
    # for minutes. 8s is enough for a healthy stream on the public internet.
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        "rtsp_transport;tcp|stimeout;8000000",  # microseconds
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger(SERVICE)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    log.info("preview runner starting",
             extra={"api": API_URL, "preview_dir": str(PREVIEW_DIR)})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await _supervisor(stop)
    finally:
        log.info("preview runner stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
