"""Postgres persistence layer for confirmation service.

Design:
  - SQLAlchemy 2.0 async + asyncpg driver.
  - Schema is defined with typed ORM models (models.py) and bootstrapped via
    create_all() on startup — we use Alembic only once we need migrations
    beyond Phase 3 Slice 2.
  - Module is optional at runtime: confirmation.dev_server and anything that
    runs without POSTGRES_HOST set will import this package but never call
    get_engine(). Nothing fails to import.
"""
from __future__ import annotations

from confirmation.db.engine import (
    DatabaseConfig,
    get_engine,
    get_sessionmaker,
    shutdown_engine,
)
from confirmation.db.models import Base, Camera, Incident

__all__ = [
    "Base",
    "Camera",
    "DatabaseConfig",
    "Incident",
    "get_engine",
    "get_sessionmaker",
    "shutdown_engine",
]
