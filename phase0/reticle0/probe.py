"""Phase C: read-only capability probe.

The probe answers "what can this camera actually do" using the camera as the
authority, not the compatibility table. It is observational: no ``set*`` call is
made, and live view is never started. Request count is kept to roughly a dozen.

Supported vs. available is the central distinction:

* getMethodTypes  -> firmware capability, stable while powered on.
* getAvailableApiList -> what is legal *right now*, which changes with camera
  state (still vs movie, remote-shooting vs contents-transfer).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .events import EventStream
from .recorder import Recorder
from .sony import SonyCamera, option_list, shutter_seconds

# Read-only shutter/settings calls, in the order they are attempted. Each is
# individually optional because the A7 II may not expose all of them.
SETTINGS_PROBES = (
    "getShutterSpeed",
    "getAvailableShutterSpeed",
    "getIsoSpeedRate",
    "getAvailableIsoSpeedRate",
    "getFNumber",
    "getAvailableFNumber",
    "getExposureCompensation",
    "getWhiteBalance",
    "getStorageInformation",
)

CAPTURE_METHODS = (
    "actTakePicture",
    "awaitTakePicture",
    "startContShooting",
    "stopContShooting",
    "startIntervalStillRec",
    "stopIntervalStillRec",
    "startLiveview",
    "stopLiveview",
    "getEvent",
    "startRecMode",
    "stopRecMode",
    "setShutterSpeed",
    "getAvailableShutterSpeed",
    "setIsoSpeedRate",
    "setFNumber",
    "setExposureCompensation",
)


def _threshold_summary(values: list[str]) -> dict[str, Any]:
    seconds = [(value, shutter_seconds(value)) for value in values]
    numeric = [(value, sec) for value, sec in seconds if sec is not None]
    numeric.sort(key=lambda item: item[1])  # type: ignore[arg-type,return-value]
    return {
        "count": len(values),
        "fastest": numeric[0][0] if numeric else None,
        "fastest_seconds": numeric[0][1] if numeric else None,
        "slowest": numeric[-1][0] if numeric else None,
        "slowest_seconds": numeric[-1][1] if numeric else None,
        "exposes_bulb": any(str(value).upper().startswith("BULB") for value in values),
        "at_or_beyond_1s": [value for value, sec in numeric if sec is not None and sec >= 1.0],
    }


def run_probe(camera: SonyCamera, recorder: Recorder) -> dict[str, Any]:
    """Execute the read-only probe and return a serialisable report."""
    report: dict[str, Any] = {
        "schema": "reticle.phase0.capability/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": camera.endpoint,
        "identity": {},
        "protocol": {},
        "capability": {},
        "event_snapshot": {},
        "settings": {},
        "derived": {},
        "errors": [],
    }

    # --- identity -----------------------------------------------------------
    app_info = camera.get_application_info()
    if app_info.ok:
        values = app_info.result or []
        report["identity"] = {
            "application_name": values[0] if len(values) > 0 else None,
            "api_version": values[1] if len(values) > 1 else None,
        }
    else:
        report["errors"].append({"step": "getApplicationInfo", "error": str(app_info.error)})

    # --- supported / available ---------------------------------------------
    capabilities = camera.load_capabilities()
    report["protocol"] = {
        "versions": capabilities.versions,
        "supported_method_count": len(capabilities.supported),
        "available_api_count": len(capabilities.available),
        "get_event_versions": capabilities.supported.get("getEvent", []),
    }
    report["capability"] = {
        "supported": {name: sorted(set(versions)) for name, versions in sorted(capabilities.supported.items())},
        "available": sorted(capabilities.available),
        "capture": {name: capabilities.supports(name) for name in CAPTURE_METHODS},
        "capture_currently_available": {
            name: capabilities.is_available(name) for name in CAPTURE_METHODS
        },
    }

    # --- event snapshot -----------------------------------------------------
    tick = EventStream(camera, recorder=recorder).snapshot()
    snapshot = tick.events  # EventStream already keys the payload by object type
    report["event_snapshot"] = snapshot
    report["protocol"]["event_object_types"] = sorted(snapshot)
    report["protocol"]["camera_status"] = (snapshot.get("cameraStatus") or {}).get("cameraStatus")

    # --- settings (best effort, one call each) -----------------------------
    for method in SETTINGS_PROBES:
        if not capabilities.supports(method):
            report["settings"][method] = {"supported": False}
            continue
        if not capabilities.is_available(method):
            report["settings"][method] = {"supported": True, "available": False}
            continue
        result = camera.rpc.call(method)
        report["settings"][method] = {
            "supported": True,
            "available": True,
            "ok": result.ok,
            "value": result.result,
            "error": result.error,
            "latency_ms": round(result.latency_ms, 1),
        }

    # --- derived facts ------------------------------------------------------
    # getAvailableShutterSpeed documents its result as [current, [options...]].
    shutter = report["settings"].get("getAvailableShutterSpeed", {})
    shutter_list = [str(item) for item in option_list(shutter.get("value"))]

    summary = _threshold_summary(shutter_list) if shutter_list else {}
    report["derived"] = {
        "shutter": summary,
        "max_exposure_seconds": summary.get("slowest_seconds"),
        "long_exposure_capable": bool(summary.get("slowest_seconds") and summary["slowest_seconds"] >= 1.0),
        "bulb_exposed_by_api": bool(summary.get("exposes_bulb")),
        "await_take_picture_supported": capabilities.supports("awaitTakePicture"),
        "interval_still_supported": capabilities.supports("startIntervalStillRec"),
        "interval_still_available": capabilities.is_available("startIntervalStillRec"),
        "liveview_supported": capabilities.supports("startLiveview"),
        "liveview_running": bool((snapshot.get("liveviewStatus") or {}).get("liveviewStatus")),
        "start_rec_mode_supported": capabilities.supports("startRecMode"),
        "start_rec_mode_available": capabilities.is_available("startRecMode"),
    }

    recorder.event("note", message="probe complete", derived=report["derived"])
    return report


# --------------------------------------------------------------------- rendering

def _mark(value: bool) -> str:
    return "yes" if value else "no"


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable capability matrix for docs/phase0/02-capability-matrix.md."""
    identity = report.get("identity", {})
    derived = report.get("derived", {})
    protocol = report.get("protocol", {})
    capture = report.get("capability", {}).get("capture", {})
    currently = report.get("capability", {}).get("capture_currently_available", {})
    shutter = derived.get("shutter", {})

    lines = [
        "# Capability matrix (measured)",
        "",
        "Generated by `phase0/run.py probe`. This file is machine-produced; do not hand-edit.",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Endpoint: `{report.get('endpoint')}`",
        f"- Application: `{identity.get('application_name')}`",
        f"- Camera Remote API version: `{identity.get('api_version')}`",
        f"- Camera status at probe time: `{protocol.get('camera_status')}`",
        "",
        "## Exposure envelope",
        "",
        "| Property | Value |",
        "| --- | --- |",
        f"| Fastest shutter | `{shutter.get('fastest')}` |",
        f"| Slowest shutter | `{shutter.get('slowest')}` |",
        f"| Slowest in seconds | `{shutter.get('slowest_seconds')}` |",
        f"| Shutter option count | {shutter.get('count', 0)} |",
        f"| Bulb exposed by API | {_mark(bool(derived.get('bulb_exposed_by_api')))} |",
        f"| `awaitTakePicture` supported | {_mark(bool(derived.get('await_take_picture_supported')))} |",
        "",
        "## Capture and control",
        "",
        "| Method | Supported | Available now |",
        "| --- | --- | --- |",
    ]
    for name, supported in capture.items():
        lines.append(f"| `{name}` | {_mark(bool(supported))} | {_mark(bool(currently.get(name)))} |")

    lines += [
        "",
        "## Derived findings",
        "",
        f"- Long exposure via API: {'yes' if derived.get('long_exposure_capable') else 'no'}"
        f" (max {derived.get('max_exposure_seconds')} s)",
        f"- Native interval still recording: "
        f"{'supported' if derived.get('interval_still_supported') else 'not supported'}"
        f" ({'available' if derived.get('interval_still_available') else 'not available'})",
        f"- Live view supported: {_mark(bool(derived.get('liveview_supported')))}, "
        f"running: {_mark(bool(derived.get('liveview_running')))}",
        f"- `startRecMode` gating: "
        f"{'supported' if derived.get('start_rec_mode_supported') else 'not offered'}"
        f" ({'available' if derived.get('start_rec_mode_available') else 'not available'})",
        "",
        "## Event object types observed",
        "",
        ", ".join(f"`{name}`" for name in protocol.get("event_object_types", [])) or "_none_",
        "",
    ]

    if report.get("errors"):
        lines += ["## Probe errors", "", "```json"]
        lines += [str(error) for error in report["errors"]]
        lines += ["```", ""]

    return "\n".join(lines)
