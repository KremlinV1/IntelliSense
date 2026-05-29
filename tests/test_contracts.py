"""Schema sanity: naive datetimes rejected, confidence bounds enforced,
bounding box x2>x1, unknown fields rejected (extra='forbid')."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from veilwatch_contracts import (
    BoundingBox,
    CameraRef,
    Detection,
    FrameEvent,
)
from veilwatch_contracts.events import ThreatClass


def _cam() -> CameraRef:
    return CameraRef(camera_id="CAM-TEST", latitude=0.0, longitude=0.0)


class TestFrameEvent:
    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                event_id="e",
                camera=_cam(),
                frame_id="f",
                frame_uri="file:///tmp/x.jpg",
                frame_sha256="a" * 64,
                width=640,
                height=480,
                captured_at=datetime(2026, 1, 1, 0, 0, 0),  # no tz
            )

    def test_hash_length_enforced(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                event_id="e",
                camera=_cam(),
                frame_id="f",
                frame_uri="file:///tmp/x.jpg",
                frame_sha256="tooshort",
                width=640,
                height=480,
                captured_at=datetime.now(tz=timezone.utc),
            )


class TestDetection:
    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            Detection(
                detection_id="d",
                class_name="person",
                threat_class=ThreatClass.PERSON,
                confidence=1.5,
                bbox=BoundingBox(x1=0, y1=0, x2=10, y2=10),
            )

    def test_bbox_x2_gt_x1(self):
        with pytest.raises(ValidationError):
            BoundingBox(x1=10, y1=0, x2=5, y2=10)

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            Detection(
                detection_id="d",
                class_name="person",
                threat_class=ThreatClass.PERSON,
                confidence=0.9,
                bbox=BoundingBox(x1=0, y1=0, x2=10, y2=10),
                sneaky_field="nope",  # type: ignore[call-arg]
            )
