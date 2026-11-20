"""Probe logic: shutter parsing, derived envelope, report rendering."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0.probe import _threshold_summary, render_markdown, run_probe, shutter_seconds  # noqa: E402
from reticle0.recorder import Recorder  # noqa: E402
from reticle0.sony import SonyCamera  # noqa: E402

from tests import support  # noqa: E402

METHODS = [
    ["getApplicationInfo", [], ["string", "string"], "1.0"],
    ["getVersions", [], ["string*"], "1.0"],
    ["getMethodTypes", ["string"], ["string*"], "1.0"],
    ["getAvailableApiList", [], ["string*"], "1.0"],
    ["getEvent", ["bool"], ["{...}*"], "1.0"],
    ["getEvent", ["bool"], ["{...}*"], "1.3"],
    ["startRecMode", [], ["int"], "1.0"],
    ["setShootMode", ["string"], ["int"], "1.0"],
    ["actTakePicture", [], ["string*"], "1.0"],
    ["awaitTakePicture", [], ["string*"], "1.0"],
    ["startIntervalStillRec", [], ["int"], "1.0"],
    ["stopIntervalStillRec", [], ["int"], "1.0"],
    ["getShutterSpeed", [], ["string"], "1.0"],
    ["getAvailableShutterSpeed", [], ["string", "string*"], "1.0"],
    ["getIsoSpeedRate", [], ["string"], "1.0"],
    ["getAvailableIsoSpeedRate", [], ["string", "string*"], "1.0"],
    ["getFNumber", [], ["string"], "1.0"],
    ["getAvailableFNumber", [], ["string", "string*"], "1.0"],
    ["getExposureCompensation", [], ["int"], "1.0"],
    ["getWhiteBalance", [], ["string"], "1.0"],
    ["getStorageInformation", [], ["{...}"], "1.0"],
]

AVAILABLE = [
    "getApplicationInfo", "getVersions", "getMethodTypes", "getAvailableApiList", "getEvent",
    "getShootMode", "actTakePicture", "awaitTakePicture",
    "getShutterSpeed", "getAvailableShutterSpeed",
    "getIsoSpeedRate", "getAvailableIsoSpeedRate",
    "getFNumber", "getAvailableFNumber",
    "getExposureCompensation", "getWhiteBalance", "getStorageInformation",
]

SHUTTERS = ["1/8000", "1/125", "1", "2", "15", "20", "25", "30\""]

EVENT = [
    {"type": "cameraStatus", "cameraStatus": "IDLE"},
    {"type": "shootMode", "currentShootMode": "still", "availableShootModes": ["still", "movie"]},
    {"type": "liveviewStatus", "liveviewStatus": False},
]


def camera_with(responses: dict) -> SonyCamera:
    return SonyCamera(support.ENDPOINT, client=support.CannedClient(responses))


def standard_responses() -> dict:
    return {
        "getApplicationInfo": support.ok(["Smart Remote Control", "2.0.0"]),
        "getVersions": support.ok([["1.0", "1.1", "1.2", "1.3"]]),
        "getMethodTypes": support.method_types(METHODS),
        "getAvailableApiList": support.ok([AVAILABLE]),
        "getEvent": support.ok(EVENT),
        "getShutterSpeed": support.ok(["1/125"]),
        "getAvailableShutterSpeed": support.ok(["1/125", SHUTTERS]),
        "getIsoSpeedRate": support.ok(["AUTO"]),
        "getAvailableIsoSpeedRate": support.ok(["AUTO", ["AUTO", "100", "400"]]),
        "getFNumber": support.ok(["4.0"]),
        "getAvailableFNumber": support.ok(["4.0", ["2.8", "4.0", "5.6"]]),
        "getExposureCompensation": support.ok([0]),
        "getWhiteBalance": support.ok(["Auto WB"]),
        "getStorageInformation": support.ok([[{"slotNumber": 1, "status": "Normal"}]]),
    }


class ShutterParsingTests(unittest.TestCase):
    def test_fractions(self) -> None:
        self.assertAlmostEqual(shutter_seconds("1/8000"), 0.000125)
        self.assertAlmostEqual(shutter_seconds("1/2"), 0.5)

    def test_whole_seconds_use_the_inch_quote(self) -> None:
        self.assertEqual(shutter_seconds('30"'), 30.0)
        self.assertEqual(shutter_seconds('1"'), 1.0)
        self.assertEqual(shutter_seconds("20"), 20.0)

    def test_bulb_is_not_a_number(self) -> None:
        self.assertIsNone(shutter_seconds("BULB"))

    def test_garbage_and_divide_by_zero(self) -> None:
        self.assertIsNone(shutter_seconds("1/0"))
        self.assertIsNone(shutter_seconds(""))
        self.assertIsNone(shutter_seconds("auto"))


class ThresholdSummaryTests(unittest.TestCase):
    def test_orders_numerically_not_lexically(self) -> None:
        summary = _threshold_summary(["1/8000", "30\"", "1/125", "2"])
        self.assertEqual(summary["fastest"], "1/8000")
        self.assertEqual(summary["slowest"], '30"')
        self.assertEqual(summary["slowest_seconds"], 30.0)
        self.assertEqual(summary["count"], 4)

    def test_bulb_is_reported_without_breaking_the_numeric_range(self) -> None:
        summary = _threshold_summary(["1/125", "BULB"])
        self.assertTrue(summary["exposes_bulb"])
        self.assertEqual(summary["slowest"], "1/125")

    def test_at_or_beyond_one_second(self) -> None:
        summary = _threshold_summary(["1/125", "1", "2", "30\""])
        self.assertEqual(summary["at_or_beyond_1s"], ["1", "2", '30"'])

    def test_empty(self) -> None:
        summary = _threshold_summary([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["slowest_seconds"])


class RunProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.recorder = Recorder(Path(self.tmp.name) / "trace.jsonl", echo=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_report_shape_and_derived_facts(self) -> None:
        report = run_probe(camera_with(standard_responses()), self.recorder)

        self.assertEqual(report["schema"], "reticle.phase0.capability/1")
        self.assertEqual(report["identity"]["application_name"], "Smart Remote Control")
        self.assertEqual(report["protocol"]["camera_status"], "IDLE")
        self.assertEqual(report["protocol"]["get_event_versions"], ["1.0", "1.3"])

        derived = report["derived"]
        self.assertEqual(derived["shutter"]["count"], len(SHUTTERS))
        self.assertEqual(derived["shutter"]["slowest"], '30"')
        self.assertEqual(derived["max_exposure_seconds"], 30.0)
        self.assertTrue(derived["long_exposure_capable"])
        self.assertTrue(derived["await_take_picture_supported"])
        self.assertTrue(derived["interval_still_supported"])
        self.assertFalse(derived["interval_still_available"], "supported is not availability")
        self.assertFalse(derived["liveview_running"])
        self.assertFalse(derived["bulb_exposed_by_api"])

    def test_unsupported_setting_is_not_called(self) -> None:
        responses = standard_responses()
        client = support.CannedClient(responses)
        camera = SonyCamera(support.ENDPOINT, client=client)
        run_probe(camera, self.recorder)
        self.assertNotIn("startLiveview", client.calls, "the probe must never start live view")
        self.assertEqual(client.calls.count("getShutterSpeed"), 1, "one read per setting, no polling")

    def test_probe_errors_are_recorded_not_raised(self) -> None:
        responses = standard_responses()
        del responses["getAvailableApiList"]
        report = run_probe(camera_with(responses), self.recorder)
        self.assertEqual(report["capability"]["available"], [])
        self.assertFalse(report["derived"]["interval_still_available"])

    def test_markdown_render_mentions_the_measured_values(self) -> None:
        markdown = render_markdown(run_probe(camera_with(standard_responses()), self.recorder))
        self.assertIn("# Capability matrix (measured)", markdown)
        self.assertIn("Smart Remote Control", markdown)
        self.assertIn("| Bulb exposed by API | no |", markdown)
        self.assertIn("`30\"`", markdown)

    def test_markdown_render_simulated_warning(self) -> None:
        report = run_probe(camera_with(standard_responses()), self.recorder)
        report["simulated"] = True
        markdown = render_markdown(report)
        self.assertIn("[SIMULATED]", markdown)
        self.assertIn("SIMULATED CAMERA EVIDENCE", markdown)
        self.assertIn("Provenance: `simulated`", markdown)

    def test_probe_simulated_vs_hardware_provenance(self) -> None:
        local_cam = camera_with(standard_responses())
        self.assertTrue(local_cam.is_simulated)
        rep_sim = run_probe(local_cam, self.recorder)
        self.assertTrue(rep_sim["simulated"])
        self.assertEqual(rep_sim["provenance"], "simulated")

        hw_cam = SonyCamera("http://192.168.122.1:10000/sony/camera", client=support.CannedClient(standard_responses()))
        self.assertFalse(hw_cam.is_simulated)
        rep_hw = run_probe(hw_cam, self.recorder)
        self.assertFalse(rep_hw["simulated"])
        self.assertEqual(rep_hw["provenance"], "hardware")


if __name__ == "__main__":
    unittest.main()
