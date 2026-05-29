"""Confirmation pipeline: DetectionEvent → ConfirmedAlert (or suppressed / queued).

Decision flow (Constraint #3: audit write PRECEDES every downstream side-effect):
    1. Pick primary detection (highest-threat non-unknown class).
    2. Compute signals (watchlist, behaviour, persistence, corroboration).
    3. Fuse → threat_score.
    4. Audit `ALERT_SCORED` with the full breakdown.
    5. Apply thresholds → disposition (AUTO_CONFIRMED / OPERATOR_REVIEW / SUPPRESSED).
    6. Audit the disposition action.
    7. If auto-confirmed, publish ConfirmedAlert to `alerts.confirmed.v1`.
    8. If queued for review, push to the operator queue instead (Redis stream).
"""
from __future__ import annotations

import logging
import time
import uuid

from veilwatch_contracts import (
    AlertDisposition,
    ConfirmedAlert,
    Detection,
    DetectionEvent,
    Topics,
)
from veilwatch_contracts.bus import EventBus
from veilwatch_contracts.events import AuditAction, ThreatClass
from veilwatch_contracts.scoring import fuse_signals

from confirmation.alert_store import AlertStore, RecentAlerts  # RecentAlerts kept for back-compat
from confirmation.config import ConfirmationConfig
from confirmation.identity_view import GlobalIdentityView
from confirmation.live_map import get_broadcaster
from confirmation.scoring import (
    signal_behaviour,
    signal_corroboration,
    signal_persistence,
    signal_watchlist_hit,
)
from confirmation.track_store import InMemoryTrackStore

log = logging.getLogger(__name__)


# Threat-class priority for picking the "primary" detection in a frame.
_THREAT_PRIORITY = {
    ThreatClass.WEAPON: 5,
    ThreatClass.PERSON: 3,
    ThreatClass.VEHICLE: 2,
    ThreatClass.PLATE: 2,
    ThreatClass.UNKNOWN: 0,
}


def _pick_primary(event: DetectionEvent) -> Detection | None:
    if not event.detections:
        return None
    # If a watchlist hit exists, prefer the detection it attaches to —
    # that's the highest-value signal we have.
    hit_ids = {h.detection_id for h in event.watchlist_hits}
    hit_dets = [d for d in event.detections if d.detection_id in hit_ids]
    if hit_dets:
        return max(hit_dets, key=lambda d: d.confidence)
    return max(
        event.detections,
        key=lambda d: (_THREAT_PRIORITY.get(d.threat_class, 0), d.confidence),
    )


