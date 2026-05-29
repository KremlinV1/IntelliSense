"""Automatic Licence Plate Recognition engine.

Architecture:
    YOLO produces vehicle detections → ALPR runs on each vehicle crop → emits
    zero or more PlateRead objects per crop. Backend is pluggable behind the
    `AlprBackend` protocol so we can swap OpenALPR for Plate Recognizer or a
    fine-tuned PaddleOCR pipeline without touching the orchestrator.

Backends shipped in Phase 1.5:
  - MockAlprBackend: deterministic pseudo-plate from crop hash. Default in POC
    so the pipeline runs on any machine and tests are reproducible.
  - OpenAlprSubprocessBackend: shells out to the `alpr` CLI binary and parses
    its JSON output. Used when the container image bundles OpenALPR (`ALPR_BACKEND=openalpr`).

Latency budget: ALPR must complete within ~80ms per vehicle crop on CPU to
keep the full detection stage under the 200ms target. The subprocess backend
is fork-cost-heavy; production deploys should replace it with a long-running
gRPC ALPR sidecar or the DeepStream plugin. Flagged as a TODO for Phase 2.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Valid canonical plate characters after normalisation — uppercase alphanumeric only.
_PLATE_CHAR_RE = re.compile(r"[A-Z0-9]")


def normalise_plate(raw: str) -> str:
    """Uppercase, strip whitespace and punctuation, keep only A-Z0-9.

    Two different OCR outputs of the same physical plate ('ABC 123', 'abc-123',
    'ABC  123 ') must collapse to the same canonical form 'ABC123' for
    watchlist matching to work. This is the single place that definition lives.
    """
    up = raw.upper()
    return "".join(ch for ch in up if _PLATE_CHAR_RE.match(ch))


@dataclass(frozen=True)
class AlprResult:
    plate_text: str
    raw_text: str
    confidence: float
    region: str = "unknown"


class AlprBackend(Protocol):
    def read(self, image_crop: np.ndarray) -> list[AlprResult]:
        """Return zero or more candidate reads for a single vehicle crop,
        ordered by confidence descending. Sync — caller offloads to thread pool."""
        ...


class MockAlprBackend:
    """POC-only. Generates a deterministic plate string from the crop hash.

    The resulting text is fake but stable: the same crop always maps to the
    same plate, which means test fixtures can seed the plate watchlist with
    the expected output of hashing a known test image. Calibrated to produce
    strings in the common 3-letter-3-digit shape for visual plausibility.
    """

    def __init__(self, base_confidence: float = 0.78) -> None:
        self._conf = base_confidence

    def read(self, image_crop: np.ndarray) -> list[AlprResult]:
        if image_crop.size == 0 or min(image_crop.shape[:2]) < 16:
            # Crop too small to read plausibly. Low-light misread guard — return
            # nothing rather than emit a false plate.
            return []
        # Downsample to decorrelate from tiny pixel-level noise so a jittery
        # bbox on the same car still produces the same plate.
        small = cv2.resize(image_crop, (32, 32))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        digest = hashlib.sha256(gray.tobytes()).hexdigest().upper()
        letters = "".join(c for c in digest if c.isalpha())[:3].ljust(3, "A")
        digits = "".join(c for c in digest if c.isdigit())[:3].ljust(3, "0")
        raw = f"{letters} {digits}"
        return [AlprResult(
            plate_text=normalise_plate(raw),
            raw_text=raw,
            confidence=self._conf,
            region="unknown",
        )]


class OpenAlprSubprocessBackend:
    """Shells out to the `alpr` binary (https://github.com/openalpr/openalpr).

    Expects the binary on PATH. Writes the crop to a named temp file and
    invokes `alpr -j -n 3 <path>`; parses the results list. Returns empty on
    non-zero exit, timeout, or unparseable output — NEVER raises. A
    misbehaving ALPR must not take down the detection loop.
    """

    def __init__(self, region_hint: str = "us", timeout_s: float = 2.0, max_candidates: int = 3) -> None:
        self._region = region_hint
        self._timeout = timeout_s
        self._n = max_candidates
        if shutil.which("alpr") is None:
            log.warning("openalpr binary not found on PATH; backend will return no reads")

    def read(self, image_crop: np.ndarray) -> list[AlprResult]:
        if shutil.which("alpr") is None or image_crop.size == 0:
            return []
        # Tempfile written in-memory via OpenCV's imencode → subprocess stdin is
        # cleaner but alpr's CLI wants a path. Use a NamedTemporaryFile.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            ok, buf = cv2.imencode(".jpg", image_crop)
            if not ok:
                return []
            tmp.write(buf.tobytes())
            tmp.flush()
            try:
                proc = subprocess.run(
                    ["alpr", "-j", "-c", self._region, "-n", str(self._n), tmp.name],
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.warning("openalpr timeout", extra={"timeout_s": self._timeout})
                return []
            if proc.returncode != 0:
                log.warning("openalpr nonzero exit", extra={"rc": proc.returncode,
                                                            "stderr": proc.stderr[:200].decode(errors="replace")})
                return []
            try:
                data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return []
        out: list[AlprResult] = []
        for result in data.get("results", []):
            for candidate in result.get("candidates", [])[: self._n]:
                raw = str(candidate.get("plate", ""))
                # OpenALPR confidence is 0-100; renormalise to [0, 1].
                conf = float(candidate.get("confidence", 0.0)) / 100.0
                conf = max(0.0, min(1.0, conf))
                if not raw:
                    continue
                out.append(AlprResult(
                    plate_text=normalise_plate(raw),
                    raw_text=raw,
                    confidence=conf,
                    region=str(result.get("region") or self._region),
                ))
        return out


def build_backend(name: str) -> AlprBackend:
    if name == "mock":
        return MockAlprBackend()
    if name == "openalpr":
        return OpenAlprSubprocessBackend()
    raise ValueError(f"unknown ALPR_BACKEND: {name!r}")


class AlprEngine:
    """Thin orchestrator: applies confidence gating and returns the top read
    per crop. Multi-frame consensus (same plate across N consecutive frames
    to suppress single-frame OCR noise) lives in the confirmation track
    store, not here — this layer is stateless by design."""

    def __init__(self, backend: AlprBackend, min_confidence: float) -> None:
        self._backend = backend
        self._min_conf = min_confidence

    def read_best(self, image_crop: np.ndarray) -> AlprResult | None:
        candidates = self._backend.read(image_crop)
        if not candidates:
            return None
        best = max(candidates, key=lambda c: c.confidence)
        if best.confidence < self._min_conf:
            return None
        return best
