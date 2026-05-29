"""Pydantic schemas for every inter-service event.

All events share a `schema_version` so consumers can fail closed when they
receive a payload they weren't built for. All timestamps are UTC ISO-8601 with
explicit offset; never naive datetimes — evidence law requires unambiguous
time."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _Event(BaseModel):
    """Base for every wire event. Frozen: events are immutable in flight."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(..., description="UUIDv4; unique per event emission.")
    # AwareDatetime rejects naive datetimes at parse time — evidence law
    # requires unambiguous timestamps across the whole event graph.
    emitted_at: AwareDatetime = Field(default_factory=_utc_now)


# ------------------------- Common value objects ----------------------------

class CameraRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    camera_id: str
    latitude: float
    longitude: float
    environment: Literal["road", "retail", "hospitality", "residential", "public"] = "public"
    label: str | None = None  # Human-friendly name, e.g. "Pier 39 entrance"


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x1: float
    y1: float
    x2: float
    y2: float

    @field_validator("x2")
    @classmethod
    def _x2_gt_x1(cls, v: float, info: Any) -> float:
        if "x1" in info.data and v <= info.data["x1"]:
            raise ValueError("x2 must be greater than x1")
        return v


class ThreatClass(str, Enum):
    PERSON = "person"
    VEHICLE = "vehicle"
    WEAPON = "weapon"
    PLATE = "plate"
    UNKNOWN = "unknown"


class Detection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    detection_id: str
    class_name: str
    threat_class: ThreatClass = ThreatClass.UNKNOWN
    confidence: float = Field(..., ge=0.0, le=1.0)
    bbox: BoundingBox
    # Short-term (per-camera) track id from the detector-side tracker — survives
    # frame-to-frame within a camera but does NOT survive camera boundaries.
    # Cross-camera persistent identity is carried by the tracker service's
    # `global_track_id` (see TrackUpdate) and is a separate concern.
    track_id: Optional[str] = None
    # Re-identification embedding attached by the detection service (Phase 2).
    # Unit-normalised so cosine similarity equals inner product. Dimensionality
    # is implementation-defined and must be validated by the consumer at
    # runtime — the contract does NOT pin it, because upgrading the re-ID
    # model (OSNet-x0_5 → x1_0 → transformer) shouldn't force a schema bump.
    # TODO(phase 2.5): switch JSON serialisation to base64(fp16) for wire efficiency.
    embedding: Optional[tuple[float, ...]] = None


class WatchlistHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    detection_id: str
    identity_id: str
    identity_label: str
    similarity: float = Field(..., ge=0.0, le=1.0)
    # How the embedding was sourced; affects how much we trust the hit.
    source: Literal["face", "gait", "osnet_reid", "plate"] = "face"


