"""VeilWatch wire contracts.

Every service imports from here. Schema evolution rule: additive only within a
major version (`.v1`). Breaking changes get a new topic version (`.v2`) and both
producer and consumer are updated before the old topic is retired.
"""
from veilwatch_contracts.topics import Topics
from veilwatch_contracts.events import (
    AlertDisposition,
    AuditAction,
    AuditRecord,
    BoundingBox,
    CameraRef,
    ConfirmedAlert,
    CrossCameraMatch,
    Detection,
    DetectionEvent,
    FrameEvent,
    PlateRead,
    ThreatClass,
    TrackUpdate,
    WatchlistHit,
)
from veilwatch_contracts.scoring import format_score, fuse_signals

__all__ = [
    "Topics",
    "AlertDisposition",
    "AuditAction",
    "AuditRecord",
    "BoundingBox",
    "CameraRef",
    "ConfirmedAlert",
    "CrossCameraMatch",
    "Detection",
    "DetectionEvent",
    "FrameEvent",
    "PlateRead",
    "ThreatClass",
    "TrackUpdate",
    "WatchlistHit",
    "format_score",
    "fuse_signals",
]
