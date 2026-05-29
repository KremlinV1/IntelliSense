"""ALPR engine + plate watchlist + plate normalisation.

These tests lock in the canonicalisation rule (the single source of truth for
'are these two plate strings the same plate?') and the determinism of the mock
backend (so pipeline integration tests are reproducible)."""
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from detection.alpr import (
    AlprEngine,
    MockAlprBackend,
    OpenAlprSubprocessBackend,
    normalise_plate,
)
from detection.watchlist import PlateWatchlist


class TestNormalisePlate:
    @pytest.mark.parametrize("raw,expected", [
        ("ABC 123",   "ABC123"),
        ("abc-123",   "ABC123"),
        ("  abc 123 ", "ABC123"),
        ("ABC.123",   "ABC123"),
        ("a!b@c#1$2%3", "ABC123"),
        ("7H4RD",     "7H4RD"),
        ("",          ""),
        ("   ",       ""),
    ])
    def test_canonical_form(self, raw, expected):
        assert normalise_plate(raw) == expected


class TestMockBackend:
    def test_deterministic_for_same_crop(self):
        be = MockAlprBackend()
        img = (np.random.default_rng(42).integers(0, 256, size=(64, 128, 3), dtype=np.uint8))
        r1 = be.read(img)
        r2 = be.read(img)
        assert r1 == r2
        assert len(r1) == 1
        assert len(r1[0].plate_text) == 6  # 3 letters + 3 digits after normalisation

    def test_different_crops_usually_differ(self):
        be = MockAlprBackend()
        img_a = np.random.default_rng(1).integers(0, 256, size=(64, 128, 3), dtype=np.uint8)
        img_b = np.random.default_rng(2).integers(0, 256, size=(64, 128, 3), dtype=np.uint8)
        a = be.read(img_a)[0].plate_text
        b = be.read(img_b)[0].plate_text
        assert a != b

    def test_tiny_crop_returns_nothing(self):
        # Guard against emitting a plate read from a 4x4 vehicle crop — that's
        # a false positive in waiting.
        be = MockAlprBackend()
        tiny = np.zeros((8, 8, 3), dtype=np.uint8)
        assert be.read(tiny) == []


class TestAlprEngineConfidenceGate:
    def test_below_threshold_returns_none(self):
        be = MockAlprBackend(base_confidence=0.40)
        eng = AlprEngine(be, min_confidence=0.55)
        img = np.random.default_rng(0).integers(0, 256, size=(64, 128, 3), dtype=np.uint8)
        assert eng.read_best(img) is None

    def test_above_threshold_returns_best(self):
        be = MockAlprBackend(base_confidence=0.78)
        eng = AlprEngine(be, min_confidence=0.55)
        img = np.random.default_rng(0).integers(0, 256, size=(64, 128, 3), dtype=np.uint8)
        result = eng.read_best(img)
        assert result is not None
        assert result.confidence == pytest.approx(0.78)


class TestPlateWatchlist:
    def _seed(self, tmp_path: Path) -> str:
        path = tmp_path / "seed.json"
        path.write_text(json.dumps({
            "plate_identities": [
                {"identity_id": "VL-1", "plate_text": "ABC 123", "label": "stolen"},
                {"identity_id": "VL-2", "plate_text": "xyz-789", "label": "amber"},
            ]
        }))
        return str(path)

    def test_exact_match_after_canonicalisation(self, tmp_path):
        wl = PlateWatchlist()
        wl.load_seed(self._seed(tmp_path))
        # Caller is responsible for canonicalising the input; mimic what the
        # detection service does.
        assert wl.match(normalise_plate("abc-123")) is not None
        assert wl.match(normalise_plate("XYZ 789")) is not None

    def test_unseen_plate_misses(self, tmp_path):
        wl = PlateWatchlist()
        wl.load_seed(self._seed(tmp_path))
        assert wl.match(normalise_plate("UNKNOWN 001")) is None

    def test_empty_canonical_is_not_a_hit(self, tmp_path):
        wl = PlateWatchlist()
        wl.load_seed(self._seed(tmp_path))
        assert wl.match("") is None

    def test_missing_seed_file_degrades_gracefully(self, tmp_path):
        wl = PlateWatchlist()
        wl.load_seed(str(tmp_path / "does_not_exist.json"))
        assert wl.match("ABC123") is None  # no crash, no hits

    def test_match_similarity_is_exactly_one(self, tmp_path):
        wl = PlateWatchlist()
        wl.load_seed(self._seed(tmp_path))
        m = wl.match(normalise_plate("ABC123"))
        assert m is not None
        assert m.similarity == 1.0  # exact match by construction


class TestOpenAlprBackendGracefulDegrade:
    def test_no_binary_returns_empty(self):
        # Without the alpr binary on PATH (the default in CI), the subprocess
        # backend must NOT raise — it must return [] so the pipeline keeps moving.
        be = OpenAlprSubprocessBackend()
        img = np.zeros((64, 128, 3), dtype=np.uint8)
        # We don't assert the binary is missing — on dev machines where it IS
        # present, a real read is also acceptable. Either way: no exception.
        result = be.read(img)
        assert isinstance(result, list)
