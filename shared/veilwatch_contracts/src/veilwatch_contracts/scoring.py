"""Shared scoring utilities. Kept in the contracts package so any service
(confirmation, dashboard, officer app) formats and fuses scores identically.

Constraint #5 from the build charter: **scores are never rounded up on display.**
A 0.799 is shown as 79%, not 80%. `format_score` enforces this with `math.floor`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def format_score(score: float, *, as_percent: bool = True) -> str:
    """Render a confidence score for display, truncating (never rounding up).

    >>> format_score(0.799)
    '79%'
    >>> format_score(0.85)
    '85%'
    >>> format_score(0.999999)
    '99%'
    >>> format_score(1.0)
    '100%'
    """
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"score out of range [0,1]: {score}")
    if as_percent:
        if score == 1.0:
            return "100%"
        # floor to avoid ever implying more confidence than we have
        return f"{math.floor(score * 100)}%"
    # non-percent: truncate to 4 dp
    return f"{math.floor(score * 10000) / 10000:.4f}"


@dataclass(frozen=True)
class SignalWeights:
    watchlist_hit: float = 0.50
    behaviour: float = 0.25
    persistence: float = 0.15
    corroboration: float = 0.10

    def as_dict(self) -> dict[str, float]:
        return {
            "watchlist_hit": self.watchlist_hit,
            "behaviour": self.behaviour,
            "persistence": self.persistence,
            "corroboration": self.corroboration,
        }

    def __post_init__(self) -> None:
        total = self.watchlist_hit + self.behaviour + self.persistence + self.corroboration
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"SignalWeights must sum to 1.0 (got {total})")


def fuse_signals(signals: dict[str, float], weights: SignalWeights | None = None) -> float:
    """Weighted linear fusion. Missing signals contribute 0 (conservative).

    We use linear fusion rather than logistic for auditability: an operator can
    read the `signal_breakdown` dict and reconstruct the score by hand. A
    logistic head will be introduced in Phase 3 after calibration data exists
    from the operator review queue.
    """
    w = weights or SignalWeights()
    score = 0.0
    for name, weight in w.as_dict().items():
        v = signals.get(name, 0.0)
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"signal '{name}' out of range [0,1]: {v}")
        score += weight * v
    # Clamp to guard against floating-point drift above 1.0.
    return min(1.0, max(0.0, score))
