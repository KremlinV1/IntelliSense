"""Redis-backed identity bank — semantic parity with the in-memory bank.

We reuse the exact same scenarios as test_tracker.py (new / continued /
handed_off / recency / cap) but against a fakeredis backend. If both banks
implement IdentityBankLike correctly, the test bodies should be near-identical.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("fakeredis")
import fakeredis  # noqa: E402

from tracker.redis_identity_bank import RedisIdentityBank


def _unit(seed: int, dim: int = 512) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / float(np.linalg.norm(v))


def _bank(threshold=0.72, recency=300.0, alpha=0.2, max_identities=1000):
    client = fakeredis.FakeRedis()
    return RedisIdentityBank(
        redis_client=client,
        match_threshold=threshold,
        recency_seconds=recency,
        max_identities=max_identities,
        centroid_alpha=alpha,
    )


class TestRedisBankParity:
    def test_first_sighting_is_new(self):
        bank = _bank()
        out = bank.observe(_unit(1), camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        assert out.disposition == "new"
        assert out.similarity == 1.0
        assert bank.size() == 1

    def test_same_person_different_camera_is_handed_off(self):
        bank = _bank()
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        second = bank.observe(emb, camera_id="CAM-B", detection_id="d2", now_ts=1005.0)
        assert second.disposition == "handed_off"
        assert second.identity.global_track_id == first.identity.global_track_id
        assert second.prior_camera == "CAM-A"
        assert second.elapsed_seconds == pytest.approx(5.0)

    def test_continued_on_same_camera(self):
        bank = _bank()
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        second = bank.observe(emb, camera_id="CAM-A", detection_id="d2", now_ts=1001.0)
        assert second.disposition == "continued"
        assert second.identity.global_track_id == first.identity.global_track_id
        assert second.identity.num_sightings == 2

    def test_recency_eviction(self):
        bank = _bank(recency=60.0)
        bank.observe(_unit(1), camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        out = bank.observe(_unit(1), camera_id="CAM-A", detection_id="d2", now_ts=1120.0)
        assert out.disposition == "new"
        assert bank.size() == 1

    def test_capacity_enforcement(self):
        bank = _bank(max_identities=3, recency=10_000)
        for i in range(5):
            bank.observe(_unit(i), camera_id="CAM-A", detection_id=f"d{i}", now_ts=1000.0 + i)
        assert bank.size() == 3

    def test_centroid_round_trip_is_unit_norm(self):
        """Redis stores bytes. If fp32 → bytes → fp32 drifts, the cosine
        assumption breaks. This guards the round-trip."""
        bank = _bank(alpha=0.5)
        a = _unit(1)
        bank.observe(a, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        # Force a match + centroid blend, then re-read.
        out = bank.observe(a, camera_id="CAM-A", detection_id="d2", now_ts=1001.0)
        n = float(np.linalg.norm(out.identity.centroid))
        assert math.isclose(n, 1.0, abs_tol=1e-5)

    def test_embedding_dim_mismatch_rejected(self):
        """A model swap that changes embedding dim would silently corrupt the
        bank if not caught. Observations of a different dim must raise."""
        bank = _bank()
        bank.observe(_unit(1, dim=512), camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        with pytest.raises(ValueError):
            bank.observe(_unit(2, dim=256), camera_id="CAM-A", detection_id="d2", now_ts=1001.0)

    def test_zero_vector_rejected(self):
        bank = _bank()
        with pytest.raises(ValueError):
            bank.observe(np.zeros(512, dtype=np.float32),
                         camera_id="CAM-A", detection_id="d", now_ts=1000.0)
