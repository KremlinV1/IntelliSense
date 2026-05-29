"""Confirmation-side read model of the tracker's global identity state.

This is NOT a copy of the tracker's IdentityBank. The tracker owns identity
creation and centroid fusion; this module just observes the resulting
TrackUpdate stream and builds a lookup table confirmation uses to score.

Two maps are maintained:
  - `gtid -> CrossCameraTrack`:  set of cameras + timestamps + sightings
  - `detection_id -> gtid`:      short ring buffer so a newly-arriving
                                 DetectionEvent can resolve its primary
                                 detection to a global identity

Race tolerance: if a DetectionEvent is scored before its TrackUpdate has been
processed, the detection_id lookup returns None and the scorer falls back to
the legacy per-camera corroboration heuristic. The next frame for the same
track scores correctly — this is the eventually-consistent path we've
deliberately chosen over buffering with a deadline.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class CrossCameraTrack:
    global_track_id: str
    cameras: set[str] = field(default_factory=set)
    first_seen_ts: float = 0.0
    last_seen_ts: float = 0.0
    num_sightings: int = 0

    def distinct_camera_count(self) -> int:
        return len(self.cameras)


class GlobalIdentityView:
    def __init__(self, recency_seconds: float, detection_index_size: int = 10_000) -> None:
        self._recency = recency_seconds
        self._tracks: dict[str, CrossCameraTrack] = {}
        # Insertion-ordered ring so we can evict the oldest mappings when full.
        # An OrderedDict gives us both O(1) lookup and O(1) oldest-key removal.
        self._detection_to_gtid: OrderedDict[str, str] = OrderedDict()
        self._detection_index_cap = detection_index_size
        self._lock = Lock()

    # --- Write side (driven by tracks.updates.v1 consumer) ------------------

    def observe(
        self,
        global_track_id: str,
        camera_id: str,
        detection_id: str,
        now_ts: Optional[float] = None,
    ) -> None:
        if now_ts is None:
            now_ts = time.time()
        with self._lock:
            track = self._tracks.get(global_track_id)
            if track is None:
                track = CrossCameraTrack(
                    global_track_id=global_track_id,
                    first_seen_ts=now_ts,
                )
                self._tracks[global_track_id] = track
            track.cameras.add(camera_id)
            track.last_seen_ts = now_ts
            track.num_sightings += 1

            # Record the detection_id → gtid mapping, dropping the oldest if
            # we've exceeded the cap. `move_to_end(last=True)` isn't needed
            # because we always append fresh entries.
            self._detection_to_gtid[detection_id] = global_track_id
            if len(self._detection_to_gtid) > self._detection_index_cap:
                self._detection_to_gtid.popitem(last=False)

            self._evict_stale(now_ts)

    # --- Read side (driven by confirmation scorer) --------------------------

    def resolve(self, detection_id: str) -> Optional[CrossCameraTrack]:
        """Returns the CrossCameraTrack for the identity this detection was
        bound to by the tracker, or None if we haven't seen a TrackUpdate for
        this detection_id (either it hasn't arrived yet, or it fell out of
        the ring). The caller must handle None — typically by falling back to
        per-camera corroboration."""
        with self._lock:
            gtid = self._detection_to_gtid.get(detection_id)
            if gtid is None:
                return None
            track = self._tracks.get(gtid)
            if track is None:
                return None
            # Return a snapshot. Mutation-by-consumer would be a bug source.
            return CrossCameraTrack(
                global_track_id=track.global_track_id,
                cameras=set(track.cameras),
                first_seen_ts=track.first_seen_ts,
                last_seen_ts=track.last_seen_ts,
                num_sightings=track.num_sightings,
            )

    def size(self) -> int:
        with self._lock:
            return len(self._tracks)

    # --- Internals ----------------------------------------------------------

    def _evict_stale(self, now_ts: float) -> None:
        stale_gtids = [
            g for g, t in self._tracks.items()
            if (now_ts - t.last_seen_ts) > self._recency
        ]
        for g in stale_gtids:
            del self._tracks[g]
