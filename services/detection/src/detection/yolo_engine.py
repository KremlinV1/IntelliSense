"""YOLOv8 wrapper.

Phase 1 uses the stock `yolov8n.pt` weights. Phase 2 swaps in fine-tuned
weights per deployment (retail shoplifting, road ALPR, residential intrusion).
The class names from COCO are mapped to VeilWatch `ThreatClass` enum values
here so upstream services don't have to know about COCO.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from ultralytics import YOLO

from veilwatch_contracts.events import ThreatClass

log = logging.getLogger(__name__)


# COCO-class → VeilWatch threat class. Anything not in this map → ThreatClass.UNKNOWN.
_COCO_TO_THREAT: dict[str, ThreatClass] = {
    "person": ThreatClass.PERSON,
    "bicycle": ThreatClass.VEHICLE,
    "car": ThreatClass.VEHICLE,
    "motorcycle": ThreatClass.VEHICLE,
    "bus": ThreatClass.VEHICLE,
    "truck": ThreatClass.VEHICLE,
    "knife": ThreatClass.WEAPON,
}


@dataclass(frozen=True)
class RawDetection:
    class_name: str
    threat_class: ThreatClass
    confidence: float
    xyxy: tuple[float, float, float, float]


class YoloEngine:
    def __init__(self, weights: str, device: str, conf_threshold: float) -> None:
        log.info("loading yolo", extra={"weights": weights, "device": device})
        self._model = YOLO(weights)
        self._device = device
        self._conf = conf_threshold

    def infer(self, image: np.ndarray) -> list[RawDetection]:
        """Synchronous inference. Caller must offload to a thread pool to keep
        the async event loop responsive."""
        results = self._model.predict(
            image,
            device=self._device,
            conf=self._conf,
            verbose=False,
        )
        out: list[RawDetection] = []
        if not results:
            return out
        r = results[0]
        names = r.names  # type: ignore[attr-defined]
        if r.boxes is None:
            return out
        for box in r.boxes:
            cls_id = int(box.cls.item())
            class_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else names[cls_id]
            conf = float(box.conf.item())
            xy = box.xyxy.cpu().numpy().flatten().tolist()
            out.append(RawDetection(
                class_name=class_name,
                threat_class=_COCO_TO_THREAT.get(class_name, ThreatClass.UNKNOWN),
                confidence=conf,
                xyxy=(float(xy[0]), float(xy[1]), float(xy[2]), float(xy[3])),
            ))
        return out


def crop(image: np.ndarray, xyxy: Iterable[float]) -> np.ndarray:
    """Safely crop a bounding box out of an image, clamped to image bounds.
    Used for watchlist embedding extraction."""
    x1, y1, x2, y2 = [int(round(v)) for v in xyxy]
    h, w = image.shape[:2]
    x1 = max(0, min(w - 1, x1))
    y1 = max(0, min(h - 1, y1))
    x2 = max(x1 + 1, min(w, x2))
    y2 = max(y1 + 1, min(h, y2))
    return image[y1:y2, x1:x2]
