"""Event stream: flattening, single-parked-poll recovery, and the no-spin rule."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0.events import EventStream, flatten  # noqa: E402
from reticle0.sony import SonyCamera  # noqa: E402

from tests import support  # noqa: E402


class FlattenTests(unittest.TestCase):
    def test_keyed_by_object_type(self) -> None:
        payload = [
            {"type": "cameraStatus", "cameraStatus": "IDLE"},
            {"type": "liveviewStatus", "liveviewStatus": False},
        ]
        events = flatten(payload)
        self.assertEqual(set(events), {"cameraStatus", "liveviewStatus"})
        self.assertEqual(events["cameraStatus"]["cameraStatus"], "IDLE")

    def test_ignores_entries_without_a_type(self) -> None:
        self.assertEqual(flatten([{"cameraStatus": "IDLE"}, "junk", 3]), {})

    def test_non_list_payload(self) -> None:
        self.assertEqual(flatten(None), {})
        self.assertEqual(flatten({"type": "cameraStatus"}), {})


class EventStreamTests(unittest.TestCase):
    def test_40402_recovers_with_exactly_one_retry(self) -> None:
        client = support.ScriptedClient(
            [
                support.error(40402, "Already Polling"),
                support.ok([{"type": "cameraStatus", "cameraStatus": "StillCapturing"}]),
            ]
        )
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client))
        with mock.patch("reticle0.events.time.sleep", lambda *_: None):
            tick = stream.poll_once()

        self.assertEqual(tick.error_code, None)
        self.assertEqual(tick.events["cameraStatus"]["cameraStatus"], "StillCapturing")
        self.assertEqual([call["method"] for call in client.calls], ["getEvent", "getEvent"])

    def test_snapshot_uses_polling_false(self) -> None:
        client = support.ScriptedClient([support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])])
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client))
        tick = stream.snapshot()
        self.assertEqual(client.calls[0]["params"], [False])
        self.assertEqual(tick.events["cameraStatus"]["cameraStatus"], "IDLE")

    def test_camera_status_helper(self) -> None:
        client = support.ScriptedClient([support.ok([{"type": "cameraStatus", "cameraStatus": "StillSaving"}])])
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client))
        self.assertEqual(stream.camera_status(), "StillSaving")

    def test_stream_stops_at_the_deadline(self) -> None:
        client = support.ScriptedClient([])  # any call would be a bug
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client))
        self.assertEqual(list(stream.stream(max_events=5, seconds=0)), [])

    def test_poll_once_custom_timeout_does_not_mutate_instance(self) -> None:
        client = support.ScriptedClient([support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])])
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client), timeout=70.0)
        tick = stream.poll_once(timeout=5.0)
        self.assertEqual(client.calls[0]["timeout"], 5.0)
        self.assertEqual(stream.timeout, 70.0, "poll_once with explicit timeout must not mutate stream.timeout")

    def test_stream_preserves_instance_timeout(self) -> None:
        client = support.ScriptedClient(
            [
                support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}]),
                support.ok([{"type": "cameraStatus", "cameraStatus": "StillSaving"}]),
            ]
        )
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client), timeout=70.0)
        with mock.patch("reticle0.events.time.sleep", lambda *_: None):
            ticks = list(stream.stream(max_events=2, seconds=10.0))
        self.assertEqual(len(ticks), 2)
        self.assertEqual(stream.timeout, 70.0, "stream() must not permanently mutate stream.timeout")

    def test_camera_status_returns_none_when_empty(self) -> None:
        client = support.ScriptedClient([support.ok([])])
        stream = EventStream(SonyCamera(support.ENDPOINT, client=client))
        self.assertIsNone(stream.camera_status())

    def test_concurrent_long_poll_is_rejected(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingClient:
            def call(self, method, params=None, *, version="1.0", timeout=10.0):
                if params == [True]:
                    started.set()
                    release.wait(timeout=2)
                    return support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])
                return support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])

        stream = EventStream(SonyCamera(support.ENDPOINT, client=BlockingClient()))
        t = threading.Thread(target=stream.poll_once, kwargs={"timeout": 1.0})
        t.start()
        self.assertTrue(started.wait(timeout=1))
        with self.assertRaises(RuntimeError):
            stream.poll_once(timeout=0.1)
        release.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())

    def test_snapshot_not_blocked_by_active_long_poll(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingClient:
            def call(self, method, params=None, *, version="1.0", timeout=10.0):
                if params == [True]:
                    started.set()
                    release.wait(timeout=2)
                    return support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])
                return support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])

        camera = SonyCamera(support.ENDPOINT, client=BlockingClient())
        stream = EventStream(camera)
        t = threading.Thread(target=stream.poll_once, kwargs={"timeout": 1.0})
        t.start()
        self.assertTrue(started.wait(timeout=1))
        snap = stream.snapshot()
        self.assertEqual(snap.events["cameraStatus"]["cameraStatus"], "IDLE")
        release.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
