"""Phase D: single-shot capture timing, including the long-exposure path.

The important mechanic, straight from the API reference: for a long exposure the
camera answers ``actTakePicture`` with error 40403 ("Still Capturing Not
Finished"). That is not a failure. The client then calls ``awaitTakePicture``,
which blocks until the exposure and save have finished, repeating while 40403 is
returned.

That behaviour is the whole basis for long-exposure control, so it is measured
rather than assumed: how often 40403 appears, how long each call blocks, and how
long after shutter release the camera reports IDLE again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .events import EventStream
from .recorder import Recorder
from .sony import ERR_STILL_CAPTURING, SonyCamera, shutter_seconds


@dataclass
class CaptureResult:
    outcome: str = "unknown"  # complete | no_postview | error | timeout | ambiguous
    requested_shutter: str | None = None
    status_before: str | None = None
    status_after: str | None = None
    await_calls: int = 0
    act_latency_ms: float | None = None
    act_error_code: int | None = None
    total_ms: float | None = None
    postview_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    simulated: bool = False
    ambiguous_reason: str | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome in {"complete", "no_postview", "success"}

    @property
    def is_ambiguous(self) -> bool:
        return self.outcome == "ambiguous"

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "requested_shutter": self.requested_shutter,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "await_calls": self.await_calls,
            "act_latency_ms": self.act_latency_ms,
            "act_error_code": self.act_error_code,
            "total_ms": self.total_ms,
            "postview_urls": self.postview_urls,
            "notes": self.notes,
            "timeline": self.timeline,
            "simulated": self.simulated,
            "ambiguous_reason": self.ambiguous_reason,
        }


def capture_once(
    camera: SonyCamera,
    recorder: Recorder,
    *,
    shutter: str | None = None,
    set_shutter: bool = False,
    expected_seconds: float | None = None,
    budget_seconds: float | None = None,
) -> CaptureResult:
    """Take exactly one still and time it end to end."""
    stream = EventStream(camera, recorder=recorder)
    result = CaptureResult(
        requested_shutter=shutter if set_shutter else None,
        simulated=getattr(camera, "is_simulated", False),
    )

    before = stream.snapshot()
    result.status_before = (before.events.get("cameraStatus") or {}).get("cameraStatus")
    result.timeline.append(
        {
            "phase": "snapshot_before",
            "camera_status": result.status_before,
            "error_code": before.error_code,
        }
    )

    if set_shutter and shutter:
        resolved, options, resolve_err = camera.resolve_shutter_speed(shutter)
        result.timeline.append(
            {
                "phase": "validate_shutter",
                "requested": shutter,
                "resolved": resolved,
                "options_count": len(options),
                "ok": resolve_err.ok if resolve_err is not None else resolved is not None,
                "error": resolve_err.error if resolve_err is not None else (None if resolved else "not available"),
            }
        )
        if resolve_err is not None:
            if resolve_err.is_ambiguous:
                result.outcome = "ambiguous"
                result.ambiguous_reason = (
                    f"getAvailableShutterSpeed transport ambiguity: {resolve_err.transport_error}"
                )
                result.notes.append(
                    "Shutter validation returned an ambiguous transport outcome; "
                    "capture aborted before actTakePicture."
                )
            else:
                result.outcome = "error"
                result.notes.append(f"Shutter validation failed: {resolve_err.error}")
            return result
        if resolved is None:
            result.outcome = "error"
            result.notes.append(
                f"Requested shutter {shutter!r} is not in getAvailableShutterSpeed options: {options}"
            )
            return result

        applied = camera.set_shutter_speed(resolved, validate=False)
        result.timeline.append(
            {
                "phase": "set_shutter",
                "requested": shutter,
                "value": resolved,
                "ok": applied.ok,
                "error": applied.error,
                "ambiguous": applied.is_ambiguous,
            }
        )
        if not applied.ok:
            if applied.is_ambiguous:
                result.outcome = "ambiguous"
                result.ambiguous_reason = (
                    f"setShutterSpeed transport ambiguity: {applied.transport_error}"
                )
                result.notes.append(
                    "setShutterSpeed returned an ambiguous transport outcome; "
                    "camera state is unknown and capture was aborted before actTakePicture."
                )
            else:
                result.outcome = "error"
                result.notes.append(f"setShutterSpeed rejected {resolved}: {applied.error}")
            return result

        # The API notes the setting may still be in flight on the server.
        time.sleep(0.35)
        readback = camera.get_shutter_speed()
        result.timeline.append(
            {
                "phase": "shutter_readback",
                "requested": resolved,
                "ok": readback.ok,
                "value": str((readback.result or [None])[0]) if readback.ok else None,
                "error": readback.error,
                "ambiguous": readback.is_ambiguous,
            }
        )
        if not readback.ok:
            if readback.is_ambiguous:
                result.outcome = "ambiguous"
                result.ambiguous_reason = (
                    f"getShutterSpeed readback transport ambiguity: {readback.transport_error}"
                )
                result.notes.append(
                    "Shutter readback was ambiguous; capture aborted before actTakePicture."
                )
            else:
                result.outcome = "error"
                result.notes.append(f"Shutter readback failed: {readback.error}")
            return result

        actual = str((readback.result or [None])[0])
        result.requested_shutter = actual
        if shutter_seconds(actual) is not None:
            expected_seconds = shutter_seconds(actual)
        if actual != resolved:
            result.outcome = "error"
            result.notes.append(
                f"Shutter readback mismatch: requested {resolved!r}, camera reports {actual!r}"
            )
            return result

    # Fresh preflight status check immediately before actTakePicture.
    preflight = stream.snapshot()
    preflight_status = (preflight.events.get("cameraStatus") or {}).get("cameraStatus")
    result.timeline.append(
        {
            "phase": "preflight_status",
            "camera_status": preflight_status,
            "error_code": preflight.error_code,
        }
    )
    if preflight_status != "IDLE":
        result.outcome = "error"
        if preflight_status is None:
            result.notes.append(
                "Preflight aborted: cameraStatus missing/unknown; refusing actTakePicture."
            )
        else:
            result.notes.append(
                f"Preflight aborted: cameraStatus is {preflight_status!r}, expected 'IDLE'."
            )
        result.status_after = preflight_status
        recorder.event(
            "note",
            message="capture aborted at preflight",
            outcome=result.outcome,
            camera_status=preflight_status,
        )
        return result

    if budget_seconds is None:
        budget_seconds = max(90.0, (expected_seconds or 0.0) * 2 + 60.0)

    started = time.monotonic()
    act = camera.take_picture()
    result.act_latency_ms = round(act.latency_ms, 1)
    result.act_error_code = act.error_code
    result.timeline.append(
        {
            "phase": "act_take_picture",
            "latency_ms": round(act.latency_ms, 1),
            "ok": act.ok,
            "error": act.error,
            "ambiguous": act.is_ambiguous,
            "transport_error": act.transport_error,
        }
    )

    if act.ok:
        result.postview_urls = camera.postview_urls(act)
        result.outcome = "complete" if result.postview_urls else "no_postview"
    elif act.is_ambiguous:
        result.outcome = "ambiguous"
        result.ambiguous_reason = f"actTakePicture transport ambiguity: {act.transport_error}"
        result.notes.append(
            "actTakePicture returned an ambiguous transport outcome; "
            "camera state is unknown and actTakePicture must not be retried automatically."
        )
    elif act.error_code == ERR_STILL_CAPTURING:
        result.notes.append("40403 on actTakePicture: exposure still running (expected for long shutter)")

        deadline = started + budget_seconds
        while time.monotonic() < deadline:
            awaited = camera.await_picture()
            result.await_calls += 1
            result.timeline.append(
                {
                    "phase": "await_take_picture",
                    "call": result.await_calls,
                    "latency_ms": round(awaited.latency_ms, 1),
                    "ok": awaited.ok,
                    "error": awaited.error,
                    "ambiguous": awaited.is_ambiguous,
                    "transport_error": awaited.transport_error,
                }
            )
            if awaited.ok:
                result.postview_urls = camera.postview_urls(awaited)
                result.outcome = "complete" if result.postview_urls else "no_postview"
                break
            if awaited.is_ambiguous:
                result.outcome = "ambiguous"
                result.ambiguous_reason = (
                    f"awaitTakePicture transport ambiguity: {awaited.transport_error}"
                )
                result.notes.append(
                    "awaitTakePicture returned an ambiguous transport outcome; "
                    "capture may have completed on camera. Do not retry actTakePicture automatically."
                )
                break
            if awaited.error_code != ERR_STILL_CAPTURING:
                result.outcome = "error"
                result.notes.append(f"awaitTakePicture failed: {awaited.error}")
                break
            # Blocking await plus a short pause keeps the loop from becoming a spin.
            time.sleep(0.2)
        else:
            result.outcome = "timeout"
            result.notes.append(f"capture exceeded {budget_seconds:.0f}s budget")
    else:
        result.outcome = "error"
        result.notes.append(f"actTakePicture failed: {act.error}")

    result.total_ms = round((time.monotonic() - started) * 1000, 1)

    # One snapshot to confirm the camera returned to a resting state.
    after = stream.snapshot()
    result.status_after = (after.events.get("cameraStatus") or {}).get("cameraStatus")
    result.timeline.append({"phase": "snapshot_after", "camera_status": result.status_after})

    recorder.event("note", message="capture finished", outcome=result.outcome, total_ms=result.total_ms)
    return result
