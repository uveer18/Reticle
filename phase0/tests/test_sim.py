"""End-to-end pipeline against the offline test double over real HTTP.

This proves the transport, descriptor parsing, probe, event snapshot and capture
loop all work together before the one-shot hardware session. Nothing here is a
measurement of the A7 II; the simulator is a stand-in with documented shapes.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0 import descriptor, sim  # noqa: E402
from reticle0.capture import capture_once  # noqa: E402
from reticle0.probe import run_probe  # noqa: E402
from reticle0.recorder import Recorder  # noqa: E402
from reticle0.sony import SonyCamera  # noqa: E402


class SimHarness:
    def __enter__(self) -> "SimHarness":
        sim.CAMERA.reset()
        sim.CAMERA.fast_mode = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sim.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def endpoint(self) -> str:
        return f"{self.base}/sony/camera"


class SimEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = SimHarness().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder(Path(self.tmp.name) / "trace.jsonl", echo=False)

    def tearDown(self) -> None:
        self.harness.__exit__(None, None, None)
        self.tmp.cleanup()

    def camera(self) -> SonyCamera:
        return SonyCamera(self.harness.endpoint, recorder=self.recorder)

    def test_descriptor_yields_the_action_endpoint(self) -> None:
        device = descriptor.fetch_device_description(f"{self.harness.base}/dd.xml")
        self.assertEqual(device.model_name, "ILCE-7M2 (simulated)")
        self.assertEqual(device.camera_endpoint, self.harness.endpoint)

    def test_probe_pipeline(self) -> None:
        report = run_probe(self.camera(), self.recorder)
        self.assertEqual(report["identity"]["application_name"], "Smart Remote Control")
        self.assertEqual(report["protocol"]["camera_status"], "IDLE")
        self.assertEqual(report["derived"]["shutter"]["slowest"], '30"')
        self.assertEqual(report["derived"]["max_exposure_seconds"], 30.0)
        self.assertFalse(report["derived"]["bulb_exposed_by_api"])
        self.assertTrue(report["derived"]["await_take_picture_supported"])
        self.assertFalse(report["derived"]["interval_still_available"])
        self.assertFalse(report["derived"]["liveview_running"])

    def test_fast_capture_is_immediate(self) -> None:
        result = capture_once(self.camera(), self.recorder, shutter="1/125", set_shutter=True)
        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.await_calls, 0)
        self.assertTrue(result.postview_urls[0].endswith(".JPG"))

    def test_long_capture_uses_await(self) -> None:
        result = capture_once(self.camera(), self.recorder, shutter='20"', set_shutter=True)
        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.act_error_code, 40403)
        self.assertGreaterEqual(result.await_calls, 1)
        self.assertTrue(result.postview_urls)

    def test_camera_rejects_an_unknown_shutter_value(self) -> None:
        camera = self.camera()
        camera.load_capabilities()
        rejected = camera.set_shutter_speed("BULB")
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error_code, 3)

    def test_set_then_read_back_round_trips(self) -> None:
        camera = self.camera()
        camera.load_capabilities()
        self.assertTrue(camera.set_shutter_speed('2"').ok)
        self.assertEqual(camera.flat_list(camera.get_shutter_speed()), ['2"'])
        self.assertTrue(camera.set_iso_speed_rate("400").ok)
        self.assertEqual(camera.flat_list(camera.get_iso_speed_rate()), ["400"])

    def test_postview_endpoint_serves_valid_jpeg(self) -> None:
        import requests
        res = requests.get(f"{self.harness.base}/postview/IMG0001.JPG", timeout=5.0)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Content-Type"), "image/jpeg")
        # Starts with SOI (0xFFD8) and ends with EOI (0xFFD9)
        self.assertTrue(res.content.startswith(b"\xff\xd8"))
        self.assertTrue(res.content.endswith(b"\xff\xd9"))

    def test_error_injection_scenario(self) -> None:
        camera = self.camera()
        sim.CAMERA.inject_error("actTakePicture", 40401, "Camera Not Ready: simulated fault")
        res = camera.take_picture()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, 40401)
        self.assertIn("simulated fault", res.error[1])

        sim.CAMERA.clear_errors()
        res_after = camera.take_picture()
        self.assertTrue(res_after.ok)

    def test_shoot_mode_transitions_and_capture_rejection(self) -> None:
        camera = self.camera()
        self.assertEqual(camera.flat_list(camera.get_shoot_mode()), ["still"])

        # Switch to movie mode
        self.assertTrue(camera.set_shoot_mode("movie").ok)
        self.assertEqual(camera.flat_list(camera.get_shoot_mode()), ["movie"])

        # Capturing still in movie mode should fail with error 40400
        res = camera.take_picture()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, 40400)

        # Switch back to still mode
        self.assertTrue(camera.set_shoot_mode("still").ok)
        self.assertTrue(camera.take_picture().ok)

    def test_storage_error_scenario(self) -> None:
        camera = self.camera()
        sim.CAMERA.storage_status = "Memory Card Error"
        storage_info = camera.get_storage_information()
        self.assertTrue(storage_info.ok)
        self.assertEqual(storage_info.result[0][0]["status"], "Memory Card Error")

        # Take picture should be refused
        res = camera.take_picture()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, 40401)

    def test_exposure_compensation_and_white_balance_simulation(self) -> None:
        camera = self.camera()
        # Exposure compensation
        avail = camera.get_available_exposure_compensation()
        self.assertTrue(avail.ok)
        self.assertEqual(avail.result, [0, 6, -6, 1])

        self.assertTrue(camera.set_exposure_compensation(2).ok)
        self.assertEqual(camera.flat_list(camera.get_exposure_compensation()), [2])

        # Out-of-bounds exposure compensation index
        self.assertFalse(camera.set_exposure_compensation(99).ok)

        # White balance
        wb_avail = camera.get_available_white_balance()
        self.assertTrue(wb_avail.ok)
        self.assertIn("Daylight", wb_avail.result[1])

        self.assertTrue(camera.set_white_balance("Daylight").ok)
        self.assertEqual(camera.flat_list(camera.get_white_balance()), ["Daylight"])
        self.assertFalse(camera.set_white_balance("InvalidWB").ok)

    def test_simulated_40402_recovery(self) -> None:
        from reticle0.events import EventStream
        from unittest import mock

        camera = self.camera()
        sim.CAMERA.simulate_40402_count = 1
        stream = EventStream(camera, recorder=self.recorder)
        with mock.patch("reticle0.events.time.sleep", lambda *_: None):
            tick = stream.poll_once()
        self.assertIsNone(tick.error_code)
        self.assertTrue(tick.events)


if __name__ == "__main__":
    unittest.main()
