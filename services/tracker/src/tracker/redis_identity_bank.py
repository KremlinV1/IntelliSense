"""Redis-backed identity bank.

Implements IdentityBankLike with the same semantics as InMemoryIdentityBank.
The difference is that state is shared across tracker instances — horizontal
scale within a Kafka consumer group.

Storage layout:
  identity:{gtid}             HASH   centroid_bytes, last_seen_camera,
                                      last_seen_ts, last_detection_id, num_sightings
  identity_recency            ZSET   score=last_seen_ts, member=gtid
                                      → O(log N) stale eviction + recency queries
  identity_embeddings_dim     STRING set on first write; enforces all
                                      embeddings share a dim (detection of
                                      model swap without a reindex)

Matching is still linear: we ZRANGEBYSCORE the recency set to get non-stale
gtids, then HMGET their centroid bytes and do np.dot in-process. At a few
hundred active identities this is <1ms/observe; at 10k+ we'd swap to
RediSearch HNSW (TODO Phase 2.5).

Atomicity: the observe()/match()/update() sequence is NOT wrapped in a
WATCH/MULTI/EXEC transaction. Race between two tracker instances can produce
a duplicate mint for the same identity arriving simultaneously on two
cameras. This is acceptable for Slice 3 — duplicates get merged by a
housekeeping job in Phase 2.5, or tolerated as cosmetic (two gtids for
one person is less bad than dropping a detection). If/when this matters,
the fix is a per-camera sharded consumer group so the same identity always
hits the same tracker instance.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import Optional

import numpy as np

from tracker.identity_bank import Identity, MatchOutcome

log = logging.getLogger(__name__)

_KEY_IDENTITY = "identity:{}"
_KEY_RECENCY = "identity_recency"
_KEY_DIM = "identity_embeddings_dim"


class RedisIdentityBank:
    def __init__(
        self,
        redis_client,  # redis.Redis-like (sync). Kept untyped to allow fakeredis in tests.
        match_threshold: float,
        recency_seconds: float,
        max_identities: int,
        centroid_alpha: float,
    ) -> None:
        if not 0.0 < centroid_alpha <= 1.0:
            raise ValueError("centroid_alpha must be in (0, 1]")
        self._r = redis_client
        self._threshold = match_threshold
        self._recency = recency_seconds
        self._max = max_identities
        self._alpha = centroid_alpha

    # --- IdentityBankLike ---------------------------------------------------

    def size(self) -> int:
        return int(self._r.zcard(_KEY_RECENCY))

    def observe(
        self,
        embedding: np.ndarray,
        camera_id: str,
        detection_id: str,
        now_ts: Optional[float] = None,
    ) -> MatchOutcome:
        if now_ts is None:
            now_ts = time.time()
        embedding = self._as_unit(embedding)

        # Enforce consistent embedding dim — if the model was swapped under
        # us, refuse to mix old and new embeddings (the geometry is invalid).
        self._assert_dim(embedding.shape[0])

        # Evict stale before matching so we don't match against a ghost.
        self._evict_stale(now_ts)

        best_gtid: Optional[str] = None
        best_sim = -1.0
        best_prior: Optional[Identity] = None

        candidate_gtids = self._live_gtids(now_ts)
        if candidate_gtids:
            # HMGET centroid_bytes for all in one pipeline call.
            pipe = self._r.pipeline()
            for gtid in candidate_gtids:
                pipe.hget(_KEY_IDENTITY.format(gtid), "centroid_bytes")
            centroid_blobs = pipe.execute()
            for gtid, blob in zip(candidate_gtids, centroid_blobs):
                if not blob:
                    continue
                centroid = np.frombuffer(blob, dtype=np.float32)
                if centroid.shape[0] != embedding.shape[0]:
                    continue
                sim = float(np.dot(centroid, embedding))
                if sim > best_sim:
                    best_sim = sim
                    best_gtid = gtid

        if best_gtid is not None and best_sim >= self._threshold:
            prior = self._load(best_gtid)
            if prior is None:
                # Race: identity expired between our scan and load. Fall
                # through to the mint path.
                pass
            else:
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
                self._save(updated)
                return MatchOutcome(
                    identity=updated,
                    disposition="handed_off" if handed_off else "continued",
                    similarity=best_sim,
                    prior_camera=prior.last_seen_camera if handed_off else None,
                    prior_detection_id=prior.last_detection_id if handed_off else None,
                    elapsed_seconds=max(0.0, now_ts - prior.last_seen_ts),
                )

        # No match → mint.
        gtid = str(uuid.uuid4())
        ident = Identity(
            global_track_id=gtid,
            centroid=embedding,
            last_seen_camera=camera_id,
            last_seen_ts=now_ts,
            last_detection_id=detection_id,
            num_sightings=1,
        )
        self._save(ident)
        self._enforce_cap()
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
        blended = (1.0 - self._alpha) * prior + self._alpha * observed
        n = float(np.linalg.norm(blended))
        return (blended / n).astype(np.float32) if n > 0 else observed

    def _assert_dim(self, dim: int) -> None:
        stored = self._r.get(_KEY_DIM)
        if stored is None:
            self._r.set(_KEY_DIM, dim)
            return
        expected = int(stored)
        if expected != dim:
            raise ValueError(
                f"embedding dim mismatch: bank holds dim={expected}, "
                f"observation has dim={dim}. A model swap requires a full reindex."
            )

    def _live_gtids(self, now_ts: float) -> list[str]:
        min_score = now_ts - self._recency
        raw = self._r.zrangebyscore(_KEY_RECENCY, min_score, "+inf")
        return [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in raw]

    def _load(self, gtid: str) -> Optional[Identity]:
        h = self._r.hgetall(_KEY_IDENTITY.format(gtid))
        if not h:
            return None
        # Keys can be bytes or str depending on decode_responses config.
        norm = {
            (k.decode() if isinstance(k, (bytes, bytearray)) else k):
            (v.decode() if isinstance(v, (bytes, bytearray)) and k != b"centroid_bytes"
             and k != "centroid_bytes" else v)
            for k, v in h.items()
        }
        centroid_bytes = norm["centroid_bytes"]
        if isinstance(centroid_bytes, str):
            # Shouldn't happen (we always write bytes), but be defensive.
            centroid_bytes = centroid_bytes.encode("latin-1")
        centroid = np.frombuffer(centroid_bytes, dtype=np.float32)
        return Identity(
            global_track_id=gtid,
            centroid=centroid,
            last_seen_camera=norm["last_seen_camera"],
            last_seen_ts=float(norm["last_seen_ts"]),
            last_detection_id=norm["last_detection_id"],
            num_sightings=int(norm["num_sightings"]),
        )

    def _save(self, ident: Identity) -> None:
        pipe = self._r.pipeline()
        pipe.hset(
            _KEY_IDENTITY.format(ident.global_track_id),
            mapping={
                "centroid_bytes": ident.centroid.astype(np.float32).tobytes(),
                "last_seen_camera": ident.last_seen_camera,
                "last_seen_ts": ident.last_seen_ts,
                "last_detection_id": ident.last_detection_id,
                "num_sightings": ident.num_sightings,
            },
        )
        pipe.zadd(_KEY_RECENCY, {ident.global_track_id: ident.last_seen_ts})
        pipe.execute()

    def _evict_stale(self, now_ts: float) -> None:
        cutoff = now_ts - self._recency
        stale = self._r.zrangebyscore(_KEY_RECENCY, "-inf", f"({cutoff}")
        if not stale:
            return
        stale_ids = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in stale]
        pipe = self._r.pipeline()
        for gtid in stale_ids:
            pipe.delete(_KEY_IDENTITY.format(gtid))
        pipe.zrem(_KEY_RECENCY, *stale_ids)
        pipe.execute()

    def _enforce_cap(self) -> None:
        size = int(self._r.zcard(_KEY_RECENCY))
        if size <= self._max:
            return
        excess = size - self._max
        # Oldest-first (lowest score) eviction.
        victims = self._r.zrange(_KEY_RECENCY, 0, excess - 1)
        if not victims:
            return
        victim_ids = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in victims]
        pipe = self._r.pipeline()
        for gtid in victim_ids:
            pipe.delete(_KEY_IDENTITY.format(gtid))
        pipe.zrem(_KEY_RECENCY, *victim_ids)
        pipe.execute()
