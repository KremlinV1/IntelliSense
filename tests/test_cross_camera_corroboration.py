"""Slice 2 — cross-camera corroboration signal.

Pins down three behaviours:
  1. When the identity view has seen the target on multiple cameras, the
     corroboration signal reflects that count (saturating at target).
  2. When the view hasn't seen the detection yet (race), scoring falls
     back to the legacy frame-count heuristic without crashing.
  3. The corroboration signal does NOT silently combine both sources.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pydantic import BaseModel

from veilwatch_contracts import (
    BoundingBox,
    CameraRef,
    Detection,
    DetectionEvent,
)
from veilwatch_contracts.events import AuditAction, ThreatClass

from confirmation.alert_store import RecentAlerts
from confirmation.config import ConfirmationConfig
from confirmation.identity_view import GlobalIdentityView
from confirmation.pipeline import ConfirmationPipeline
from confirmation.scoring import signal_corroboration
from confirmation.track_store import InMemoryTrackStore


class FakeBus:
    def __init__(self) -> None:
        self.calls: list = []
        self.service_name = "confirmation"

    async def publish(self, topic, event: BaseModel, *, key=None):
        self.calls.append((topic, event))

    async def audit(self, **kw):
        self.calls.append(("audit", kw))


# ---------------------------- signal_corroboration ---------------------------

class TestSignalCorroboration:
    def test_legacy_frame_count_used_when_no_cross_camera_info(self):
        # Mimic an 8-frame track on one camera.
        class T:
            detection_count = 8
        assert signal_corroboration(T()) == 1.0  # saturates at 8 frames

    def test_cross_camera_count_overrides_frame_count(self):
        """A single camera with 8 frames AND a cross-camera count of 1 should
        still score 1/target_cameras, NOT 1.0. This is the design decision
        that the cross-camera signal is the ground truth when available."""
        class T:
            detection_count = 8
        v = signal_corroboration(T(), distinct_cameras=1, target_cameras=3)
        # 1/3, not the 8/8 the legacy path would give.
        assert 0.33 < v < 0.34

    def test_cross_camera_saturates_at_target(self):
        assert signal_corroboration(None, distinct_cameras=3, target_cameras=3) == 1.0
        assert signal_corroboration(None, distinct_cameras=10, target_cameras=3) == 1.0

    def test_zero_cameras_is_zero(self):
        assert signal_corroboration(None, distinct_cameras=0, target_cameras=3) == 0.0


# ---------------------- Pipeline integration --------------------------------

def _det_event(camera_id: str, detection_id: str = "d1") -> DetectionEvent:
    cam = CameraRef(camera_id=camera_id, latitude=0.0, longitude=0.0)
    person = Detection(
        detection_id=detection_id,
        class_name="person",
        threat_class=ThreatClass.PERSON,
        confidence=0.85,
        bbox=BoundingBox(x1=0, y1=0, x2=50, y2=100),
        track_id="local-t1",
    )
    return DetectionEvent(
        event_id="e",
        camera=cam,
        frame_id=f"f-{detection_id}",
        frame_uri="file:///tmp/f.jpg",
        frame_sha256="a" * 64,
        detections=(person,),
        emitted_at=datetime.now(tz=timezone.utc),
    )


def _pipeline(identity_view: GlobalIdentityView | None):
    cfg = ConfirmationConfig(
        CONFIRMATION_AUTOCONFIRM_THRESHOLD=0.85,
        CONFIRMATION_OPERATOR_REVIEW_THRESHOLD=0.55,
        CONFIRMATION_TRACK_WINDOW_SECONDS=30,
        CONFIRMATION_CORROBORATION_TARGET_CAMERAS=3,
        CONFIRMATION_IDENTITY_RECENCY_SECONDS=300.0,
        CONFIRMATION_HTTP_PORT=8080,
        REDIS_URL="redis://localhost:6379/0",
    )  # type: ignore[call-arg]
    bus = FakeBus()
    pipe = ConfirmationPipeline(
        cfg, bus, InMemoryTrackStore(30), RecentAlerts(),
        identity_view=identity_view,
    )
    return bus, pipe


def _last_scored_signals(bus: FakeBus) -> dict:
    scored = [c for c in bus.calls
              if c[0] == "audit" and c[1].get("action") == AuditAction.ALERT_SCORED]
    assert scored, "no ALERT_SCORED audit recorded"
    return scored[-1][1]["payload"]


class TestPipelineUsesCrossCamera:
    def test_identity_on_three_cameras_saturates_corroboration(self):
        """The Phase 2 headline behaviour: the same global identity observed
        on 3 distinct cameras maxes out the corroboration signal, driving the
        fused score up even without a watchlist hit."""
        view = GlobalIdentityView(recency_seconds=300.0)
        # Tracker has seen the global identity 'g-1' on three cameras. The
        # detection_id in the DetectionEvent we'll score is 'd-current'.
        view.observe("g-1", camera_id="CAM-A", detection_id="d-current", now_ts=1000.0)
        view.observe("g-1", camera_id="CAM-B", detection_id="d-old-b", now_ts=1001.0)
        view.observe("g-1", camera_id="CAM-C", detection_id="d-old-c", now_ts=1002.0)

        bus, pipe = _pipeline(view)
        asyncio.run(pipe.handle(_det_event("CAM-A", detection_id="d-current")))

        sigs = _last_scored_signals(bus)
        assert sigs["signals"]["corroboration"] == 1.0
        assert sigs["distinct_cameras"] == 3
        assert sigs["global_track_id"] == "g-1"

    def test_identity_on_one_camera_gives_partial_corroboration(self):
        view = GlobalIdentityView(recency_seconds=300.0)
        view.observe("g-1", camera_id="CAM-A", detection_id="d-current", now_ts=1000.0)
        bus, pipe = _pipeline(view)
        asyncio.run(pipe.handle(_det_event("CAM-A", detection_id="d-current")))
        sigs = _last_scored_signals(bus)
        # 1 camera / target 3
        assert 0.33 < sigs["signals"]["corroboration"] < 0.34
        assert sigs["distinct_cameras"] == 1

    def test_unresolved_detection_falls_back_to_legacy(self):
        """The race case: TrackUpdate hasn't arrived yet. detection_id can't
        be resolved. Pipeline must NOT crash; it scores with frame-count
        corroboration and omits global_track_id from the audit payload."""
        view = GlobalIdentityView(recency_seconds=300.0)
        bus, pipe = _pipeline(view)
        asyncio.run(pipe.handle(_det_event("CAM-A", detection_id="d-unknown")))
        sigs = _last_scored_signals(bus)
        assert "global_track_id" not in sigs
        assert "distinct_cameras" not in sigs
        # Legacy corroboration: 1 frame / 8 → 0.125
        assert sigs["signals"]["corroboration"] == 0.125

    def test_no_identity_view_still_scores(self):
        """Backwards compatibility: a pipeline constructed without an
        identity_view (pre-Slice-2 tests, Slice-1 deployments) still works."""
        bus, pipe = _pipeline(identity_view=None)
        asyncio.run(pipe.handle(_det_event("CAM-A", detection_id="d-any")))
        sigs = _last_scored_signals(bus)
        assert "global_track_id" not in sigs


# ---------------------- GlobalIdentityView unit tests ------------------------

class TestGlobalIdentityView:
    def test_tracks_distinct_cameras(self):
        view = GlobalIdentityView(recency_seconds=300.0)
        view.observe("g", camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        view.observe("g", camera_id="CAM-B", detection_id="d2", now_ts=1001.0)
        view.observe("g", camera_id="CAM-A", detection_id="d3", now_ts=1002.0)
        track = view.resolve("d3")
        assert track is not None
        assert track.distinct_camera_count() == 2  # A + B, not 3
        assert track.num_sightings == 3

    def test_stale_identities_are_evicted(self):
        view = GlobalIdentityView(recency_seconds=60.0)
        view.observe("g", camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        # 120s later, add a new observation that triggers GC.
        view.observe("g2", camera_id="CAM-B", detection_id="d2", now_ts=1120.0)
        assert view.resolve("d1") is None  # evicted
        assert view.resolve("d2") is not None

    def test_detection_ring_is_capped(self):
        """The detection_id→gtid map must not grow unbounded."""
        view = GlobalIdentityView(recency_seconds=3600.0, detection_index_size=3)
        for i in range(5):
            view.observe(f"g-{i}", camera_id="CAM-A",
                         detection_id=f"d-{i}", now_ts=1000.0 + i)
        # Only the last 3 detection_ids are resolvable.
        assert view.resolve("d-0") is None
        assert view.resolve("d-1") is None
        assert view.resolve("d-4") is not None

    def test_resolve_returns_snapshot_not_live_reference(self):
        """Caller mutation must not corrupt internal state."""
        view = GlobalIdentityView(recency_seconds=300.0)
        view.observe("g", camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        snap = view.resolve("d1")
        assert snap is not None
        snap.cameras.add("CAM-HACK")
        snap2 = view.resolve("d1")
        assert snap2 is not None
        assert "CAM-HACK" not in snap2.cameras
