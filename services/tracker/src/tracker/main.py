"""Tracker service entrypoint.

Consumes DetectionEvents, ignores everything except person detections that
carry a re-ID embedding, maintains a global identity bank, and emits:
  - TrackUpdate on `tracks.updates.v1` for EVERY person detection (new /
    continued / handed_off)
  - CrossCameraMatch on `tracks.handoffs.v1` for the 'handed_off' subset

Event ordering invariants (Constraint #3):
  1. Audit the disposition (TRACK_ASSIGNED / TRACK_CONTINUED / CROSS_CAMERA_HANDOFF).
  2. Publish TrackUpdate.
  3. If handed_off, publish CrossCameraMatch.

A CrossCameraMatch is NOT a side-channel alert; it's diagnostic state for the
operator map. Threat scoring still flows through confirmation, which will (in
Slice 2) subscribe to tracks.updates.v1 to upgrade its corroboration signal.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import numpy as np

from veilwatch_contracts import (
    CrossCameraMatch,
    Detection,
    DetectionEvent,
    Topics,
    TrackUpdate,
)
from veilwatch_contracts.bus import EventBus
from veilwatch_contracts.events import AuditAction, ThreatClass
from veilwatch_contracts.obs import configure_logging, set_correlation

from tracker.bank_factory import build_bank
from tracker.config import TrackerConfig
from tracker.identity_bank import IdentityBankLike, MatchOutcome

SERVICE = "tracker"


_DISPOSITION_TO_AUDIT = {
    "new": AuditAction.TRACK_ASSIGNED,
    "continued": AuditAction.TRACK_CONTINUED,
    "handed_off": AuditAction.CROSS_CAMERA_HANDOFF,
}


async def _handle_detection(
    event: DetectionEvent,
    detection: Detection,
    bank: IdentityBankLike,
    bus: EventBus,
) -> None:
    log = logging.getLogger(SERVICE)
    if detection.embedding is None:
        # Older detection events (pre-Phase-2) or detections where re-ID was
        # skipped (tiny crop, etc.). Nothing to do.
        return

    set_correlation(
        camera_id=event.camera.camera_id,
        frame_id=event.frame_id,
        detection_id=detection.detection_id,
    )

    try:
        embedding = np.asarray(detection.embedding, dtype=np.float32)
        outcome: MatchOutcome = bank.observe(
            embedding=embedding,
            camera_id=event.camera.camera_id,
            detection_id=detection.detection_id,
        )
    except ValueError:
        # Degenerate embedding (all zeros, NaNs). Drop without crashing the
        # consumer — a single bad detection must not poison the bank.
        log.warning("skipping degenerate embedding",
                    extra={"detection_id": detection.detection_id})
        return

    audit_action = _DISPOSITION_TO_AUDIT[outcome.disposition]
    await bus.audit(
        action=audit_action,
        subject_id=outcome.identity.global_track_id,
        correlation_ids={
            "camera_id": event.camera.camera_id,
            "frame_id": event.frame_id,
            "detection_id": detection.detection_id,
        },
        payload={
            "similarity": outcome.similarity,
            "num_sightings": outcome.identity.num_sightings,
            **(
                {"prior_camera_id": outcome.prior_camera,
                 "elapsed_seconds": outcome.elapsed_seconds}
                if outcome.disposition == "handed_off" else {}
            ),
        },
    )

    update = TrackUpdate(
        event_id=_uuid(),
        global_track_id=outcome.identity.global_track_id,
        camera=event.camera,
        detection_id=detection.detection_id,
        frame_id=event.frame_id,
        disposition=outcome.disposition,
        match_similarity=outcome.similarity,
    )
    await bus.publish(Topics.TRACKS_UPDATES, update, key=outcome.identity.global_track_id)

    if outcome.disposition == "handed_off":
        assert outcome.prior_camera is not None  # narrowing for type checkers
        assert outcome.prior_detection_id is not None
        # The prior_camera field on CrossCameraMatch needs a CameraRef, but the
        # identity bank only remembers the camera_id string. For Slice 1 we
        # reconstruct a minimal CameraRef; Slice 3's Redis migration will
        # store the full CameraRef snapshot so lat/lon travels with handoffs.
        match = CrossCameraMatch(
            event_id=_uuid(),
            global_track_id=outcome.identity.global_track_id,
            prior_camera=event.camera.model_copy(update={"camera_id": outcome.prior_camera}),
            new_camera=event.camera,
            prior_detection_id=outcome.prior_detection_id,
            new_detection_id=detection.detection_id,
            similarity=outcome.similarity,
            elapsed_seconds=outcome.elapsed_seconds,
        )
        await bus.publish(Topics.CROSS_CAMERA_MATCHES, match, key=outcome.identity.global_track_id)


def _uuid() -> str:
    import uuid as _u
    return str(_u.uuid4())


async def run() -> None:
    cfg = TrackerConfig()  # type: ignore[call-arg]
    log = configure_logging(SERVICE)
    log.info("starting tracker",
             extra={"threshold": cfg.match_threshold,
                    "recency_s": cfg.recency_seconds,
                    "max_identities": cfg.max_identities})

    bank = build_bank(cfg)
    log.info("identity bank ready", extra={"backend": cfg.bank_backend})
    bus = EventBus(SERVICE)
    await bus.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async for event in bus.consume(
            topic=Topics.DETECTION_EVENTS,
            group_id=cfg.consumer_group,
            schema=DetectionEvent,
            dlq_topic=Topics.DLQ_DETECTION_EVENTS,
        ):
            if stop.is_set():
                break
            try:
                for det in event.detections:
                    if det.threat_class != ThreatClass.PERSON:
                        continue
                    await _handle_detection(event, det, bank, bus)
            except Exception:
                log.exception("tracker handler failed",
                              extra={"frame_id": event.frame_id})
    finally:
        await bus.stop()
        log.info("tracker stopped")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