class PlateRead(BaseModel):
    """Single ALPR read against one vehicle crop.

    `plate_text` is the normalised, uppercase, whitespace-stripped canonical
    string. `raw_text` preserves the OCR's raw output for forensic replay.
    `region` follows ISO 3166-1 alpha-2 where known (e.g., 'us', 'jm', 'gb'),
    'unknown' otherwise. `confidence` is the ALPR engine's own posterior.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    detection_id: str  # the vehicle Detection this plate was read from
    plate_text: str = Field(..., min_length=1, max_length=16)
    raw_text: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    region: str = "unknown"


# ------------------------------ Topic events -------------------------------

class FrameEvent(_Event):
    """Capture → Detection. Carries a URI, not bytes — large payloads belong in
    the blob store. `frame_hash` is computed at capture time and persists with
    the clip for chain-of-custody."""
    kind: Literal["frame"] = "frame"
    camera: CameraRef
    frame_id: str
    frame_uri: str
    frame_sha256: str = Field(..., min_length=64, max_length=64)
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    captured_at: AwareDatetime


class DetectionEvent(_Event):
    """Detection → Confirmation. References the source frame by id + uri so
    confirmation can re-fetch for evidence packaging without re-decoding video."""
    kind: Literal["detection"] = "detection"
    camera: CameraRef
    frame_id: str
    frame_uri: str
    frame_sha256: str
    detections: tuple[Detection, ...]
    watchlist_hits: tuple[WatchlistHit, ...] = ()
    # Added in v1.5 (additive, optional). ALPR reads for vehicle detections.
    plate_reads: tuple[PlateRead, ...] = ()


# ---------------------------- Track events (v2) ----------------------------

class TrackUpdate(_Event):
    """Tracker → downstream. Emitted for every person detection that carries an
    embedding, whether or not a cross-camera match was made. Downstream
    consumers (confirmation, operator dashboard live map) use these to unify
    the corroboration signal and render persistent identities across cameras.

    `global_track_id` is the tracker's persistent UUID. It is STABLE: once
    assigned to an identity, it does not change across cameras or across
    re-appearances inside the tracker's recency window. If the identity
    leaves and comes back after the recency window expires, a NEW
    global_track_id is minted — this is intentional to bound memory and
    avoid identity-drift confabulation over long gaps.
    """
    kind: Literal["track_update"] = "track_update"
    global_track_id: str
    camera: CameraRef
    detection_id: str
    frame_id: str
    # 'new' = first time we've seen this identity; 'continued' = same
    # identity, same camera as last seen; 'handed_off' = same identity,
    # different camera than last seen (i.e., cross-camera match).
    disposition: Literal["new", "continued", "handed_off"]
    # Cosine similarity to the nearest existing identity's centroid. 1.0 for
    # 'new' tracks (no comparison); in [match_threshold, 1.0] for 'continued'
    # and 'handed_off'.
    match_similarity: float = Field(..., ge=0.0, le=1.0)


class CrossCameraMatch(_Event):
    """Tracker → operator dashboard. Emitted only on 'handed_off' track
    updates — the subset that's interesting for the live map. A separate
    event type (rather than a flag on TrackUpdate) so the dashboard can
    subscribe to a low-volume topic without having to filter every update.
    """
    kind: Literal["cross_camera_match"] = "cross_camera_match"
    global_track_id: str
    prior_camera: CameraRef
    new_camera: CameraRef
    prior_detection_id: str
    new_detection_id: str
    similarity: float = Field(..., ge=0.0, le=1.0)
    # Wall-clock gap between last sighting on prior camera and first on new.
    elapsed_seconds: float = Field(..., ge=0.0)


class AlertDisposition(str, Enum):
    # Auto-confirmed by threshold engine → dispatch fires.
    AUTO_CONFIRMED = "auto_confirmed"
    # Queued for human operator review (below auto threshold, above review threshold).
    OPERATOR_REVIEW = "operator_review"
    # Below review threshold — logged for audit, no alert fires.
    SUPPRESSED = "suppressed"


class ConfirmedAlert(_Event):
    """Confirmation → Dispatch/Narrator.

    `threat_score` is the fused, calibrated score in [0, 1]. Never round up on
    display — use `scoring.format_score`."""
    kind: Literal["confirmed_alert"] = "confirmed_alert"
    incident_id: str
    camera: CameraRef
    frame_id: str
    frame_uri: str
    frame_sha256: str
    threat_score: float = Field(..., ge=0.0, le=1.0)
    disposition: AlertDisposition
    primary_detection: Detection
    watchlist_hits: tuple[WatchlistHit, ...] = ()
    # Component signal breakdown for operator transparency + post-hoc calibration.
    signal_breakdown: dict[str, float]
    # Operator identity if confirmation came via manual gate; None on auto-confirm.
    confirming_operator_id: Optional[str] = None


class AuditAction(str, Enum):
    FRAME_CAPTURED = "frame_captured"
    DETECTION_EMITTED = "detection_emitted"
    ALERT_SCORED = "alert_scored"
    ALERT_AUTO_CONFIRMED = "alert_auto_confirmed"
    ALERT_QUEUED_FOR_REVIEW = "alert_queued_for_review"
    ALERT_SUPPRESSED = "alert_suppressed"
    OPERATOR_CONFIRMED = "operator_confirmed"
    OPERATOR_REJECTED = "operator_rejected"
    DISPATCH_SENT = "dispatch_sent"
    # Phase 2 — cross-camera identity.
    TRACK_ASSIGNED = "track_assigned"          # new global_track_id minted
    TRACK_CONTINUED = "track_continued"        # same identity, same camera
    CROSS_CAMERA_HANDOFF = "cross_camera_handoff"  # same identity, different camera


class AuditRecord(_Event):
    """Every state-changing action writes one of these BEFORE the action's
    downstream side-effects fire. Appended-only; no service may update or delete.
    """
    kind: Literal["audit"] = "audit"
    service: str
    action: AuditAction
    subject_id: str  # frame_id, incident_id, track_id — whatever the action is about
    correlation_ids: dict[str, str] = Field(default_factory=dict)
    actor_id: str = "system"  # human operator user id, or 'system'
    payload: dict[str, Any] = Field(default_factory=dict)
