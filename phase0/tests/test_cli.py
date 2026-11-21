"""CLI tests for phase0/run.py."""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

import run  # noqa: E402
from reticle0 import __version__  # noqa: E402
from tests import support  # noqa: E402


class CliParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = run.build_parser()

    def test_version_flag(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            with mock.patch("sys.stdout"):
                self.parser.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_subcommand_parsing(self) -> None:
        args_netinfo = self.parser.parse_args(["netinfo"])
        self.assertEqual(args_netinfo.command, "netinfo")

        args_probe = self.parser.parse_args(["probe", "--no-publish"])
        self.assertEqual(args_probe.command, "probe")
        self.assertFalse(args_probe.publish)

        args_events = self.parser.parse_args(["events", "--count", "10", "--seconds", "30.0"])
        self.assertEqual(args_events.command, "events")
        self.assertEqual(args_events.count, 10)
        self.assertEqual(args_events.seconds, 30.0)

        args_capture = self.parser.parse_args(["capture", "--shutter", "1/125", "--budget", "45.0"])
        self.assertEqual(args_capture.command, "capture")
        self.assertEqual(args_capture.shutter, "1/125")
        self.assertEqual(args_capture.budget, 45.0)

        args_set = self.parser.parse_args(["set", "--iso", "400", "--exposure-comp", "1", "--dry-run"])
        self.assertEqual(args_set.command, "set")
        self.assertEqual(args_set.iso, "400")
        self.assertEqual(args_set.exposure_comp, 1)
        self.assertTrue(args_set.dry_run)

        args_rec_mode = self.parser.parse_args(["rec-mode"])
        self.assertEqual(args_rec_mode.command, "rec-mode")


class CliExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.art_patcher = mock.patch("run.artifact_dir", lambda cmd: Path(self.tmp.name) / cmd)
        self.art_patcher.start()

    def tearDown(self) -> None:
        self.art_patcher.stop()
        self.tmp.cleanup()

    def test_cmd_events_rejects_non_positive_inputs(self) -> None:
        args_bad_count = argparse.Namespace(count=0, seconds=10.0)
        self.assertEqual(run.cmd_events(args_bad_count), 2)

        args_bad_seconds = argparse.Namespace(count=5, seconds=0.0)
        self.assertEqual(run.cmd_events(args_bad_seconds), 2)

    def test_cmd_capture_rejects_non_positive_budget(self) -> None:
        args = argparse.Namespace(budget=-5.0)
        self.assertEqual(run.cmd_capture(args), 2)

    def test_cmd_set_rejects_empty_request(self) -> None:
        args = argparse.Namespace(
            endpoint=support.ENDPOINT,
            shutter=None,
            iso=None,
            fnumber=None,
            exposure_comp=None,
        )
        self.assertEqual(run.cmd_set(args), 2)

    def test_cmd_set_dry_run(self) -> None:
        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([]),
                "getAvailableApiList": support.ok([[]]),
                "getAvailableIsoSpeedRate": support.ok([["AUTO", "100", "200"]]),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(
                    endpoint=support.ENDPOINT,
                    shutter=None,
                    iso="100",
                    fnumber=None,
                    exposure_comp=None,
                    dry_run=True,
                )
                self.assertEqual(run.cmd_set(args), 0)
                # Dry run must not invoke setIsoSpeedRate
                self.assertNotIn("setIsoSpeedRate", client.calls)

    def test_cmd_set_failure_returns_exit_code_3(self) -> None:
        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([]),
                "getAvailableApiList": support.ok([[]]),
                "setIsoSpeedRate": support.error(3, "Illegal Argument"),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(
                    endpoint=support.ENDPOINT,
                    shutter=None,
                    iso="INVALID_ISO",
                    fnumber=None,
                    exposure_comp=None,
                    dry_run=False,
                )
                self.assertEqual(run.cmd_set(args), 3)

    def test_cmd_probe_skips_publishing_simulated_report(self) -> None:
        client = support.CannedClient(
            {
                "getApplicationInfo": support.ok(["Smart Remote Control", "2.0.0"]),
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([]),
                "getAvailableApiList": support.ok([[]]),
                "getEvent": support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}]),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                with mock.patch("pathlib.Path.write_text") as mock_write:
                    args = argparse.Namespace(
                        endpoint=support.ENDPOINT,
                        publish=True,
                    )
                    code = run.cmd_probe(args)
                    self.assertEqual(code, 0)
                    # Docs target must NOT have been written because it was simulated
                    written_paths = [str(call.args[0]) if call.args else "" for call in mock_write.mock_calls]
                    self.assertFalse(any("02-capability-matrix.md" in p for p in written_paths))

    def test_cmd_rec_mode_success(self) -> None:
        import json

        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([["startRecMode", [], ["int"], "1.0"]]),
                "getAvailableApiList": support.ok([["startRecMode"]]),
                "getEvent": support.ok(
                    [
                        {"type": "cameraStatus", "cameraStatus": "IDLE"},
                        {"type": "availableApiList", "names": ["startRecMode", "actTakePicture"]},
                    ]
                ),
                "startRecMode": support.ok([0]),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(endpoint=support.ENDPOINT)
                code = run.cmd_rec_mode(args)
                self.assertEqual(code, 0)
                self.assertEqual(client.calls.count("startRecMode"), 1)

                artifact_path = Path(self.tmp.name) / "rec-mode" / "rec-mode.json"
                self.assertTrue(artifact_path.exists())
                data = json.loads(artifact_path.read_text(encoding="utf-8"))
                self.assertTrue(data["rpc_result"]["ok"])
                self.assertEqual(data["camera_status_before"], "IDLE")
                self.assertEqual(data["camera_status_after"], "IDLE")
                self.assertIn("actTakePicture", data["available_apis_after"])

    def test_cmd_rec_mode_not_supported(self) -> None:
        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([]),
                "getAvailableApiList": support.ok([[]]),
                "getEvent": support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}]),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(endpoint=support.ENDPOINT)
                code = run.cmd_rec_mode(args)
                self.assertEqual(code, 3)
                self.assertNotIn("startRecMode", client.calls)

    def test_cmd_rec_mode_not_available(self) -> None:
        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([["startRecMode", [], ["int"], "1.0"]]),
                "getAvailableApiList": support.ok([[]]),
                "getEvent": support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}]),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(endpoint=support.ENDPOINT)
                code = run.cmd_rec_mode(args)
                self.assertEqual(code, 3)
                self.assertNotIn("startRecMode", client.calls)

    def test_cmd_rec_mode_rpc_failure(self) -> None:
        client = support.CannedClient(
            {
                "getVersions": support.ok([["1.0"]]),
                "getMethodTypes": support.method_types([["startRecMode", [], ["int"], "1.0"]]),
                "getAvailableApiList": support.ok([["startRecMode"]]),
                "getEvent": support.ok([{"type": "cameraStatus", "cameraStatus": "IDLE"}]),
                "startRecMode": support.error(500, "Internal Error"),
            }
        )
        with mock.patch("run._resolve_endpoint", return_value=support.ENDPOINT):
            with mock.patch("run.SonyCamera", return_value=run.SonyCamera(support.ENDPOINT, client=client)):
                args = argparse.Namespace(endpoint=support.ENDPOINT)
                code = run.cmd_rec_mode(args)
                self.assertEqual(code, 4)
                self.assertEqual(client.calls.count("startRecMode"), 1)


if __name__ == "__main__":
    unittest.main()
