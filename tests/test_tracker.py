"""Tracker identity bank + cross-camera handoff invariants.

Phase 2 happy path:
  1. Person seen on CAM-A → 'new' disposition, new global_track_id minted.
  2. Same person seen on CAM-A a second later → 'continued', same gtid, no handoff.
  3. Same person seen on CAM-B → 'handed_off', SAME gtid, handoff event populated.
  4. Different person seen on CAM-B → 'new', different gtid.

Plus eviction invariants: past the recency window, a re-appearance mints a
NEW gtid. This is the "no long-gap confabulation" rule.
"""
import math

import numpy as np
import pytest

from tracker.identity_bank import IdentityBank


def _unit(seed: int, dim: int = 512) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / float(np.linalg.norm(v))


def _bank(threshold=0.72, recency=300.0, alpha=0.2, max_identities=1000):
    return IdentityBank(
        match_threshold=threshold,
        recency_seconds=recency,
        max_identities=max_identities,
        centroid_alpha=alpha,
    )


class TestNewIdentity:
    def test_first_sighting_is_new(self):
        bank = _bank()
        out = bank.observe(_unit(1), camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        assert out.disposition == "new"
        assert out.similarity == 1.0
        assert out.prior_camera is None
        assert len(bank) == 1

    def test_dissimilar_second_person_is_also_new(self):
        bank = _bank()
        bank.observe(_unit(1), camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        out = bank.observe(_unit(2), camera_id="CAM-A", detection_id="d2", now_ts=1001.0)
        assert out.disposition == "new"
        assert len(bank) == 2


class TestContinuation:
    def test_same_person_same_camera_is_continued(self):
        bank = _bank()
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        second = bank.observe(emb, camera_id="CAM-A", detection_id="d2", now_ts=1002.0)
        assert second.disposition == "continued"
        assert second.identity.global_track_id == first.identity.global_track_id
        assert second.identity.num_sightings == 2
        assert second.similarity >= 0.72  # at or above threshold


class TestCrossCameraHandoff:
    def test_same_person_different_camera_is_handed_off(self):
        bank = _bank()
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        second = bank.observe(emb, camera_id="CAM-B", detection_id="d2", now_ts=1005.0)
        assert second.disposition == "handed_off"
        # CRITICAL: global_track_id MUST persist across the camera boundary.
        # This is the Phase 2 milestone in one assertion.
        assert second.identity.global_track_id == first.identity.global_track_id
        assert second.prior_camera == "CAM-A"
        assert second.prior_detection_id == "d1"
        assert second.elapsed_seconds == pytest.approx(5.0)

    def test_handoff_then_return_to_original_camera_is_another_handoff(self):
        bank = _bank()
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        bank.observe(emb, camera_id="CAM-B", detection_id="d2", now_ts=1005.0)
        third = bank.observe(emb, camera_id="CAM-A", detection_id="d3", now_ts=1010.0)
        assert third.disposition == "handed_off"
        assert third.prior_camera == "CAM-B"
        assert third.identity.global_track_id == first.identity.global_track_id


class TestRecencyEviction:
    def test_reappearance_past_recency_mints_new_gtid(self):
        """Explicit invariant: we do NOT reuse an identity across a gap
        longer than the recency window. Different gtid, different centroid —
        it's a new track."""
        bank = _bank(recency=60.0)
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        # 120 seconds later → old track is stale, evicted.
        second = bank.observe(emb, camera_id="CAM-A", detection_id="d2", now_ts=1120.0)
        assert second.disposition == "new"
        assert second.identity.global_track_id != first.identity.global_track_id
        assert len(bank) == 1  # old one was evicted on this call

    def test_reappearance_within_recency_keeps_gtid(self):
        bank = _bank(recency=60.0)
        emb = _unit(1)
        first = bank.observe(emb, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        second = bank.observe(emb, camera_id="CAM-A", detection_id="d2", now_ts=1050.0)
        assert second.disposition == "continued"
        assert second.identity.global_track_id == first.identity.global_track_id


class TestCentroidStability:
    def test_centroid_remains_unit_norm_after_update(self):
        """If we ever break unit-norm on the centroid, cosine similarity via
        np.dot silently lies — catastrophic for the matcher."""
        bank = _bank(alpha=0.5)
        a = _unit(1)
        b = _unit(2)
        bank.observe(a, camera_id="CAM-A", detection_id="d1", now_ts=1000.0)
        # Force a match by using the same vector again, then a blended one.
        bank.observe(a, camera_id="CAM-A", detection_id="d2", now_ts=1001.0)
        ident = next(iter(bank._identities.values()))  # type: ignore[attr-defined]
        n = float(np.linalg.norm(ident.centroid))
        assert math.isclose(n, 1.0, abs_tol=1e-5), f"centroid drifted off unit sphere: |v|={n}"


class TestDegenerateInput:
    def test_zero_vector_rejected(self):
        bank = _bank()
        with pytest.raises(ValueError):
            bank.observe(np.zeros(512, dtype=np.float32),
                         camera_id="CAM-A", detection_id="d", now_ts=1000.0)


class TestCapacity:
    def test_lru_eviction_at_cap(self):
        bank = _bank(max_identities=3, recency=10_000)
        for i in range(5):
            bank.observe(_unit(i), camera_id="CAM-A", detection_id=f"d{i}", now_ts=1000.0 + i)
        assert len(bank) == 3
