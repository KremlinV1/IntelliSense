"""Tiny DI shim so the FastAPI layer can reach the running EventBus without a
circular import between api.py and main.py."""
from __future__ import annotations

from veilwatch_contracts.bus import EventBus

_bus: EventBus | None = None


def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def get_bus() -> EventBus:
    if _bus is None:
        raise RuntimeError("EventBus not initialised — confirmation.main must start it first")
    return _bus
