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


def ambiguous(method: str = "stub", *, transport_error: str = "timeout after 10s", timed_out: bool = True):
    from reticle0.rpc import RpcResult

    return RpcResult(
        method=method,
        endpoint=support.ENDPOINT,
        http_status=None,
        latency_ms=1.0,
        payload=None,
        transport_error=transport_error,
        timed_out=timed_out,
    )


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
                IDLE,                            # snapshot_before
                support.ok([0]),                 # setShutterSpeed
                support.ok(['20"']),             # getShutterSpeed read-back
                IDLE,                            # preflight_status
                support.error(40403, "Still Capturing Not Finished"),
                support.error(40403, "Still Capturing Not Finished"),
                support.ok([[URL]]),             # awaitTakePicture completes
                IDLE,                            # snapshot_after
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
                "getEvent",
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
        camera, _ = camera_with([IDLE, IDLE, support.ok([[URL]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.await_calls, 0)
        self.assertIsNone(result.requested_shutter)

    def test_unexpected_act_error_is_reported_as_error(self) -> None:
        camera, _ = camera_with([IDLE, IDLE, support.error(40401, "Camera Not Ready"), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.await_calls, 0)
        self.assertTrue(any("40401" in note for note in result.notes))

    def test_await_failure_that_is_not_40403_stops_the_loop(self) -> None:
        camera, _ = camera_with(
            [
                IDLE,
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
        camera, _ = camera_with([IDLE, IDLE, support.error(40403, "Still Capturing Not Finished"), IDLE])
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
        camera, _ = camera_with([IDLE, IDLE, support.error(40403, "Still Capturing Not Finished"), support.ok([[URL]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "complete")
        self.assertTrue(any("exposure still running" in note for note in result.notes))

    def test_postview_missing_is_called_out(self) -> None:
        camera, _ = camera_with([IDLE, IDLE, support.ok([[]]), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "no_postview")
        self.assertEqual(result.postview_urls, [])

    def test_capture_result_simulated_provenance(self) -> None:
        camera, _ = camera_with([IDLE, IDLE, support.ok([[URL]]), IDLE])
        self.assertTrue(camera.is_simulated)
        result = capture_once(camera, self.recorder)
        self.assertTrue(result.simulated)
        self.assertTrue(result.as_dict()["simulated"])

    def test_capture_result_hardware_provenance(self) -> None:
        client = support.ScriptedClient([IDLE, IDLE, support.ok([[URL]]), IDLE])
        hw_camera = SonyCamera("http://192.168.122.1:10000/sony/camera", client=client)
        self.assertFalse(hw_camera.is_simulated)
        result = capture_once(hw_camera, self.recorder)
        self.assertFalse(result.simulated)
        self.assertFalse(result.as_dict()["simulated"])

    def test_preflight_aborts_when_status_not_idle(self) -> None:
        busy = support.ok([{"type": "cameraStatus", "cameraStatus": "StillSaving"}])
        camera, client = camera_with([IDLE, busy])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertTrue(any("expected 'IDLE'" in note for note in result.notes))
        self.assertNotIn("actTakePicture", [call["method"] for call in client.calls])

    def test_preflight_aborts_when_status_missing(self) -> None:
        missing = support.ok([])
        camera, client = camera_with([IDLE, missing])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertTrue(any("missing/unknown" in note for note in result.notes))
        self.assertNotIn("actTakePicture", [call["method"] for call in client.calls])

    def test_preflight_aborts_when_status_unknown_value(self) -> None:
        unknown = support.ok([{"type": "cameraStatus", "cameraStatus": "UNKNOWN_STATE"}])
        camera, client = camera_with([IDLE, unknown])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "error")
        self.assertNotIn("actTakePicture", [call["method"] for call in client.calls])

    def test_ambiguous_act_timeout_is_not_retried(self) -> None:
        camera, client = camera_with([IDLE, IDLE, ambiguous("actTakePicture"), IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "ambiguous")
        self.assertEqual(result.await_calls, 0)
        self.assertEqual(client.calls.count({"method": "actTakePicture", "params": [], "version": "1.0", "timeout": 40.0}), 1)
        self.assertTrue(any("must not be retried automatically" in note for note in result.notes))
        self.assertIn("transport ambiguity", result.ambiguous_reason or "")

    def test_ambiguous_await_timeout_is_not_retried(self) -> None:
        camera, client = camera_with(
            [
                IDLE,
                IDLE,
                support.error(40403, "Still Capturing Not Finished"),
                ambiguous("awaitTakePicture"),
                IDLE,
            ]
        )
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "ambiguous")
        self.assertEqual(result.await_calls, 1)
        self.assertEqual([c["method"] for c in client.calls].count("actTakePicture"), 1)
        self.assertEqual([c["method"] for c in client.calls].count("awaitTakePicture"), 1)
        self.assertTrue(any("Do not retry actTakePicture automatically" in note for note in result.notes))

    def test_unparseable_act_response_is_ambiguous(self) -> None:
        from reticle0.rpc import RpcResult

        bad = RpcResult(
            method="actTakePicture",
            endpoint=support.ENDPOINT,
            http_status=200,
            latency_ms=1.0,
            payload={"_unparsed": "<html>"},
            transport_error="response was not JSON",
        )
        camera, client = camera_with([IDLE, IDLE, bad, IDLE])
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "ambiguous")
        self.assertEqual([c["method"] for c in client.calls].count("actTakePicture"), 1)

    def test_connection_loss_act_is_ambiguous(self) -> None:
        camera, client = camera_with(
            [IDLE, IDLE, ambiguous("actTakePicture", transport_error="ConnectionError: reset", timed_out=False), IDLE]
        )
        result = capture_once(camera, self.recorder)
        self.assertEqual(result.outcome, "ambiguous")
        self.assertEqual([c["method"] for c in client.calls].count("actTakePicture"), 1)

    def test_ambiguous_set_shutter_aborts_before_act(self) -> None:
        camera, client = camera_with([IDLE, ambiguous("setShutterSpeed"), IDLE])
        result = capture_once(camera, self.recorder, shutter='20"', set_shutter=True)
        self.assertEqual(result.outcome, "ambiguous")
        self.assertNotIn("actTakePicture", [c["method"] for c in client.calls])
        self.assertTrue(any("aborted before actTakePicture" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
