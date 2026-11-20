"""Capture orchestration: the 40403 long-exposure path, timing, and failure modes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0.capture import capture_once  # noqa: E402
from reticle0.recorder import Recorder  # noqa: E402
from reticle0.sony import SonyCamera  # noqa: E402

from tests import support  # noqa: E402

IDLE = support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}])
URL = "http://127.0.0.1:10000/postview/IMG0001.JPG"


def camera_with(script: list) -> tuple[SonyCamera, support.ScriptedClient]:
    client = support.ScriptedClient(script)
    return SonyCamera(support.ENDPOINT, client=client), client


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder(Path(self.tmp.name) / "trace.jsonl", echo=False)
        # capture.py sleeps between awaits; the sleeps are not under test.
        self.sleep = mock.patch("reticle0.capture.time.sleep", lambda *_: None)
        self.sleep.start()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.tmp.cleanup()

    def test_long_exposure_walks_the_documented_40403_path(self) -> None:
        camera, client = camera_with(
            [
                IDLE,
                support.ok([0]),                 # setShutterSpeed
                support.ok(['20"']),             # getShutterSpeed read-back
                support.error(40403, "Still Capturing Not Finished"),
                support.error(40403, "Still Capturing Not Finished"),
                support.ok([[URL]]),             # awaitTakePicture completes
                IDLE,
            ]
        )

        result = capture_once(camera, self.recorder, shutter="20", set_shutter=True)

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.await_calls, 2)
        self.assertEqual(result.act_error_code, 40403)
        self.assertEqual(result.postview_urls, [URL])
        self.assertEqual(result.status_before, "IDLE")
        self.assertEqual(result.status_after, "IDLE")
        self.assertEqual(result.requested_shutter, '20"')

        methods = [call["method"] for call in client.calls]
        self.assertEqual(
            methods,
            [
                "getEvent",
                "setShutterSpeed",
                "getShutterSpeed",
                "actTakePicture",
                "awaitTakePicture",
                "awaitTakePicture",
                "getEvent",
            ],
        )
        # The loop must not re-issue actTakePicture: one shutter release, one frame.
        self.assertEqual(methods.count("actTakePicture"), 1)
        self.assertIsNotNone(result.total_ms)

    def test_immediate_capture_needs_no_await(self) -> None:
        camera, _ = camera_with([IDLE, support.ok([[URL]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.await_calls, 0)
        self.assertIsNone(result.requested_shutter)

    def test_unexpected_act_error_is_reported_as_error(self) -> None:
        camera, _ = camera_with([IDLE, support.error(40401, "Camera Not Ready"), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.await_calls, 0)
        self.assertTrue(any("40401" in note for note in result.notes))

    def test_await_failure_that_is_not_40403_stops_the_loop(self) -> None:
        camera, _ = camera_with(
            [
                IDLE,
                support.error(40403, "Still Capturing Not Finished"),
                support.error(1, "Unknown Error"),
                IDLE,
            ]
        )
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.await_calls, 1)

    def test_budget_exhaustion_is_a_timeout_not_an_error(self) -> None:
        camera, _ = camera_with([IDLE, support.error(40403, "Still Capturing Not Finished"), IDLE])
        result = capture_once(camera, self.recorder, budget_seconds=0)
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(result.await_calls, 0)

    def test_rejected_shutter_speed_stops_before_the_shutter_release(self) -> None:
        camera, client = camera_with([IDLE, support.error(3, "Illegal Argument"), IDLE])
        result = capture_once(camera, self.recorder, shutter="BULB", set_shutter=True)
        self.assertEqual(result.outcome, "error")
        self.assertTrue(any("setShutterSpeed" in note for note in result.notes))
        self.assertNotIn("actTakePicture", [call["method"] for call in client.calls])

    def test_40403_on_act_is_not_treated_as_a_failure(self) -> None:
        camera, _ = camera_with([IDLE, support.error(40403, "Still Capturing Not Finished"), support.ok([[URL]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "complete")
        self.assertTrue(any("exposure still running" in note for note in result.notes))

    def test_postview_missing_is_called_out(self) -> None:
        camera, _ = camera_with([IDLE, support.ok([[]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "no_postview")
        self.assertEqual(result.postview_urls, [])


if __name__ == "__main__":
    unittest.main()
