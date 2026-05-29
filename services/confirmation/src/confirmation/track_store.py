"""Short-term track state for the persistence + corroboration signals.

Phase 1: in-memory dict with periodic GC. Good enough for a single-instance
confirmation service at POC throughput.
Phase 2: replace with Redis (key = {camera_id}:{track_id}, TTL = window).
"""
from __future__ import annotations

import time

from confirmation.scoring import TrackState


class InMemoryTrackStore:
    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._tracks: dict[str, TrackState] = {}

    @staticmethod
    def _key(camera_id: str, track_id: str) -> str:
        return f"{camera_id}:{track_id}"

    def observe(self, camera_id: str, track_id: str, confidence: float) -> TrackState:
        key = self._key(camera_id, track_id)
        now = time.time()
        track = self._tracks.get(key)
        if track is None or (now - track.last_seen_ts) > self._window:
            track = TrackState(
                first_seen_ts=now,
                last_seen_ts=now,
                detection_count=1,
                max_confidence=confidence,
            )
        else:
            track = TrackState(
                first_seen_ts=track.first_seen_ts,
                last_seen_ts=now,
                detection_count=track.detection_count + 1,
                max_confidence=max(track.max_confidence, confidence),
            )
        self._tracks[key] = track
        return track

    def gc(self) -> int:
        """Drop tracks older than the window. Returns number of tracks evicted."""
        now = time.time()
        stale = [k for k, t in self._tracks.items() if now - t.last_seen_ts > self._window]
        for k in stale:
            del self._tracks[k]
        return len(stale)
