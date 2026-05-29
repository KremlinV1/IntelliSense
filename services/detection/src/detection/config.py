from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DetectionConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    yolo_weights: str = Field(alias="YOLO_WEIGHTS", default="yolov8n.pt")
    yolo_device: str = Field(alias="YOLO_DEVICE", default="cpu")
    yolo_conf_threshold: float = Field(alias="YOLO_CONF_THRESHOLD", default=0.35)

    watchlist_seed_path: str = Field(
        alias="WATCHLIST_SEED_PATH", default="/etc/veilwatch/watchlist_seed.json"
    )
    # Cosine similarity threshold above which we consider a face/re-id embedding
    # to be a watchlist hit. Tuned per deployment with an ROC sweep on held-out data.
    watchlist_match_threshold: float = Field(
        alias="WATCHLIST_MATCH_THRESHOLD", default=0.62
    )

    # ALPR (Phase 1.5).
    alpr_backend: str = Field(alias="ALPR_BACKEND", default="mock")
    # Minimum per-read confidence below which an ALPR result is discarded
    # rather than emitted. Guards against low-light / motion-blur misreads.
    alpr_min_confidence: float = Field(alias="ALPR_MIN_CONFIDENCE", default=0.55)

    # Re-identification (Phase 2). Produces the embedding attached to each
    # person Detection so the tracker can maintain cross-camera identity.
    reid_backend: str = Field(alias="REID_BACKEND", default="mock")
    reid_onnx_model_path: str = Field(
        alias="REID_ONNX_MODEL_PATH", default="/etc/veilwatch/osnet.onnx"
    )

    # Consumer group stability: changing this resets the offset — treat as a deploy-time decision.
    consumer_group: str = "detection-v1"
