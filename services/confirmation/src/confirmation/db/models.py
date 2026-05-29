"""SQLAlchemy ORM models for incidents and cameras.

Schema notes:
  - `threat_score` is stored as NUMERIC(5,4) not FLOAT — calibration analysis
    later needs bit-exact round-trips, and floats lose precision at scale.
  - `disposition` is a VARCHAR with CHECK, not a Postgres ENUM: adding a new
    disposition value later is a migration-free operation with CHECK, and
    the .v1 contract promises only additive changes anyway.
  - `signal_breakdown` + `watchlist_hits` live in JSONB. Post-hoc queries
    like "show me every alert where corroboration > 0.3" are handled by
    jsonb_path_ops expression indexes added in Slice 3.
  - Cameras get a PostGIS GEOGRAPHY(Point) so "cameras within 500m of X"
    queries are cheap. We carry (lat, lon) columns too for trivial JSON
    serialisation — a trigger keeps them in sync in Slice 3.
  - In dev/test against SQLite the GEOGRAPHY column collapses to a string;
    code that actually uses ST_ functions is routed through raw SQL so the
    SQLAlchemy layer never needs PostGIS at the type level.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base. Using the 2.0-style typed API."""


# JSON column type. Uses native JSONB on Postgres (supports GIN indexes for
# jsonb_path_ops operator queries) and falls back to generic JSON on other
# dialects — SQLAlchemy handles the dict <-> json-text serialisation for
# SQLite automatically via the JSON type.
def _json_col() -> Any:
    return JSON().with_variant(JSONB(), "postgresql")


class Camera(Base):
    __tablename__ = "cameras"

    # camera_id is the business key — referenced by every event. We keep it
    # as the primary key rather than a surrogate int so joins against the
    # event stream stay lexical (debuggable in logs).
    camera_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    environment: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="public",
    )
    latitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    # Operator-controllable: disabled cameras continue to exist so historical
    # incidents can still resolve the join, but new alerts shouldn't route
    # to them (enforced by the capture service on next config reload).
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    rtsp_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "environment IN ('road', 'retail', 'hospitality', 'residential', 'public')",
            name="ck_cameras_environment",
        ),
        CheckConstraint(
            "latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180",
            name="ck_cameras_geo_bounds",
        ),
    )


class Incident(Base):
    __tablename__ = "incidents"

    # incident_id is provided by the confirmation pipeline (UUIDv4). We do NOT
    # generate it here — the id is the stable correlation key for the whole
    # downstream graph (dispatch, audit, operator notes).
    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cameras.camera_id", ondelete="RESTRICT"),
        nullable=False,
    )
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    threat_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    # Primary detection flattened for cheap display queries.
    primary_class_name: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_threat_class: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)

    # Full fidelity lives here. Operator UI can hydrate the card from the
    # flattened columns and only fetch the blob on demand (not yet wired).
    signal_breakdown: Mapped[dict] = mapped_column(_json_col(), nullable=False)
    watchlist_hits: Mapped[list] = mapped_column(_json_col(), nullable=False, default=list)
    frame_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    frame_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    confirming_operator_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "disposition IN ('auto_confirmed', 'operator_review', "
            "'operator_dismissed', 'suppressed')",
            name="ck_incidents_disposition",
        ),
        CheckConstraint(
            "threat_score BETWEEN 0 AND 1",
            name="ck_incidents_score_range",
        ),
        # Operator dashboard's primary sort = emitted_at DESC. This index
        # makes the "recent alerts" and date-range search both fast.
        Index("ix_incidents_emitted_at_desc", "emitted_at"),
        Index("ix_incidents_camera_emitted", "camera_id", "emitted_at"),
        Index("ix_incidents_disposition", "disposition"),
    )
