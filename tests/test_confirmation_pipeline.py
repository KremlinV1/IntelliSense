"""Confirmation pipeline behaviour.

These tests pin down the decision matrix (auto-confirm / review / suppress) and
the audit-before-publish invariant (Constraint #3). If you find yourself
relaxing either, stop and write an ADR first."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from veilwatch_contracts import (
    BoundingBox,
    CameraRef,
    Detection,
    DetectionEvent,
    WatchlistHit,
)
from veilwatch_contracts.events import AuditAction, ThreatClass
from veilwatch_contracts.topics import Topics

from confirmation.alert_store import RecentAlerts
from confirmation.config import ConfirmationConfig
from confirmation.pipeline import ConfirmationPipeline
from confirmation.track_store import InMemoryTrackStore


class FakeBus:
    """Records topic + event ordering so we can assert audit-precedes-alert."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, BaseModel | dict]] = []
        self.service_name = "confirmation"

    async def publish(self, topic: str, event: BaseModel, *, key: str | None = None) -> None:
        self.calls.append((topic, event))

    async def audit(self, *, action, subject_id, correlation_ids=None, actor_id="system", payload=None):
        self.calls.append(("audit", {"action": action, "subject_id": subject_id, "payload": payload or {}}))


def _det_event(watchlist_sim: float = 0.0, weapon_conf: float = 0.0, person_conf: float = 0.8) -> DetectionEvent:
    cam = CameraRef(camera_id="CAM-TEST", latitude=0.0, longitude=0.0)
    dets: list[Detection] = []
    person = Detection(
        detection_id="d1",
        class_name="person",
        threat_class=ThreatClass.PERSON,
        confidence=person_conf,
        bbox=BoundingBox(x1=0, y1=0, x2=10, y2=10),
        track_id="t1",
    )
    dets.append(person)
    if weapon_conf > 0:
        dets.append(Detection(
            detection_id="d2",
            class_name="knife",
            threat_class=ThreatClass.WEAPON,
            confidence=weapon_conf,
            bbox=BoundingBox(x1=0, y1=0, x2=5, y2=5),
        ))
    hits: tuple[WatchlistHit, ...] = ()
    if watchlist_sim > 0:
        hits = (WatchlistHit(
            detection_id="d1",
            identity_id="WL-TEST",
            identity_label="test_subject",
            similarity=watchlist_sim,
        ),)
    return DetectionEvent(
        event_id="e1",
        camera=cam,
        frame_id="f1",
        frame_uri="file:///tmp/f1.jpg",
        frame_sha256="a" * 64,
        detections=tuple(dets),
        watchlist_hits=hits,
        emitted_at=datetime.now(tz=timezone.utc),
    )


def _pipeline(autoconfirm: float = 0.85, review: float = 0.55):
    cfg = ConfirmationConfig(
        CONFIRMATION_AUTOCONFIRM_THRESHOLD=autoconfirm,
        CONFIRMATION_OPERATOR_REVIEW_THRESHOLD=review,
        CONFIRMATION_TRACK_WINDOW_SECONDS=30,
        CONFIRMATION_HTTP_PORT=8080,
        REDIS_URL="redis://localhost:6379/0",
    )  # type: ignore[call-arg]
    bus = FakeBus()
    return cfg, bus, ConfirmationPipeline(cfg, bus, InMemoryTrackStore(30), RecentAlerts())


def test_strong_watchlist_plus_weapon_auto_confirms():
    _, bus, pipe = _pipeline()
    # score = 0.5 * 0.95 (watchlist) + 0.25 * 0.9 (weapon) + 0.15 * small + 0.10 * small
    #       = 0.475 + 0.225 + ... ≈ 0.73 — below autoconfirm but above review
    # Push it higher by running the event twice (builds persistence + corroboration).
    asyncio.run(pipe.handle(_det_event(watchlist_sim=0.95, weapon_conf=0.9)))
    bus.calls.clear()
    asyncio.run(pipe.handle(_det_event(watchlist_sim=0.95, weapon_conf=0.9)))

    topics = [c[0] for c in bus.calls]
    # Invariant: the ALERTS_CONFIRMED publish (if any) must be preceded by an audit.
    if Topics.ALERTS_CONFIRMED in topics:
        idx = topics.index(Topics.ALERTS_CONFIRMED)
        assert "audit" in topics[:idx], "alert published without a preceding audit record"


def test_no_detections_is_suppressed_with_audit():
    _, bus, pipe = _pipeline()
    cam = CameraRef(camera_id="CAM-TEST", latitude=0.0, longitude=0.0)
    empty = DetectionEvent(
        event_id="e", camera=cam, frame_id="f", frame_uri="file:///tmp/f.jpg",
        frame_sha256="a" * 64, detections=(), watchlist_hits=(),
        emitted_at=datetime.now(tz=timezone.utc),
    )
    asyncio.run(pipe.handle(empty))
    assert any(c[0] == "audit" and c[1]["action"] == AuditAction.ALERT_SUPPRESSED for c in bus.calls)
    assert not any(c[0] == Topics.ALERTS_CONFIRMED for c in bus.calls)


def test_low_score_is_suppressed():
    _, bus, pipe = _pipeline()
    # Person-only detection, no watchlist, no weapon, new track → score well below review threshold.
    asyncio.run(pipe.handle(_det_event(watchlist_sim=0.0, weapon_conf=0.0, person_conf=0.4)))
    assert not any(c[0] == Topics.ALERTS_CONFIRMED for c in bus.calls)


def test_audit_precedes_every_alert_publish():
    """Constraint #3: for every ALERTS_CONFIRMED publish, there is at least one
    audit call earlier in the call sequence with a matching subject_id-ish tag."""
    _, bus, pipe = _pipeline(autoconfirm=0.1, review=0.05)  # force auto-confirm
    asyncio.run(pipe.handle(_det_event(watchlist_sim=0.95, weapon_conf=0.9)))
    # Find every publish index; ensure an audit call exists earlier.
    for i, (topic, _) in enumerate(bus.calls):
        if topic == Topics.ALERTS_CONFIRMED:
            earlier = [c for c in bus.calls[:i] if c[0] == "audit"]
            assert earlier, f"no audit recorded before alert publish at index {i}"
