"""RTSP frame reader.

Uses OpenCV's VideoCapture (which wraps FFmpeg under the hood in the
`opencv-python-headless` build). In production (Phase 2+) this is replaced by
a GStreamer pipeline with NVIDIA DeepStream for GPU decode. The public
interface — an iterator of (timestamp, ndarray) — stays the same so the
surrounding code doesn't change.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class RawFrame:
    captured_at: datetime
    image: np.ndarray  # BGR
    width: int
    height: int


class RtspReader:
    def __init__(
        self,
        url: str,
        target_fps: float,
        reconnect_min_s: float,
        reconnect_max_s: float,
    ) -> None:
        self._url = url
        self._frame_interval = 1.0 / max(target_fps, 0.1)
        self._reconnect_min = reconnect_min_s
        self._reconnect_max = reconnect_max_s

    async def frames(self) -> AsyncIterator[RawFrame]:
        """Yield decoded frames at approximately target_fps. Reconnects on
        stream loss with exponential backoff capped at `reconnect_max_s`."""
        backoff = self._reconnect_min
        loop = asyncio.get_running_loop()

        while True:
            cap = await loop.run_in_executor(None, cv2.VideoCapture, self._url)
            if not cap.isOpened():
                log.warning("rtsp open failed; backing off",
                            extra={"backoff_s": backoff, "url": self._url})
                await asyncio.sleep(backoff)
                backoff = min(self._reconnect_max, backoff * 2)
                continue

            log.info("rtsp connected", extra={"url": self._url})
            backoff = self._reconnect_min
            last_emit = 0.0
            try:
                while True:
                    ok, frame = await loop.run_in_executor(None, cap.read)
                    if not ok or frame is None:
                        log.warning("rtsp read failed; will reconnect",
                                    extra={"url": self._url})
                        break
                    now = time.monotonic()
                    # Downsample to target FPS at read time — we don't want to
                    # decode the full stream rate if we only forward 5 fps.
                    if now - last_emit < self._frame_interval:
                        continue
                    last_emit = now
                    h, w = frame.shape[:2]
                    yield RawFrame(
                        captured_at=datetime.now(tz=timezone.utc),
                        image=frame,
                        width=int(w),
                        height=int(h),
                    )
            finally:
                await loop.run_in_executor(None, cap.release)
            # loop back to reconnect
            await asyncio.sleep(backoff)
            backoff = min(self._reconnect_max, backoff * 2)
