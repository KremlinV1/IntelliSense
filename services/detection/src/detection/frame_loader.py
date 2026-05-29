"""Load a frame given the URI contained in a FrameEvent.

For Phase 1 the only scheme is `file://`. Phase 8 adds `s3://` and `minio://`."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np


def load_frame(frame_uri: str) -> np.ndarray:
    parsed = urlparse(frame_uri)
    if parsed.scheme != "file":
        raise NotImplementedError(f"frame URI scheme '{parsed.scheme}' not supported yet")
    path = Path(parsed.path)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"could not decode frame at {path}")
    return image