class ConfirmationPipeline:
    def __init__(
        self,
        cfg: ConfirmationConfig,
        bus: EventBus,
        tracks: InMemoryTrackStore,
        alerts: AlertStore,
        identity_view: GlobalIdentityView | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._tracks = tracks
        self._alerts = alerts
        # Slice 2: Optional cross-camera identity view. If present (production
        # path), corroboration uses the distinct-camera count from it. If
        # absent (unit tests for pre-Slice-2 behaviour), we fall through to
        # the legacy frame-count heuristic.
        self._identity_view = identity_view

    async def handle(self, event: DetectionEvent) -> None:
        primary = _pick_primary(event)
        if primary is None:
            # Nothing to score. We still audit that we saw the frame's detection
            # event (empty) — useful for diagnosing blind spots.
            await self._bus.audit(
                action=AuditAction.ALERT_SUPPRESSED,
                subject_id=event.frame_id,
                correlation_ids={"camera_id": event.camera.camera_id},
                payload={"reason": "no_detections"},
            )
            return

        track_id = primary.track_id or primary.detection_id
        track = self._tracks.observe(
            event.camera.camera_id, track_id, primary.confidence
        )

        # Slice 2: try to resolve the primary detection to a cross-camera
        # global identity. If unresolved (race with tracker, or this track
        # has only ever been seen on one camera and the TrackUpdate hasn't
        # arrived yet), distinct_cameras stays None and corroboration falls
        # back to the legacy frame-count heuristic.
        distinct_cameras: int | None = None
        global_track_id: str | None = None
        if self._identity_view is not None:
            cross = self._identity_view.resolve(primary.detection_id)
            if cross is not None:
                distinct_cameras = cross.distinct_camera_count()
                global_track_id = cross.global_track_id

        signals = {
            "watchlist_hit": signal_watchlist_hit(event),
            "behaviour": signal_behaviour(event),
            "persistence": signal_persistence(track, time.time(), self._cfg.track_window_seconds),
            "corroboration": signal_corroboration(
                track,
                distinct_cameras=distinct_cameras,
                target_cameras=self._cfg.corroboration_target_cameras,
            ),
        }
        score = fuse_signals(signals)

        # Step 4: audit the score and its breakdown. This record exists even if
        # the alert is ultimately suppressed — an operator needs to be able to
        # answer "why DIDN'T you alert on this?" as well as the opposite.
        audit_payload: dict = {"threat_score": score, "signals": signals}
        if distinct_cameras is not None:
            audit_payload["distinct_cameras"] = distinct_cameras
        if global_track_id is not None:
            audit_payload["global_track_id"] = global_track_id
        correlation = {
            "camera_id": event.camera.camera_id,
            "track_id": track_id,
        }
        if global_track_id is not None:
            correlation["global_track_id"] = global_track_id
        await self._bus.audit(
            action=AuditAction.ALERT_SCORED,
            subject_id=event.frame_id,
            correlation_ids=correlation,
            payload=audit_payload,
        )

        disposition = self._decide(score)

        if disposition == AlertDisposition.SUPPRESSED:
            await self._bus.audit(
                action=AuditAction.ALERT_SUPPRESSED,
                subject_id=event.frame_id,
                correlation_ids={"camera_id": event.camera.camera_id},
                payload={"threat_score": score},
            )
            return

        incident_id = str(uuid.uuid4())
        alert = ConfirmedAlert(
            event_id=str(uuid.uuid4()),
            incident_id=incident_id,
            camera=event.camera,
            frame_id=event.frame_id,
            frame_uri=event.frame_uri,
            frame_sha256=event.frame_sha256,
            threat_score=score,
            disposition=disposition,
            primary_detection=primary,
            watchlist_hits=event.watchlist_hits,
            signal_breakdown=signals,
            confirming_operator_id=None,  # auto-path; set by operator console on manual confirm
        )

        # Step 6: disposition audit BEFORE publish / queue.
        if disposition == AlertDisposition.AUTO_CONFIRMED:
            await self._bus.audit(
                action=AuditAction.ALERT_AUTO_CONFIRMED,
                subject_id=incident_id,
                correlation_ids={
                    "frame_id": event.frame_id,
                    "camera_id": event.camera.camera_id,
                },
                payload={"threat_score": score},
            )
            await self._bus.publish(
                Topics.ALERTS_CONFIRMED, alert, key=event.camera.camera_id
            )
            await self._alerts.add(alert)
            # Fan out to any connected operator dashboards. Non-blocking by
            # design — a slow dashboard must not back-pressure the pipeline.
            await get_broadcaster().publish("alert", alert)
        else:  # OPERATOR_REVIEW
            await self._bus.audit(
                action=AuditAction.ALERT_QUEUED_FOR_REVIEW,
                subject_id=incident_id,
                correlation_ids={
                    "frame_id": event.frame_id,
                    "camera_id": event.camera.camera_id,
                },
                payload={"threat_score": score},
            )
            # Phase 1 keeps the review queue in-memory alongside confirmed alerts
            # so the operator console has something to render. Phase 3 moves
            # this to a Redis Stream so multiple operator instances share it.
            await self._alerts.add(alert)

    def _decide(self, score: float) -> AlertDisposition:
        if score >= self._cfg.autoconfirm_threshold:
            return AlertDisposition.AUTO_CONFIRMED
        if score >= self._cfg.operator_review_threshold:
            return AlertDisposition.OPERATOR_REVIEW
        return AlertDisposition.SUPPRESSED
