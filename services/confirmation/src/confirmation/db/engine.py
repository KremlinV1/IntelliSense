"""Async engine + session factory for Postgres persistence.

Lifecycle:
  - get_engine(config) is idempotent — first call builds the engine, subsequent
    calls return the cached one.
  - shutdown_engine() disposes the pool. Called from the FastAPI lifespan on
    shutdown so asyncio doesn't complain about pending connections.

Connection URL:
  - Postgres in production: "postgresql+asyncpg://user:pass@host:port/db"
  - SQLite in tests: "sqlite+aiosqlite:///:memory:"
  The models are written to work against both (JSONB → Text on SQLite) so a
  test can exercise the query path without standing up a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "DatabaseConfig | None":
        """Build config from env vars. Returns None if Postgres isn't configured
        (caller falls back to in-memory).

        Accepts either POSTGRES_URL (full DSN) or POSTGRES_HOST + friends.
        """
        url = env.get("POSTGRES_URL", "").strip()
        if not url:
            host = env.get("POSTGRES_HOST", "").strip()
            if not host:
                return None
            port = env.get("POSTGRES_PORT", "5432")
            db = env.get("POSTGRES_DB", "veilwatch")
            user = env.get("POSTGRES_USER", "veilwatch")
            password = env.get("POSTGRES_PASSWORD", "")
            auth = f"{user}:{password}@" if password else f"{user}@"
            url = f"postgresql+asyncpg://{auth}{host}:{port}/{db}"
        echo = env.get("POSTGRES_ECHO", "").lower() in ("1", "true", "yes")
        return cls(url=url, echo=echo)


_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(config: DatabaseConfig) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        # SQLite can't honour pool_size/max_overflow — the async driver is
        # single-connection by nature. Detect and elide those kwargs so tests
        # don't emit noisy warnings.
        kwargs: dict = {"echo": config.echo, "future": True}
        if not config.url.startswith("sqlite"):
            kwargs["pool_size"] = config.pool_size
            kwargs["max_overflow"] = config.max_overflow
        _engine = create_async_engine(config.url, **kwargs)
        _sessionmaker = async_sessionmaker(
            _engine, class_=AsyncSession, expire_on_commit=False
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError(
            "get_sessionmaker() called before get_engine(); "
            "did you forget to initialise the DB?"
        )
    return _sessionmaker


async def shutdown_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
