"""Build the four scoring signals from a DetectionEvent + short-term track state.

These are deliberately simple heuristics for Phase 1. Phase 3 replaces
`behaviour` with a learned classifier over pose + motion + scene features; it
replaces `corroboration` with cross-camera re-ID evidence from OSNet.
"""
from __future__ import annotations

from dataclasses import dataclass

from veilwatch_contracts.events import DetectionEvent, ThreatClass


@dataclass
class TrackState:
    """Running window of detections for a (camera, track_id) pair. In-memory
    for Phase 1; Redis-backed in Phase 2 when we scale past one instance."""
    first_seen_ts: float
    last_seen_ts: float
    detection_count: int
    max_confidence: float


def signal_watchlist_hit(event: DetectionEvent) -> float:
    """Highest watchlist similarity across all hits in this frame."""
    if not event.watchlist_hits:
        return 0.0
    return max(h.similarity for h in event.watchlist_hits)


def signal_behaviour(event: DetectionEvent) -> float:
    """Phase 1 heuristic: a weapon detection contributes 1.0; otherwise we use
    the max non-person detection confidence bounded by 0.6 so a confident
    'car' detection can't trigger an alert on its own. A real behaviour
    classifier replaces this in Phase 3."""
    weapon = max(
        (d.confidence for d in event.detections if d.threat_class == ThreatClass.WEAPON),
        default=0.0,
    )
    if weapon > 0:
        return weapon
    non_person = max(
        (d.confidence for d in event.detections if d.threat_class != ThreatClass.PERSON),
        default=0.0,
    )
    return min(non_person, 0.6)


def signal_persistence(track: TrackState | None, now_ts: float, window_s: int) -> float:
    """How long has this subject been visible, normalised to the track window.
    A target seen for >= the full window scores 1.0."""
    if track is None:
        return 0.0
    elapsed = max(0.0, now_ts - track.first_seen_ts)
    return min(1.0, elapsed / max(window_s, 1))


def signal_corroboration(
    track: TrackState | None,
    distinct_cameras: int | None = None,
    target_cameras: int = 3,
) -> float:
    """Corroboration evidence strength.

    Phase 2 preferred path: `distinct_cameras` is the number of separate
    cameras the tracker has seen this identity on within the recency window.
    Saturates at `target_cameras`. An identity on 3+ cameras within 5 minutes
    is near-impossible noise.

    Phase 1 fallback: if `distinct_cameras` is None (no TrackUpdate resolved
    yet for this detection, typically because of the tracker → confirmation
    race on the very first frame of a track), fall back to the legacy
    frame-count heuristic.

    The two are NOT combined because they measure different things, and
    averaging them would dilute the cross-camera signal (which is the whole
    point of Phase 2). We prefer the stronger signal when available.
    """
    if distinct_cameras is not None:
        return min(1.0, distinct_cameras / max(target_cameras, 1))
    if track is None:
        return 0.0
    return min(1.0, track.detection_count / 8.0)
