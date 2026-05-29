"""Kafka topic names. Versioned in the name so a consumer can't accidentally
read a newer schema than it was compiled against."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Topics:
    # Producer: capture. Consumer: detection.
    CAMERA_FRAMES: str = "camera.frames.v1"
    # Producer: detection. Consumers: confirmation, tracker.
    DETECTION_EVENTS: str = "detection.events.v1"
    # Producer: confirmation. Consumer: dispatch, narrator, officer-app gateway.
    ALERTS_CONFIRMED: str = "alerts.confirmed.v1"
    # Producer: tracker. Consumers: confirmation (for corroboration signal), operator dashboard.
    TRACKS_UPDATES: str = "tracks.updates.v1"
    # Producer: tracker. Consumer: operator dashboard (live map).
    CROSS_CAMERA_MATCHES: str = "tracks.handoffs.v1"
    # Producer: every service. Consumer: audit sink + cold storage.
    AUDIT_LOG: str = "audit.log.v1"

    # Dead-letter queues — one per primary topic. Messages that fail parse or
    # handler logic land here rather than blocking the consumer group.
    DLQ_CAMERA_FRAMES: str = "camera.frames.v1.dlq"
    DLQ_DETECTION_EVENTS: str = "detection.events.v1.dlq"
    DLQ_TRACKS_UPDATES: str = "tracks.updates.v1.dlq"
