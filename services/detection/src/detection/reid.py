"""Re-identification (re-ID) embedding engine — Phase 2.

Produces a unit-normalised embedding vector for a person crop. The vector is
attached to the `Detection` and consumed by the `tracker` service to maintain
persistent identities across cameras.

Backend strategy (same as ALPR): pluggable behind a Protocol so the real
OSNet-x1_0 ONNX model can be swapped in without touching the orchestrator.

Backends in Slice 1:
  - MockReidBackend: deterministic pseudo-embedding from the crop's perceptual
    hash. Default. Same identity (same crop content) → same embedding;
    different identities → near-orthogonal vectors. Enough to exercise the
    tracker's cosine-NN search and make cross-camera test scenarios
    reproducible without a GPU.
  - OnnxReidBackend: loads an OSNet ONNX model via onnxruntime. Expected path
    defaults to `/etc/veilwatch/osnet.onnx`. If onnxruntime isn't installed or
    the model file is missing, construction RAISES — we refuse to silently
    degrade to zero-vectors because that would make every identity look the
    same to the tracker, which is worse than no tracking at all.

Latency budget: re-ID must add < 30ms per person crop on CPU to keep the
detection stage under the 200ms target alongside YOLO + ALPR. ONNX OSNet-x0_25
hits this comfortably; x1_0 needs GPU or batched inference (Phase 2.5).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Dimensionality used by the mock backend. The real OSNet ONNX model determines
# its own dim at load time — consumers must not assume a fixed size.
MOCK_EMBEDDING_DIM = 512


@dataclass(frozen=True)
class ReidResult:
    embedding: np.ndarray  # float32, unit-norm, shape (dim,)


class ReidBackend(Protocol):
    def embed(self, image_crop: np.ndarray) -> ReidResult | None:
        """Return a unit-norm embedding, or None if the crop is unusable."""
        ...


def _unit_normalise(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n <= 0.0:
        # Degenerate crop (all zeros) — return None upstream rather than an
        # undefined unit vector. Caller checks for None and skips.
        return v
    return (v / n).astype(np.float32)


class MockReidBackend:
    """Deterministic pseudo-embedding for POC / test.

    We downsample the crop to 48x96 (the standard person-crop aspect ratio used
    by OSNet), hash the pixels with SHA-256, and tile the hash to 512 bytes
    which are mapped into [-1, 1] floats and unit-normalised. Two crops of the
    same person (and therefore very similar pixel content) hash to nearby —
    but not identical — values; in practice hashing is too sensitive to pixel
    perturbations for real cross-frame matching, but it IS sufficient for:
      - reproducible test fixtures (same crop → same embedding)
      - distinguishing different people (different pixels → different vectors)
    which is what Slice 1 needs. The real OSNet model replaces this.
    """

    def embed(self, image_crop: np.ndarray) -> ReidResult | None:
        if image_crop.size == 0 or min(image_crop.shape[:2]) < 16:
            return None
        resized = cv2.resize(image_crop, (48, 96))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY) if resized.ndim == 3 else resized
        # Blur to absorb single-pixel jitter between frames — without this,
        # same-person-next-frame embeddings decorrelate too aggressively.
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        digest = hashlib.sha256(gray.tobytes()).digest()
        # Tile 32 bytes → 512 bytes, map to [-1, 1], unit-normalise.
        tiled = np.frombuffer(digest * (MOCK_EMBEDDING_DIM // len(digest) + 1),
                              dtype=np.uint8)[:MOCK_EMBEDDING_DIM].astype(np.float32)
        vec = (tiled / 127.5) - 1.0
        unit = _unit_normalise(vec)
        if unit is vec:  # degenerate
            return None
        return ReidResult(embedding=unit)


class OnnxReidBackend:
    """Production re-ID via OSNet exported to ONNX.

    Load-time contract: the model file MUST exist at `model_path` AND the
    onnxruntime package MUST be importable. Either failure raises at
    construction — we never silently degrade to meaningless embeddings.
    Expected input: NCHW float32, (1, 3, 256, 128), BGR normalised to [0, 1].
    Expected output: (1, dim) float32. Unit-normalised here regardless of
    whether the model was trained with normalisation built in.
    """

    def __init__(self, model_path: str = "/etc/veilwatch/osnet.onnx") -> None:
        try:
            import onnxruntime as ort  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "OnnxReidBackend requires `onnxruntime`. Install it, or set "
                "REID_BACKEND=mock if you don't need real re-identification."
            ) from e
        if not os.path.exists(model_path):
            raise RuntimeError(
                f"OnnxReidBackend: model file not found at {model_path!r}. "
                "Export OSNet to ONNX and vendor it in the image, or set "
                "REID_BACKEND=mock."
            )
        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        log.info("onnx re-id model loaded", extra={"path": model_path})

    def embed(self, image_crop: np.ndarray) -> ReidResult | None:
        if image_crop.size == 0 or min(image_crop.shape[:2]) < 16:
            return None
        # OSNet input: 256H x 128W, normalised to [0, 1], NCHW float32.
        resized = cv2.resize(image_crop, (128, 256))
        arr = resized.astype(np.float32) / 255.0
        chw = np.transpose(arr, (2, 0, 1))[np.newaxis, ...]
        outputs = self._session.run(None, {self._input_name: chw})
        vec = np.asarray(outputs[0]).flatten().astype(np.float32)
        unit = _unit_normalise(vec)
        if unit is vec:
            return None
        return ReidResult(embedding=unit)


def build_reid_backend(name: str, onnx_model_path: str | None = None) -> ReidBackend:
    if name == "mock":
        return MockReidBackend()
    if name == "onnx":
        return OnnxReidBackend(onnx_model_path or "/etc/veilwatch/osnet.onnx")
    raise ValueError(f"unknown REID_BACKEND: {name!r}")


class ReidEngine:
    """Thin orchestrator. Kept for symmetry with AlprEngine and so future
    cross-crop batching (a real Phase 2.5 optimisation) has a natural home."""

    def __init__(self, backend: ReidBackend) -> None:
        self._backend = backend

    def embed(self, image_crop: np.ndarray) -> tuple[float, ...] | None:
        result = self._backend.embed(image_crop)
        if result is None:
            return None
        return tuple(float(x) for x in result.embedding)
