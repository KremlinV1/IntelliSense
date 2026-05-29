"""Lightweight dev server for the confirmation service.

Runs ONLY the HTTP/WebSocket server + demo event generator. No Kafka, no Redis,
no detection pipeline. Use this to develop the operator dashboard without
spinning up the full backend stack.

Usage:
    DEV_MODE=1 python -m confirmation.dev_server

The server listens on CONFIRMATION_HTTP_PORT (default 8080).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import uvicorn

from confirmation.alert_store import AlertStore, RecentAlerts
from confirmation.api import build_app

SERVICE = "confirmation-dev"


async def _maybe_wire_postgres(
    log: logging.Logger,
) -> tuple[AlertStore, object | None]:
    """Return (alert_store, cameras_repo). If Postgres isn't configured or
    the connection fails, fall back to in-memory for a smooth dev flow."""
    from confirmation.db import DatabaseConfig, Base, get_engine, get_sessionmaker

    cfg = DatabaseConfig.from_env(dict(os.environ))
    if cfg is None:
        log.info("no POSTGRES_HOST set — using in-memory store")
        return RecentAlerts(), None

    try:
        engine = get_engine(cfg)
        # Auto-create tables on first boot. Fine for dev; production uses
        # migrations (deferred to Slice 3's Alembic wiring).
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = get_sessionmaker()

        from confirmation.db.cameras_repo import PostgresCameraRepo
        from confirmation.db.incidents_repo import PostgresIncidentsRepo

        log.info("Postgres wired: %s", cfg.url.split("@")[-1])  # redact creds
        return PostgresIncidentsRepo(sm), PostgresCameraRepo(sm)
    except Exception as exc:
        log.warning("Postgres connection failed (%s); falling back to in-memory", exc)
        return RecentAlerts(), None


async def run() -> None:
    # Force DEV_MODE on — this script exists solely for dev.
    os.environ["DEV_MODE"] = "1"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger(SERVICE)

    port = int(os.getenv("CONFIRMATION_HTTP_PORT", "8080"))
    log.info("starting dev server on :%d (DEV_MODE=1, no Kafka)", port)

    alerts, cameras_repo = await _maybe_wire_postgres(log)
    app = build_app(alerts, cameras_repo=cameras_repo)

    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server_task = asyncio.create_task(server.serve(), name="http")

    await stop.wait()
    log.info("shutdown signal received")
    server.should_exit = True
    await server_task
    log.info("dev server stopped")


def main() -> None:
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
