"""Re-ID engine: embedding geometry + determinism + graceful degrade."""
import math

import numpy as np
import pytest

from detection.reid import (
    MOCK_EMBEDDING_DIM,
    MockReidBackend,
    OnnxReidBackend,
    ReidEngine,
)


def _rand_crop(seed: int, h: int = 128, w: int = 64) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, size=(h, w, 3), dtype=np.uint8)


class TestMockBackendGeometry:
    def test_output_is_unit_norm(self):
        be = MockReidBackend()
        result = be.embed(_rand_crop(0))
        assert result is not None
        assert math.isclose(float(np.linalg.norm(result.embedding)), 1.0, abs_tol=1e-5)

    def test_dimensionality_is_stable(self):
        be = MockReidBackend()
        result = be.embed(_rand_crop(0))
        assert result is not None
        assert result.embedding.shape == (MOCK_EMBEDDING_DIM,)

    def test_deterministic_for_same_crop(self):
        be = MockReidBackend()
        img = _rand_crop(42)
        a = be.embed(img)
        b = be.embed(img)
        assert a is not None and b is not None
        np.testing.assert_array_equal(a.embedding, b.embedding)

    def test_different_crops_are_near_orthogonal(self):
        """Two random crops should have low cosine similarity — otherwise the
        mock backend is useless for tracker testing."""
        be = MockReidBackend()
        a = be.embed(_rand_crop(1))
        b = be.embed(_rand_crop(2))
        assert a is not None and b is not None
        cos = float(np.dot(a.embedding, b.embedding))
        # Generous bound; we just need "not the same person".
        assert abs(cos) < 0.3, f"unexpectedly similar random crops: cos={cos}"

    def test_tiny_crop_returns_none(self):
        be = MockReidBackend()
        assert be.embed(np.zeros((8, 8, 3), dtype=np.uint8)) is None

    def test_empty_crop_returns_none(self):
        be = MockReidBackend()
        assert be.embed(np.zeros((0, 0, 3), dtype=np.uint8)) is None


class TestReidEngine:
    def test_returns_tuple_of_floats(self):
        eng = ReidEngine(MockReidBackend())
        emb = eng.embed(_rand_crop(0))
        assert emb is not None
        assert isinstance(emb, tuple)
        assert all(isinstance(x, float) for x in emb)
        assert len(emb) == MOCK_EMBEDDING_DIM

    def test_none_propagates_on_unusable_crop(self):
        eng = ReidEngine(MockReidBackend())
        assert eng.embed(np.zeros((4, 4, 3), dtype=np.uint8)) is None


class TestOnnxBackendGracefulFail:
    def test_missing_model_file_raises_clearly(self, tmp_path):
        """The contract: OnnxReidBackend refuses to load without a model,
        rather than silently returning zero-vectors that would make every
        identity look the same to the tracker."""
        missing = str(tmp_path / "nope.onnx")
        with pytest.raises(RuntimeError) as excinfo:
            OnnxReidBackend(model_path=missing)
        assert "model file not found" in str(excinfo.value).lower() or \
               "onnxruntime" in str(excinfo.value).lower()
