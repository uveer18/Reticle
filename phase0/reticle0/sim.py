"""A stand-in camera so the harness can be exercised before touching hardware.

This is a *test double*, not a model of the A7 II. It reproduces the documented
response shapes and the 40403 long-exposure path so the harness, the report
rendering and the CLI can be verified offline. Any result produced against the
simulator is labelled ``simulated`` and must never be mistaken for a measurement.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHUTTERS = [
    "1/8000", "1/4000", "1/2000", "1/1000", "1/500", "1/250", "1/125", "1/60",
    "1/30", "1/15", "1/8", "1/4", "1/2", "1\"", "2\"", "4\"", "8\"", "15\"",
    "20\"", "25\"", "30\"",
]
ISOS = ["AUTO", "100", "200", "400", "800", "1600", "3200", "6400", "12800", "25600"]
FNUMBERS = ["2.8", "3.2", "3.5", "4.0", "4.5", "5.0", "5.6", "6.3", "7.1", "8.0"]

METHODS = [
    ["getApplicationInfo", [], ["string", "string"], "1.0"],
    ["getVersions", [], ["string*"], "1.0"],
    ["getMethodTypes", ["string"], ["string*"], "1.0"],
    ["getAvailableApiList", [], ["string*"], "1.0"],
    ["getEvent", ["bool"], ["{...}*"], "1.0"],
    ["getEvent", ["bool"], ["{...}*"], "1.1"],
    ["getEvent", ["bool"], ["{...}*"], "1.3"],
    ["startRecMode", [], ["int"], "1.0"],
    ["stopRecMode", [], ["int"], "1.0"],
    ["setCameraFunction", ["string"], ["int"], "1.0"],
    ["getShootMode", [], ["string"], "1.0"],
    ["setShootMode", ["string"], ["int"], "1.0"],
    ["actTakePicture", [], ["string*"], "1.0"],
    ["awaitTakePicture", [], ["string*"], "1.0"],
    ["getShutterSpeed", [], ["string"], "1.0"],
    ["getAvailableShutterSpeed", [], ["string", "string*"], "1.0"],
    ["setShutterSpeed", ["string"], ["int"], "1.0"],
    ["getIsoSpeedRate", [], ["string"], "1.0"],
    ["getAvailableIsoSpeedRate", [], ["string*"], "1.0"],
    ["setIsoSpeedRate", ["string"], ["int"], "1.0"],
    ["getFNumber", [], ["string"], "1.0"],
    ["getAvailableFNumber", [], ["string*"], "1.0"],
    ["setFNumber", ["string"], ["int"], "1.0"],
    ["getExposureCompensation", [], ["int"], "1.0"],
    ["setExposureCompensation", ["int"], ["int"], "1.0"],
    ["getWhiteBalance", [], ["string"], "1.0"],
    ["getStorageInformation", [], ["{...}"], "1.0"],
]


class FakeCamera:
    """Mutable camera state shared by request handler threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Return to the power-on state (used between test runs)."""
        self.shutter = "1/125"
        self.iso = "AUTO"
        self.fnumber = "4.0"
        self.exposure_compensation = 0
        self.status = "IDLE"
        self.pending_awaits = 0
        self.shots = 0

    def shoot(self) -> dict:
        with self.lock:
            self.shots += 1
            seconds = _seconds(self.shutter)
            if seconds >= 1.0:
                # Mirror the documented long-exposure behaviour.
                self.status = "StillCapturing"
                self.pending_awaits = 3
                time.sleep(0.12)
                return {"error": [40403, "Still Capturing Not Finished"]}
            self.status = "StillSaving"
            time.sleep(0.12)
            self.status = "IDLE"
            return {"result": [[f"http://127.0.0.1:10000/postview/IMG{self.shots:04d}.JPG"]]}

    def await_picture(self) -> dict:
        with self.lock:
            time.sleep(0.4)
            if self.pending_awaits > 0:
                self.pending_awaits -= 1
                if self.pending_awaits == 0:
                    self.status = "StillSaving"
                    time.sleep(0.1)
                    self.status = "IDLE"
                    return {"result": [[f"http://127.0.0.1:10000/postview/IMG{self.shots:04d}.JPG"]]}
                return {"error": [40403, "Still Capturing Not Finished"]}
            return {"error": [40403, "Still Capturing Not Finished"]}

    def event_payload(self) -> list:
        with self.lock:
            return [
                {"type": "cameraStatus", "cameraStatus": self.status},
                {"type": "shootMode", "currentShootMode": "still", "availableShootModes": ["still", "movie"]},
                {"type": "liveviewStatus", "liveviewStatus": False},
                {"type": "storageInformation", "storageInformation": [{"slotNumber": 1, "status": "Normal"}]},
                {"type": "availableApiList", "names": AVAILABLE},
            ]


def _seconds(shutter: str) -> float:
    # Documented wire form quotes whole seconds: 30" means thirty seconds.
    text = shutter.replace('"', "")
    if "/" in text:
        a, b = text.split("/")
        return float(a) / float(b)
    return float(text)


AVAILABLE = [
    "getApplicationInfo", "getVersions", "getMethodTypes", "getAvailableApiList",
    "getEvent", "getShootMode", "actTakePicture", "awaitTakePicture",
    "getShutterSpeed", "getAvailableShutterSpeed", "setShutterSpeed",
    "getIsoSpeedRate", "getAvailableIsoSpeedRate", "setIsoSpeedRate",
    "getFNumber", "getAvailableFNumber", "setFNumber",
    "getExposureCompensation", "setExposureCompensation", "getWhiteBalance",
    "getStorageInformation",
]
CAMERA = FakeCamera()


def _dispatch(method: str, params: list) -> dict:
    if method == "getApplicationInfo":
        return {"result": ["Smart Remote Control", "2.0.0"]}
    if method == "getVersions":
        return {"result": [["1.0", "1.1", "1.2", "1.3"]]}
    if method == "getMethodTypes":
        return {"results": METHODS}
    if method == "getAvailableApiList":
        return {"result": [AVAILABLE]}
    if method == "getEvent":
        if params and params[0]:
            time.sleep(0.8)
        return {"result": CAMERA.event_payload()}
    if method == "getShootMode":
        return {"result": ["still"]}
    if method == "setShootMode":
        return {"result": [0]}
    if method == "getShutterSpeed":
        return {"result": [CAMERA.shutter]}
    if method == "getAvailableShutterSpeed":
        return {"result": [CAMERA.shutter, SHUTTERS]}
    if method == "setShutterSpeed":
        if params[0] not in SHUTTERS:
            return {"error": [3, "Illegal Argument"]}
        CAMERA.shutter = params[0]
        return {"result": [0]}
    if method == "getIsoSpeedRate":
        return {"result": [CAMERA.iso]}
    if method == "getAvailableIsoSpeedRate":
        return {"result": [ISOS]}
    if method == "setIsoSpeedRate":
        CAMERA.iso = str(params[0])
        return {"result": [0]}
    if method == "getFNumber":
        return {"result": [CAMERA.fnumber]}
    if method == "getAvailableFNumber":
        return {"result": [FNUMBERS]}
    if method == "setFNumber":
        CAMERA.fnumber = str(params[0])
        return {"result": [0]}
    if method == "getExposureCompensation":
        return {"result": [CAMERA.exposure_compensation]}
    if method == "setExposureCompensation":
        CAMERA.exposure_compensation = int(params[0])
        return {"result": [0]}
    if method == "getWhiteBalance":
        return {"result": ["Auto WB"]}
    if method == "getStorageInformation":
        return {"result": [[{"slotNumber": 1, "status": "Normal"}]]}
    if method == "actTakePicture":
        return CAMERA.shoot()
    if method == "awaitTakePicture":
        return CAMERA.await_picture()
    if method in ("startRecMode", "stopRecMode", "setCameraFunction"):
        return {"result": [0]}
    if method in ("startIntervalStillRec", "stopIntervalStillRec"):
        return {"error": [12, "No Such Method"]}
    return {"error": [12, "No Such Method"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeSonyScalarWebAPI/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # silence
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/dd.xml", "/"):
            port = self.server.server_address[1]
            xml = (
                '<?xml version="1.0"?>\n'
                '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av">\n'
                "  <device>\n"
                "    <friendlyName>SIMULATED ILCE-7M2</friendlyName>\n"
                "    <modelName>ILCE-7M2 (simulated)</modelName>\n"
                "    <av:X_ScalarWebAPI_DeviceInfo>\n"
                "      <av:X_ScalarWebAPI_Version>2.0</av:X_ScalarWebAPI_Version>\n"
                "      <av:X_ScalarWebAPI_ServiceList>\n"
                "        <av:X_ScalarWebAPI_Service>\n"
                "          <av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>\n"
                f"          <av:X_ScalarWebAPI_ActionList_URL>http://127.0.0.1:{port}/sony</av:X_ScalarWebAPI_ActionList_URL>\n"
                "        </av:X_ScalarWebAPI_Service>\n"
                "      </av:X_ScalarWebAPI_ServiceList>\n"
                "    </av:X_ScalarWebAPI_DeviceInfo>\n"
                "  </device>\n"
                "</root>\n"
            )
            self._send(200, xml.encode("utf-8"), "text/xml; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/sony/camera":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(200, b'{"error":[5,"Illegal Request"],"id":0}', "application/json")
            return

        method = str(request.get("method", ""))
        params = request.get("params") or []
        payload = _dispatch(method, params)
        payload["id"] = request.get("id", 1)
        self._send(200, json.dumps(payload).encode("utf-8"), "application/json")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the simulated Sony camera")
    parser.add_argument("--port", type=int, default=10000)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Simulated camera on http://127.0.0.1:{args.port}")
    print(f"  descriptor : http://127.0.0.1:{args.port}/dd.xml")
    print(f"  endpoint   : http://127.0.0.1:{args.port}/sony/camera")
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
