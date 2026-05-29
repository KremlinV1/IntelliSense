"""Phase 2 end-to-end: detection -> tracker -> confirmation, in-process.

This test drives the REAL tracker handler and the REAL confirmation pipeline
through a shared in-memory bus, using the production IdentityBank and
GlobalIdentityView classes. No Kafka. No Redis (unless explicitly chosen).
No Docker. Runs in <1s, but proves the Phase 2 milestone end-to-end:

    A person observed on CAM-A then CAM-B within the tracking window is
    re-identified by the tracker under the SAME global_track_id, and the
    subsequent DetectionEvent on CAM-B scores with cross-camera
    corroboration reflected in the fused threat score.

Why this > a Docker smoke run at this stage:

  - Docker with mock capture + mock re-ID only proves containers boot and
    connect to Kafka. It does NOT exercise the identity bank matching
    geometry, the scoring integration, or the consumer race behaviour.
  - This test exercises all three in the same wall-clock second.

What this test does NOT cover (deliberately):

  - Kafka partition assignment, consumer-group rebalancing, DLQ replay.
    Those are transport-level concerns; tested separately against a real
    broker in CI.
  - HTTP API + WebSocket fan-out. Those are tested in test_live_map and
    the (separate) API tests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import numpy as np
from pydantic import BaseModel

from veilwatch_contracts import (
    BoundingBox,
    CameraRef,
    Detection,
    DetectionEvent,
    Topics,
    TrackUpdate,
    WatchlistHit,
)
from veilwatch_contracts.events import AlertDisposition, AuditAction, ThreatClass

from confirmation.alert_store import RecentAlerts
from confirmation.config import ConfirmationConfig
from confirmation.identity_view import GlobalIdentityView
from confirmation.pipeline import ConfirmationPipeline
from confirmation.track_store import InMemoryTrackStore
from tracker.identity_bank import IdentityBank
from tracker.main import _handle_detection as tracker_handle_detection


# ---------------------------------------------------------------------------
# In-memory bus shared by tracker and confirmation. It satisfies the EventBus
# duck-type both services use: `publish(topic, event)`, `audit(**kwargs)`.
# Detection events go into a queue so callers can choose when to drain them.
# ---------------------------------------------------------------------------

class InMemoryBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []
        self.audits: list[dict] = []
        self.service_name = "test"

    async def publish(self, topic: str, event: BaseModel, *, key: str | None = None) -> None:
        self.published.append((topic, event))

    async def audit(self, **kw) -> None:
        self.audits.append(kw)

    # Convenience filters.
    def track_updates(self) -> list[TrackUpdate]:
        return [e for t, e in self.published if t == Topics.TRACKS_UPDATES]

    def confirmed_alerts(self) -> list:
        return [e for t, e in self.published if t == Topics.ALERTS_CONFIRMED]

    def audits_of(self, action: AuditAction) -> list[dict]:
        return [a for a in self.audits if a.get("action") == action]


# ---------------------------------------------------------------------------
# Test fixtures — deterministic embeddings shaped exactly like the mock
# re-ID backend produces (unit-norm float32, 512 dims).
# ---------------------------------------------------------------------------

_DIM = 512


def _unit_embedding(seed: int) -> tuple[float, ...]:
    v = np.random.default_rng(seed).standard_normal(_DIM).astype(np.float32)
    v = v / float(np.linalg.norm(v))
    return tuple(float(x) for x in v)


def _person(det_id: str, embedding: tuple[float, ...],
            conf: float = 0.9) -> Detection:
    return Detection(
        detection_id=det_id,
        class_name="person",
        threat_class=ThreatClass.PERSON,
        confidence=conf,
        bbox=BoundingBox(x1=0, y1=0, x2=50, y2=120),
        track_id=f"local-{det_id}",
        embedding=embedding,
    )


def _event(
    camera_id: str,
    frame_id: str,
    detection: Detection,
    *,
    watchlist_hit: bool = False,
) -> DetectionEvent:
    cam = CameraRef(camera_id=camera_id, latitude=0.0, longitude=0.0)
    hits = ()
    if watchlist_hit:
        hits = (
            WatchlistHit(
                detection_id=detection.detection_id,
                identity_id="WL-001",
                identity_label="person_of_interest",
                similarity=1.0,
                source="face",
            ),
        )
    return DetectionEvent(
        event_id=f"e-{frame_id}",
        camera=cam,
        frame_id=frame_id,
        frame_uri=f"file:///tmp/{frame_id}.jpg",
        frame_sha256="a" * 64,
        detections=(detection,),
        watchlist_hits=hits,
        emitted_at=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Harness: stands up a tracker + a confirmation pipeline backed by the same
# bus. Mimics what confirmation.main does with its two consumer loops,
# except here we drive the flow manually so we can assert at each step.
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self) -> None:
        self.bus = InMemoryBus()
        self.bank = IdentityBank(
            match_threshold=0.72,
            recency_seconds=300.0,
            max_identities=1000,
            centroid_alpha=0.2,
        )
        self.identity_view = GlobalIdentityView(recency_seconds=300.0)
        cfg = ConfirmationConfig(
            CONFIRMATION_AUTOCONFIRM_THRESHOLD=0.85,
            CONFIRMATION_OPERATOR_REVIEW_THRESHOLD=0.55,
            CONFIRMATION_TRACK_WINDOW_SECONDS=30,
            CONFIRMATION_CORROBORATION_TARGET_CAMERAS=3,
            CONFIRMATION_IDENTITY_RECENCY_SECONDS=300.0,
            CONFIRMATION_HTTP_PORT=8080,
            REDIS_URL="redis://localhost:6379/0",
        )  # type: ignore[call-arg]
        self.pipeline = ConfirmationPipeline(
            cfg=cfg,
            bus=self.bus,
            tracks=InMemoryTrackStore(30),
            alerts=RecentAlerts(),
            identity_view=self.identity_view,
        )

    async def process(self, event: DetectionEvent) -> None:
        """Full fan-out for one DetectionEvent:
          1. Tracker handler → emits TrackUpdate (and maybe CrossCameraMatch).
          2. Confirmation's identity-view consumer ingests the TrackUpdate.
          3. Confirmation pipeline scores the event.

        Ordering here is tracker-before-confirmation, which is the happy path
        (no race). The race variant is already tested in
        test_cross_camera_corroboration::test_unresolved_detection_falls_back_to_legacy.
        """
        for det in event.detections:
            if det.threat_class != ThreatClass.PERSON:
                continue
            await tracker_handle_detection(event, det, self.bank, self.bus)

        # Drain the track updates the tracker just emitted into the
        # confirmation identity view. This is what _track_update_loop
        # does in production.
        for update in self.bus.track_updates():
            self.identity_view.observe(
                global_track_id=update.global_track_id,
                camera_id=update.camera.camera_id,
                detection_id=update.detection_id,
            )

        await self.pipeline.handle(event)


# ---------------------------------------------------------------------------
# The Phase 2 milestone scenarios.
# ---------------------------------------------------------------------------


class TestPhase2EndToEnd:
    def test_single_camera_single_frame_baseline(self):
        """Baseline: one frame, one camera, no watchlist. Should produce a
        'new' TRACK_ASSIGNED audit and a scored-but-suppressed alert."""
        async def scenario():
            h = Harness()
            emb = _unit_embedding(1)
            await h.process(_event("CAM-A", "f1", _person("d1", emb)))
            return h

        h = asyncio.run(scenario())
        assigned = h.bus.audits_of(AuditAction.TRACK_ASSIGNED)
        assert len(assigned) == 1
        # This is the first sighting → one camera → legacy corroboration path
        # in the scorer (distinct_cameras=1 / target=3).
        scored = h.bus.audits_of(AuditAction.ALERT_SCORED)
        assert scored, "no ALERT_SCORED audit"
        assert scored[0]["payload"]["distinct_cameras"] == 1
        assert "global_track_id" in scored[0]["payload"]

    def test_cross_camera_handoff_preserves_global_track_id(self):
        """THE Phase 2 milestone. Same person on CAM-A then CAM-B → one
        global_track_id spans both, handoff audit recorded, corroboration
        on the CAM-B score reflects 2 distinct cameras."""
        async def scenario():
            h = Harness()
            emb = _unit_embedding(42)

            await h.process(_event("CAM-A", "f1", _person("d1-a", emb)))
            await h.process(_event("CAM-B", "f2", _person("d2-b", emb)))
            return h

        h = asyncio.run(scenario())

        # 1. Tracker emitted one ASSIGN and one HANDOFF audit.
        assigned = h.bus.audits_of(AuditAction.TRACK_ASSIGNED)
        handed = h.bus.audits_of(AuditAction.CROSS_CAMERA_HANDOFF)
        assert len(assigned) == 1
        assert len(handed) == 1

        # 2. The global_track_id is the SAME across both events.
        gtid_assigned = assigned[0]["subject_id"]
        gtid_handed = handed[0]["subject_id"]
        assert gtid_assigned == gtid_handed, \
            "global_track_id did not persist across camera boundary"

        # 3. Tracker also published a TrackUpdate(disposition=handed_off)
        #    and a CrossCameraMatch.
        updates = h.bus.track_updates()
        assert any(u.disposition == "handed_off" for u in updates)
        handoff_events = [e for t, e in h.bus.published
                          if t == Topics.CROSS_CAMERA_MATCHES]
        assert len(handoff_events) == 1
        assert handoff_events[0].prior_camera.camera_id == "CAM-A"
        assert handoff_events[0].new_camera.camera_id == "CAM-B"

        # 4. Confirmation scored the CAM-B frame with distinct_cameras=2.
        scored = h.bus.audits_of(AuditAction.ALERT_SCORED)
        cam_b_scored = [s for s in scored if s["correlation_ids"]["camera_id"] == "CAM-B"]
        assert len(cam_b_scored) == 1
        cam_b_payload = cam_b_scored[0]["payload"]
        assert cam_b_payload["distinct_cameras"] == 2
        assert cam_b_payload["global_track_id"] == gtid_assigned
        # 2/target_cameras(3) ≈ 0.667
        assert 0.66 < cam_b_payload["signals"]["corroboration"] < 0.67

    def test_three_cameras_saturate_corroboration(self):
        """Three distinct cameras in the window → corroboration=1.0. This is
        the evidentiary ceiling the Phase 2 scorer is designed around."""
        async def scenario():
            h = Harness()
            emb = _unit_embedding(7)
            await h.process(_event("CAM-A", "f1", _person("d1", emb)))
            await h.process(_event("CAM-B", "f2", _person("d2", emb)))
            await h.process(_event("CAM-C", "f3", _person("d3", emb)))
            return h

        h = asyncio.run(scenario())
        scored = h.bus.audits_of(AuditAction.ALERT_SCORED)
        cam_c = [s for s in scored if s["correlation_ids"]["camera_id"] == "CAM-C"][-1]
        assert cam_c["payload"]["distinct_cameras"] == 3
        assert cam_c["payload"]["signals"]["corroboration"] == 1.0

    def test_different_people_get_distinct_gtids(self):
        """Two people on two cameras must NOT be fused into one identity."""
        async def scenario():
            h = Harness()
            e1 = _unit_embedding(1)
            e2 = _unit_embedding(999)
            await h.process(_event("CAM-A", "f1", _person("d1", e1)))
            await h.process(_event("CAM-B", "f2", _person("d2", e2)))
            return h

        h = asyncio.run(scenario())
        assigned = h.bus.audits_of(AuditAction.TRACK_ASSIGNED)
        assert len(assigned) == 2, "expected two distinct identities"
        gtids = {a["subject_id"] for a in assigned}
        assert len(gtids) == 2
        # No handoff audit should have fired.
        assert not h.bus.audits_of(AuditAction.CROSS_CAMERA_HANDOFF)

    def test_watchlist_hit_plus_cross_camera_lifts_score_above_review(self):
        """The flagship Phase 2 outcome: a person-of-interest (watchlist hit)
        seen on 3 cameras within the window → score crosses operator-review
        threshold AND the score MONOTONICALLY rises as more cameras are
        added. This is the behaviour the scorer was re-designed around.

        Signal math (default weights 0.50 / 0.25 / 0.15 / 0.10):
            watchlist_hit=1.0 (→0.50) + corroboration=k/3 (→0.10*k/3)
              CAM-A:  k=1  →  0.50 + 0.033 = 0.533  (SUPPRESSED,  below 0.55)
              CAM-B:  k=2  →  0.50 + 0.067 = 0.567  (OPERATOR_REVIEW)
              CAM-C:  k=3  →  0.50 + 0.100 = 0.600  (OPERATOR_REVIEW)

        Full auto-confirm at 0.85 requires persistence to mature (30s window).
        That's verified end-to-end in the existing plate-to-alert tests.

        Note: OPERATOR_REVIEW alerts are stored in-process (RecentAlerts),
        not published to Kafka — ALERTS_CONFIRMED is reserved for auto-confirms.
        That's the published contract and this test pins it.
        """
        async def scenario():
            h = Harness()
            emb = _unit_embedding(123)
            await h.process(_event("CAM-A", "f1", _person("d1", emb),
                                   watchlist_hit=True))
            await h.process(_event("CAM-B", "f2", _person("d2", emb),
                                   watchlist_hit=True))
            await h.process(_event("CAM-C", "f3", _person("d3", emb),
                                   watchlist_hit=True))
            return h

        h = asyncio.run(scenario())

        scored = h.bus.audits_of(AuditAction.ALERT_SCORED)
        by_cam = {s["correlation_ids"]["camera_id"]: s["payload"] for s in scored}

        # Monotonic rise driven purely by cross-camera corroboration.
        assert by_cam["CAM-A"]["threat_score"] < by_cam["CAM-B"]["threat_score"]
        assert by_cam["CAM-B"]["threat_score"] < by_cam["CAM-C"]["threat_score"]

        # CAM-C score crosses the review threshold (0.55).
        assert by_cam["CAM-C"]["threat_score"] >= 0.55
        assert by_cam["CAM-C"]["signals"]["corroboration"] == 1.0
        assert by_cam["CAM-C"]["signals"]["watchlist_hit"] == 1.0

        # The operator review queue got the alert.
        # Use recent_sync() — this test runs outside any event loop context
        # and we only need the snapshot, not the async interface.
        assert len(h.pipeline._alerts.recent_sync()) >= 1, \
            "expected at least one alert in the in-process review queue"

        # A QUEUED_FOR_REVIEW audit was recorded (the operator-visible trail).
        reviewed = h.bus.audits_of(AuditAction.ALERT_QUEUED_FOR_REVIEW)
        assert len(reviewed) >= 1

        # All three cameras share the same global_track_id in the audit trail —
        # that's the operator's "why did you correlate these three cameras?" story.
        gtids_in_audit = {
            s["payload"].get("global_track_id") for s in scored
            if s["payload"].get("global_track_id")
        }
        assert len(gtids_in_audit) == 1

    def test_degenerate_embedding_does_not_crash_tracker(self):
        """If re-ID produced garbage (all zeros), the tracker must log and
        skip, not blow up the whole consumer loop."""
        async def scenario():
            h = Harness()
            bad = tuple([0.0] * _DIM)
            await h.process(_event("CAM-A", "f1", _person("d1", bad)))
            return h

        h = asyncio.run(scenario())
        # No TRACK_ASSIGNED audit (tracker rejected the bad embedding).
        assert not h.bus.audits_of(AuditAction.TRACK_ASSIGNED)
        # Confirmation still scored the frame (watchlist + behaviour path).
        assert h.bus.audits_of(AuditAction.ALERT_SCORED)
