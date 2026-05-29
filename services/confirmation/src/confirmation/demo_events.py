"""Dev-mode synthetic event generator.

When DEV_MODE=1, this module spawns a background task that emits fake alerts
and cross-camera handoffs every few seconds so the operator dashboard has
something to render without requiring Kafka, Redis, or the full backend stack.

NOT for production. The events are plausible but entirely synthetic.
"""
from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from veilwatch_contracts.events import (
    AlertDisposition,
    BoundingBox,
    CameraRef,
    ConfirmedAlert,
    CrossCameraMatch,
    Detection,
    ThreatClass,
    WatchlistHit,
)

if TYPE_CHECKING:
    from confirmation.alert_store import RecentAlerts
    from confirmation.live_map import LiveMapBroadcaster

# Demo camera network — matches the frontend's stores/cameras.ts demo set.
DEMO_CAMERAS: list[CameraRef] = [
    CameraRef(
        camera_id="CAM-A",
        latitude=37.8084,
        longitude=-122.4098,
        label="Embarcadero · Pier 39",
        environment="road",
    ),
    CameraRef(
        camera_id="CAM-B",
        latitude=37.8058,
        longitude=-122.4170,
        label="Fisherman's Wharf",
        environment="public",
    ),
    CameraRef(
        camera_id="CAM-C",
        latitude=37.8024,
        longitude=-122.4058,
        label="Bay Bridge approach",
        environment="road",
    ),
    CameraRef(
        camera_id="CAM-D",
        latitude=37.8024,
        longitude=-122.4233,
        label="Ghirardelli Square",
        environment="public",
    ),
]

THREAT_CLASSES = [ThreatClass.PERSON, ThreatClass.VEHICLE, ThreatClass.WEAPON]
CLASS_NAMES = {
    ThreatClass.PERSON: ["person", "pedestrian"],
    ThreatClass.VEHICLE: ["car", "truck", "motorcycle"],
    ThreatClass.WEAPON: ["handgun", "knife"],
}

# Fake watchlist identities for demo hits.
WATCHLIST_IDENTITIES = [
    ("WL-001", "John Doe (wanted)"),
    ("WL-002", "Jane Smith (missing)"),
    ("WL-003", "Plate: 7ABC123"),
    ("WL-004", "Plate: STOLEN99"),
]


def _random_detection() -> Detection:
    tc = random.choice(THREAT_CLASSES)
    return Detection(
        detection_id=str(uuid.uuid4()),
        class_name=random.choice(CLASS_NAMES[tc]),
        threat_class=tc,
        confidence=round(random.uniform(0.65, 0.98), 2),
        bbox=BoundingBox(x1=100, y1=100, x2=300, y2=400),
        embedding=None,
    )


def _random_alert(camera: CameraRef) -> ConfirmedAlert:
    """Generate a plausible synthetic alert."""
    score = round(random.uniform(0.45, 0.96), 2)
    # Disposition based on score thresholds matching the real pipeline.
    if score >= 0.85:
        disp = AlertDisposition.AUTO_CONFIRMED
    elif score >= 0.55:
        disp = AlertDisposition.OPERATOR_REVIEW
    else:
        disp = AlertDisposition.SUPPRESSED

    detection = _random_detection()

    # ~30% chance of a watchlist hit on high-score alerts.
    hits: list[WatchlistHit] = []
    if score >= 0.70 and random.random() < 0.3:
        wl_id, wl_label = random.choice(WATCHLIST_IDENTITIES)
        hits.append(
            WatchlistHit(
                detection_id=detection.detection_id,
                identity_id=wl_id,
                identity_label=wl_label,
                similarity=round(random.uniform(0.88, 0.99), 2),
            )
        )

    frame_id = str(uuid.uuid4())
    return ConfirmedAlert(
        event_id=str(uuid.uuid4()),
        incident_id=str(uuid.uuid4()),
        camera=camera,
        frame_id=frame_id,
        frame_uri=f"file:///frames/{camera.camera_id}/{frame_id[:8]}.jpg",
        frame_sha256="0" * 64,
        primary_detection=detection,
        watchlist_hits=tuple(hits),
        threat_score=score,
        signal_breakdown={
            "watchlist": 0.4 if hits else 0.0,
            "behaviour": round(random.uniform(0.0, 0.2), 2),
            "corroboration": round(random.uniform(0.0, 0.15), 2),
            "persistence": round(random.uniform(0.0, 0.1), 2),
        },
        disposition=disp,
        emitted_at=datetime.now(timezone.utc),
    )


def _random_handoff() -> CrossCameraMatch:
    """Generate a plausible cross-camera handoff between two random cameras."""
    cams = random.sample(DEMO_CAMERAS, 2)
    return CrossCameraMatch(
        event_id=str(uuid.uuid4()),
        global_track_id=str(uuid.uuid4()),
        prior_camera=cams[0],
        new_camera=cams[1],
        prior_detection_id=str(uuid.uuid4()),
        new_detection_id=str(uuid.uuid4()),
        similarity=round(random.uniform(0.82, 0.98), 2),
        elapsed_seconds=round(random.uniform(2.0, 45.0), 1),
        emitted_at=datetime.now(timezone.utc),
    )


async def run_demo_loop(
    alerts_store: RecentAlerts,
    broadcaster: LiveMapBroadcaster,
    interval_seconds: float = 4.0,
) -> None:
    """Background task that emits demo events until cancelled.

    - Every `interval_seconds`: emit 1 alert (random camera).
    - Every 3rd cycle: also emit a cross-camera handoff.
    """
    import logging
    log = logging.getLogger("confirmation.demo_events")
    log.info("demo loop started, interval=%.1fs", interval_seconds)

    cycle = 0
    while True:
        await asyncio.sleep(interval_seconds)
        cycle += 1

        try:
            camera = random.choice(DEMO_CAMERAS)
            alert = _random_alert(camera)
            log.info("emitting demo alert %s on %s", alert.incident_id[:8], camera.camera_id)

            # Store so /alerts/recent returns it.
            await alerts_store.add(alert)

            # Broadcast to live-map WebSocket clients.
            await broadcaster.publish("alert", alert)

            # Every 3rd cycle, also emit a handoff.
            if cycle % 3 == 0:
                handoff = _random_handoff()
                await broadcaster.publish("handoff", handoff)
        except Exception:
            log.exception("demo event generation failed")
