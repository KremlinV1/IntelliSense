from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TrackerConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Cosine similarity threshold above which two embeddings are considered
    # the same identity. Tune with an ROC sweep once the real OSNet model is
    # in place — the mock backend's embedding geometry isn't representative.
    match_threshold: float = Field(alias="TRACKER_MATCH_THRESHOLD", default=0.72)

    # How long an identity remains in the bank after its last sighting. Past
    # this point, a re-appearance mints a NEW global_track_id. Bounds memory
    # and prevents long-gap confabulation (someone who was in the frame
    # yesterday must not be silently re-identified today).
    recency_seconds: float = Field(alias="TRACKER_RECENCY_SECONDS", default=300.0)

    # Upper bound on the identity bank. LRU eviction when exceeded. Slice 3
    # replaces this in-memory store with Redis keyed by global_track_id.
    max_identities: int = Field(alias="TRACKER_MAX_IDENTITIES", default=10_000)

    # Running-average weight for updating an identity's centroid embedding
    # when a new sighting matches it. Higher = more responsive to appearance
    # changes (lighting, pose); lower = more stable long-term identity.
    centroid_alpha: float = Field(alias="TRACKER_CENTROID_ALPHA", default=0.2)

    consumer_group: str = Field(alias="TRACKER_CONSUMER_GROUP", default="tracker-v1")

    # Slice 3: identity bank storage backend. 'memory' = single-instance
    # in-process (default, good for POC + unit tests); 'redis' = shared across
    # tracker instances for horizontal scale.
    bank_backend: str = Field(alias="TRACKER_BANK_BACKEND", default="memory")
    redis_url: str = Field(alias="REDIS_URL", default="redis://redis:6379/0")
