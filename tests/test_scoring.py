"""Scoring invariants — these tests enforce Constraint #5 (never round up) and
the linearity property of the fusion function. They are the load-bearing tests
for the whole confirmation pipeline; do not weaken without an explicit ADR."""
import math

import pytest

from veilwatch_contracts.scoring import SignalWeights, format_score, fuse_signals


class TestFormatScore:
    @pytest.mark.parametrize("score,expected", [
        (0.0, "0%"),
        (0.001, "0%"),
        (0.79, "79%"),
        (0.799, "79%"),   # never round up — 79.9 displays as 79
        (0.7999999, "79%"),
        (0.80, "80%"),
        (0.85, "85%"),
        (0.999, "99%"),
        (0.999999, "99%"),
        (1.0, "100%"),
    ])
    def test_never_rounds_up(self, score, expected):
        assert format_score(score) == expected

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            format_score(1.1)
        with pytest.raises(ValueError):
            format_score(-0.01)


class TestFuseSignals:
    def test_all_zero_is_zero(self):
        assert fuse_signals({}) == 0.0

    def test_all_one_is_one(self):
        score = fuse_signals({
            "watchlist_hit": 1.0,
            "behaviour": 1.0,
            "persistence": 1.0,
            "corroboration": 1.0,
        })
        assert math.isclose(score, 1.0)

    def test_watchlist_weight_dominates(self):
        # watchlist (0.5) alone should reach 0.5
        assert math.isclose(fuse_signals({"watchlist_hit": 1.0}), 0.5)

    def test_signal_out_of_range_raises(self):
        with pytest.raises(ValueError):
            fuse_signals({"watchlist_hit": 1.2})

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError):
            SignalWeights(0.5, 0.5, 0.5, 0.5)

    def test_custom_weights_applied(self):
        w = SignalWeights(watchlist_hit=1.0, behaviour=0.0, persistence=0.0, corroboration=0.0)
        assert fuse_signals({"watchlist_hit": 0.8}, w) == pytest.approx(0.8)
