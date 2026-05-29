"""In-memory global identity bank for cross-camera person tracking.

Data model:
    identity: {
        global_track_id: UUID
        centroid: unit-norm embedding (exponential running average)
        last_seen_camera: camera_id
        last_seen_ts: wall clock
        last_detection_id: the detection that produced the most recent update
        num_sightings: total sightings so far
    }

Matching: cosine similarity against all live identities (N linear for Slice 1).
Slice 3 migrates this to a FAISS IndexFlatIP with Redis for cross-instance
sharing. The Python-level interface here is intentionally narrow so that
swap is a one-file change.

Eviction: any identity whose `last_seen_ts` is older than `recency_seconds` is
evicted lazily when we fail to find a match (avoiding periodic GC). An
additional LRU cap at `max_identities` protects against pathological growth.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Literal, Optional, Protocol

import numpy as np


# Publicly-exposed structural type that both InMemory and Redis-backed banks
# implement. Tracker.main depends on this, not on a concrete class.
class IdentityBankLike(Protocol):
    def observe(
        self,
        embedding: np.ndarray,
        camera_id: str,
        detection_id: str,
        now_ts: Optional[float] = None,
    ) -> "MatchOutcome": ...
    def size(self) -> int: ...


@dataclass(frozen=True)
class Identity:
    global_track_id: str
    centroid: np.ndarray            # unit-norm float32 (dim,)
    last_seen_camera: str
    last_seen_ts: float
    last_detection_id: str
    num_sightings: int


@dataclass(frozen=True)
class MatchOutcome:
    identity: Identity
    disposition: Literal["new", "continued", "handed_off"]
    similarity: float               # 1.0 for 'new' (no comparison)
    prior_camera: Optional[str]     # populated only when disposition == "handed_off"
    prior_detection_id: Optional[str]
    elapsed_seconds: float          # 0.0 for 'new'


class IdentityBank:
    def __init__(
        self,
        match_threshold: float,
        recency_seconds: float,
        max_identities: int,
        centroid_alpha: float,
    ) -> None:
        if not 0.0 < centroid_alpha <= 1.0:
            raise ValueError("centroid_alpha must be in (0, 1]")
        self._threshold = match_threshold
        self._recency = recency_seconds
        self._max = max_identities
        self._alpha = centroid_alpha
        self._identities: dict[str, Identity] = {}

    # --- Introspection (tests + operator diagnostics) -----------------------

    def __len__(self) -> int:
        return len(self._identities)

    def size(self) -> int:
        return len(self._identities)

    def get(self, gtid: str) -> Optional[Identity]:
        return self._identities.get(gtid)

    # --- Core ---------------------------------------------------------------

    def observe(
        self,
        embedding: np.ndarray,
        camera_id: str,
        detection_id: str,
        now_ts: Optional[float] = None,
    ) -> MatchOutcome:
        """Match `embedding` to an existing identity (if within threshold and
        recency window), or mint a new one. Returns a MatchOutcome describing
        what happened — the caller is responsible for emitting the correct
        downstream events and audit records in the correct order.

        NOTE: this method mutates internal state BEFORE returning. Callers
        MUST audit the disposition before publishing any TrackUpdate /
        CrossCameraMatch, per Constraint #3. The 'audit precedes side-effect'
        invariant is enforced at the service layer, not here.
        """
        if now_ts is None:
            now_ts = time.time()
        embedding = self._as_unit(embedding)

        self._evict_stale(now_ts)

        best_gtid: Optional[str] = None
        best_sim = -1.0
        for gtid, ident in self._identities.items():
            # np.dot of two unit vectors = cosine similarity.
            sim = float(np.dot(ident.centroid, embedding))
            if sim > best_sim:
                best_sim = sim
                best_gtid = gtid

        if best_gtid is not None and best_sim >= self._threshold:
            prior = self._identities[best_gtid]
            handed_off = prior.last_seen_camera != camera_id
            updated_centroid = self._update_centroid(prior.centroid, embedding)
            updated = replace(
                prior,
                centroid=updated_centroid,
                last_seen_camera=camera_id,
                last_seen_ts=now_ts,
                last_detection_id=detection_id,
                num_sightings=prior.num_sightings + 1,
            )
            self._identities[best_gtid] = updated
            return MatchOutcome(
                identity=updated,
                disposition="handed_off" if handed_off else "continued",
                similarity=best_sim,
                prior_camera=prior.last_seen_camera if handed_off else None,
                prior_detection_id=prior.last_detection_id if handed_off else None,
                elapsed_seconds=max(0.0, now_ts - prior.last_seen_ts),
            )

        # No match → mint new identity.
        gtid = str(uuid.uuid4())
        ident = Identity(
            global_track_id=gtid,
            centroid=embedding,
            last_seen_camera=camera_id,
            last_seen_ts=now_ts,
            last_detection_id=detection_id,
            num_sightings=1,
        )
        self._identities[gtid] = ident
        self._enforce_cap(now_ts)
        return MatchOutcome(
            identity=ident,
            disposition="new",
            similarity=1.0,
            prior_camera=None,
            prior_detection_id=None,
            elapsed_seconds=0.0,
        )

    # --- Internals ----------------------------------------------------------

    @staticmethod
    def _as_unit(v: np.ndarray) -> np.ndarray:
        v = np.asarray(v, dtype=np.float32).flatten()
        n = float(np.linalg.norm(v))
        if n <= 0.0:
            raise ValueError("cannot normalise zero-length embedding")
        return (v / n).astype(np.float32)

    def _update_centroid(self, prior: np.ndarray, observed: np.ndarray) -> np.ndarray:
        """Exponential running average, renormalised. The renormalisation is
        important: a simple weighted sum of two unit vectors isn't unit-norm,
        which would silently break the cosine==dot assumption downstream."""
        blended = (1.0 - self._alpha) * prior + self._alpha * observed
        n = float(np.linalg.norm(blended))
        return (blended / n).astype(np.float32) if n > 0 else observed

    def _evict_stale(self, now_ts: float) -> None:
        stale = [g for g, i in self._identities.items()
                 if (now_ts - i.last_seen_ts) > self._recency]
        for g in stale:
            del self._identities[g]

    def _enforce_cap(self, now_ts: float) -> None:
        if len(self._identities) <= self._max:
            return
        # Evict oldest-seen first.
        ordered = sorted(self._identities.items(), key=lambda kv: kv[1].last_seen_ts)
        excess = len(self._identities) - self._max
        for g, _ in ordered[:excess]:
            del self._identities[g]
