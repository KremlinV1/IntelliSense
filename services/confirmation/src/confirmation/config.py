from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfirmationConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    autoconfirm_threshold: float = Field(
        alias="CONFIRMATION_AUTOCONFIRM_THRESHOLD", default=0.85
    )
    operator_review_threshold: float = Field(
        alias="CONFIRMATION_OPERATOR_REVIEW_THRESHOLD", default=0.55
    )
    # How long a detection counts towards the 'persistence' signal for its track.
    track_window_seconds: int = Field(
        alias="CONFIRMATION_TRACK_WINDOW_SECONDS", default=30
    )
    # Cross-camera corroboration target: an identity seen on this many distinct
    # cameras within the tracker's recency window saturates the corroboration
    # signal. Matches the 4-camera Phase 2 test network (3 of 4 is strong).
    corroboration_target_cameras: int = Field(
        alias="CONFIRMATION_CORROBORATION_TARGET_CAMERAS", default=3
    )
    # Must be ≥ the tracker's recency_seconds or the identity view will expire
    # identities that the tracker still holds, producing stale Nones. Default
    # matches TRACKER_RECENCY_SECONDS=300 in the tracker config.
    identity_recency_seconds: float = Field(
        alias="CONFIRMATION_IDENTITY_RECENCY_SECONDS", default=300.0
    )
    http_port: int = Field(alias="CONFIRMATION_HTTP_PORT", default=8080)
    consumer_group: str = "confirmation-v1"

    redis_url: str = Field(alias="REDIS_URL", default="redis://redis:6379/0")
