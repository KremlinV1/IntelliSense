from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CaptureConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    camera_id: str = Field(alias="CAPTURE_CAMERA_ID")
    camera_lat: float = Field(alias="CAPTURE_CAMERA_LAT", default=0.0)
    camera_lon: float = Field(alias="CAPTURE_CAMERA_LON", default=0.0)
    camera_environment: str = Field(alias="CAPTURE_CAMERA_ENV", default="public")

    rtsp_url: str = Field(alias="RTSP_URL")
    target_fps: float = Field(alias="CAPTURE_TARGET_FPS", default=5.0)
    jpeg_quality: int = Field(alias="CAPTURE_JPEG_QUALITY", default=85)

    frame_store_backend: str = Field(alias="FRAME_STORE_BACKEND", default="local")
    frame_store_local_path: str = Field(
        alias="FRAME_STORE_LOCAL_PATH", default="/var/veilwatch/frames"
    )

    # Reconnect backoff bounds in seconds — capped so a dropped camera
    # doesn't silently stall for an hour waiting for exponential backoff.
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
