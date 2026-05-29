"""FAISS-backed face/re-ID watchlist matcher + exact-match plate watchlist.

Phase 1 STUB. A real VeilWatch watchlist carries:
  - 512-d face embeddings (ArcFace) per identity
  - 512-d OSNet re-ID embeddings per identity
  - associated plate strings for plate matches
Phase 2 replaces the `_image_to_embedding` function with a real ArcFace/OSNet
inference path. Until then, we derive a deterministic pseudo-embedding from the
crop's perceptual hash so the pipeline plumbing is exercisable end-to-end.

The index itself is real FAISS — the same code swaps in once embeddings are
real. That's the whole point of putting this behind a clean interface now.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import faiss  # type: ignore
import numpy as np

log = logging.getLogger(__name__)

EMBEDDING_DIM = 512


@dataclass(frozen=True)
class Identity:
    identity_id: str
    label: str


@dataclass(frozen=True)
class WatchlistMatch:
    identity: Identity
    similarity: float  # cosine similarity in [0, 1]


def _image_to_embedding(image: np.ndarray) -> np.ndarray:
    """POC-only: deterministic pseudo-embedding derived from the crop.

    We resize to 32x32 grayscale, SHA-256 the bytes, and expand the hash into a
    512-float vector normalised to unit length. This is NOT a real face
    embedding — but it IS a function that:
      - returns the same vector for the same crop (so seed identities match themselves)
      - returns different vectors for different crops (so the matcher isn't a no-op)
    which is enough to exercise the FAISS wiring until Phase 2 plugs in ArcFace.
    """
    small = cv2.resize(image, (32, 32))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    digest = hashlib.sha256(gray.tobytes()).digest()
    # Expand 32 hash bytes into 512 floats by tiling then scaling to [-1, 1].
    raw = np.frombuffer(digest * 16, dtype=np.uint8)[:EMBEDDING_DIM].astype(np.float32)
    vec = (raw / 127.5) - 1.0
    n = np.linalg.norm(vec)
    return vec / n if n > 0 else vec


def embedding_from_label(label: str) -> np.ndarray:
    """Seed identities don't have a reference photo in the POC. Derive a
    deterministic embedding from the label so the same person-id always maps
    to the same vector, and a crop matching that label's hash matches."""
    # We use a sha256 of a fixed synthetic 32x32 gray image seeded by label,
    # so `_image_to_embedding` applied to that same synthetic image returns
    # the same embedding. This makes test fixtures reproducible.
    rng = np.random.default_rng(int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big"))
    synthetic = (rng.integers(0, 256, size=(32, 32), dtype=np.uint8))
    digest = hashlib.sha256(synthetic.tobytes()).digest()
    raw = np.frombuffer(digest * 16, dtype=np.uint8)[:EMBEDDING_DIM].astype(np.float32)
    vec = (raw / 127.5) - 1.0
    n = np.linalg.norm(vec)
    return vec / n if n > 0 else vec


class Watchlist:
    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        # Inner-product index on L2-normalised vectors = cosine similarity.
        self._index = faiss.IndexFlatIP(EMBEDDING_DIM)
        self._identities: list[Identity] = []

    def load_seed(self, seed_path: str) -> None:
        p = Path(seed_path)
        if not p.exists():
            log.warning("watchlist seed not found; matcher will return no hits",
                        extra={"path": seed_path})
            return
        seed = json.loads(p.read_text())
        vectors: list[np.ndarray] = []
        for entry in seed.get("identities", []):
            ident = Identity(identity_id=entry["identity_id"], label=entry["label"])
            self._identities.append(ident)
            vectors.append(embedding_from_label(ident.label))
        if vectors:
            mat = np.stack(vectors).astype(np.float32)
            self._index.add(mat)
        log.info("watchlist loaded",
                 extra={"identities": len(self._identities), "path": seed_path})

    def match(self, image_crop: np.ndarray) -> Optional[WatchlistMatch]:
        if self._index.ntotal == 0:
            return None
        vec = _image_to_embedding(image_crop).reshape(1, -1).astype(np.float32)
        scores, idxs = self._index.search(vec, k=1)
        score = float(scores[0][0])
        idx = int(idxs[0][0])
        if idx < 0:
            return None
        # Inner product of unit vectors is cosine similarity in [-1, 1].
        # Map to [0, 1] for downstream scoring.
        sim = max(0.0, (score + 1.0) / 2.0)
        if sim < self._threshold:
            return None
        return WatchlistMatch(identity=self._identities[idx], similarity=sim)


class PlateWatchlist:
    """Exact-match plate watchlist.

    Plates are canonicalised with `normalise_plate` on both sides (input OCR
    reading and seed entry) so formatting differences ('ABC 123', 'ABC-123',
    'abc123') collapse to one canonical key. Matches are boolean-exact;
    similarity=1.0 for a hit, nothing for a miss. Fuzzy edit-distance matching
    to absorb single-character OCR errors is a Phase 2 upgrade — it needs
    calibration data from the operator review queue to pick a safe threshold.
    """

    def __init__(self) -> None:
        self._plates: dict[str, Identity] = {}

    def load_seed(self, seed_path: str) -> None:
        from detection.alpr import normalise_plate  # local import avoids cycle
        p = Path(seed_path)
        if not p.exists():
            log.warning("plate watchlist seed not found; plate matcher disabled",
                        extra={"path": seed_path})
            return
        seed = json.loads(p.read_text())
        for entry in seed.get("plate_identities", []):
            canonical = normalise_plate(entry["plate_text"])
            if not canonical:
                log.warning("skipping plate seed with empty canonical form",
                            extra={"entry": entry})
                continue
            self._plates[canonical] = Identity(
                identity_id=entry["identity_id"],
                label=entry["label"],
            )
        log.info("plate watchlist loaded",
                 extra={"plates": len(self._plates), "path": seed_path})

    def match(self, canonical_plate_text: str) -> Optional[WatchlistMatch]:
        """Caller MUST have already canonicalised the input."""
        if not canonical_plate_text:
            return None
        ident = self._plates.get(canonical_plate_text)
        if ident is None:
            return None
        return WatchlistMatch(identity=ident, similarity=1.0)
