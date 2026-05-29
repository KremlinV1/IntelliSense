"""Factory for the identity bank backend. Isolates the redis import so the
tracker still starts cleanly (and tests still run) when redis isn't installed
or reachable."""
from __future__ import annotations

from tracker.config import TrackerConfig
from tracker.identity_bank import IdentityBank, IdentityBankLike


def build_bank(cfg: TrackerConfig) -> IdentityBankLike:
    if cfg.bank_backend == "memory":
        return IdentityBank(
            match_threshold=cfg.match_threshold,
            recency_seconds=cfg.recency_seconds,
            max_identities=cfg.max_identities,
            centroid_alpha=cfg.centroid_alpha,
        )
    if cfg.bank_backend == "redis":
        # Lazy import so the redis package is optional when using the memory backend.
        import redis  # type: ignore
        from tracker.redis_identity_bank import RedisIdentityBank
        client = redis.Redis.from_url(cfg.redis_url)
        # Probe the connection at startup rather than on first observe() — we
        # want a clear, loud failure in the container logs if Redis isn't
        # reachable, not a silent first-request failure 30 seconds later.
        client.ping()
        return RedisIdentityBank(
            redis_client=client,
            match_threshold=cfg.match_threshold,
            recency_seconds=cfg.recency_seconds,
            max_identities=cfg.max_identities,
            centroid_alpha=cfg.centroid_alpha,
        )
    raise ValueError(f"unknown TRACKER_BANK_BACKEND: {cfg.bank_backend!r}")
