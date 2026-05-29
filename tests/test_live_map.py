"""Live-map broadcaster: fan-out semantics + back-pressure policy + WebSocket
integration via FastAPI's TestClient.

We stick to the broadcaster's public surface. The Kafka wiring is tested at
the contract level elsewhere; what matters here is that:
  1. Multiple clients receive the same payload.
  2. A slow client is dropped rather than blocking the publisher.
  3. The FastAPI /ws/live-map route registers and unregisters cleanly.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from veilwatch_contracts import (
    AlertDisposition,
    BoundingBox,
    CameraRef,
    ConfirmedAlert,
    CrossCameraMatch,
    Detection,
)
from veilwatch_contracts.events import ThreatClass

from confirmation.alert_store import RecentAlerts
from confirmation.api import build_app
from confirmation.live_map import LiveMapBroadcaster, get_broadcaster


def _alert() -> ConfirmedAlert:
    cam = CameraRef(camera_id="CAM-A", latitude=0.0, longitude=0.0)
    det = Detection(
        detection_id="d1",
        class_name="person",
        threat_class=ThreatClass.PERSON,
        confidence=0.9,
        bbox=BoundingBox(x1=0, y1=0, x2=10, y2=10),
    )
    return ConfirmedAlert(
        event_id="e1",
        incident_id="inc-1",
        camera=cam,
        frame_id="f1",
        frame_uri="file:///tmp/f.jpg",
        frame_sha256="a" * 64,
        threat_score=0.9,
        disposition=AlertDisposition.AUTO_CONFIRMED,
        primary_detection=det,
        signal_breakdown={"watchlist_hit": 1.0, "behaviour": 0.6,
                          "persistence": 0.8, "corroboration": 1.0},
    )


def _handoff() -> CrossCameraMatch:
    cam_a = CameraRef(camera_id="CAM-A", latitude=0.0, longitude=0.0)
    cam_b = CameraRef(camera_id="CAM-B", latitude=0.0, longitude=0.0)
    return CrossCameraMatch(
        event_id="e-h",
        global_track_id="g-1",
        prior_camera=cam_a,
        new_camera=cam_b,
        prior_detection_id="d-a",
        new_detection_id="d-b",
        similarity=0.88,
        elapsed_seconds=5.0,
    )


class TestBroadcaster:
    def test_fan_out_to_two_clients(self):
        async def scenario():
            b = LiveMapBroadcaster()
            c1 = await b.register("1.1.1.1:1")
            c2 = await b.register("2.2.2.2:2")
            await b.publish("alert", _alert())
            # Both clients saw the same envelope.
            m1 = await asyncio.wait_for(c1.queue.get(), timeout=1.0)
            m2 = await asyncio.wait_for(c2.queue.get(), timeout=1.0)
            assert m1 == m2
            env = json.loads(m1)
            assert env["type"] == "alert"
            assert env["data"]["incident_id"] == "inc-1"

        asyncio.run(scenario())

    def test_slow_client_is_dropped(self):
        """A client whose queue fills must be disconnected, not allowed to
        slow down the publisher. This is the non-negotiable policy for any
        broadcaster that sits in front of a real-time pipeline."""
        async def scenario():
            b = LiveMapBroadcaster()
            slow = await b.register("slow")
            # Don't consume slow.queue. Publish enough to overflow.
            for i in range(100):
                await b.publish("alert", _alert())
            # The publisher finished without hanging. The slow client was dropped.
            assert b.client_count() == 0

        asyncio.run(scenario())

    def test_handoff_envelope_shape(self):
        async def scenario():
            b = LiveMapBroadcaster()
            c = await b.register("x")
            await b.publish("handoff", _handoff())
            env = json.loads(await asyncio.wait_for(c.queue.get(), timeout=1.0))
            assert env["type"] == "handoff"
            assert env["data"]["global_track_id"] == "g-1"
            assert env["data"]["prior_camera"]["camera_id"] == "CAM-A"
            assert env["data"]["new_camera"]["camera_id"] == "CAM-B"

        asyncio.run(scenario())


class TestWebSocketRoute:
    def test_connect_and_disconnect_updates_client_count(self):
        """Smoke-level integration: the FastAPI route accepts connections,
        registers them with the module-level broadcaster, and unregisters
        cleanly on disconnect.

        We deliberately don't drive a broadcast through the test client here
        because FastAPI's TestClient runs the app on a separate anyio thread
        and cross-thread coroutine scheduling adds more complexity than this
        test is worth. Fan-out semantics are covered by the unit tests
        above (TestBroadcaster), which exercise the exact code path the
        WebSocket handler uses.
        """
        import confirmation.live_map as lm
        lm._broadcaster = None  # reset singleton
        app = build_app(RecentAlerts())

        with TestClient(app) as client:
            assert get_broadcaster().client_count() == 0
            with client.websocket_connect("/ws/live-map"):
                # Poll briefly — the registration happens on the app thread.
                import time
                deadline = time.time() + 2.0
                while time.time() < deadline and get_broadcaster().client_count() < 1:
                    time.sleep(0.02)
                assert get_broadcaster().client_count() == 1
            # On disconnect, the handler's `finally` calls unregister.
            deadline = time.time() + 2.0
            while time.time() < deadline and get_broadcaster().client_count() > 0:
                time.sleep(0.02)
            assert get_broadcaster().client_count() == 0
