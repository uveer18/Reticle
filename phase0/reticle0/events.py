"""Event-stream handling built on a single parked long-poll.

This is the power story. Sony's ``getEvent`` with ``polling=true`` blocks on the
camera until something changes (or the camera times out). One request can sit
there for tens of seconds, so the client learns about ``IDLE -> StillCapturing ->
StillSaving -> IDLE`` without a polling loop hammering the camera.

Rules encoded here:
* At most one long-poll in flight. A second concurrent request earns error 40402.
* When a response arrives (or the client times out), re-arm immediately.
* Never spin: any repeat after a transport failure is delayed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from .recorder import Recorder
from .rpc import LONG_POLL_TIMEOUT
from .sony import ERR_ALREADY_POLLING, SonyCamera


def flatten(result_payload: object) -> dict[str, dict]:
    """Turn the ``getEvent`` array into ``{object type: object}``."""
    events: dict[str, dict] = {}
    if not isinstance(result_payload, list):
        return events
    for entry in result_payload:
        if isinstance(entry, dict) and "type" in entry:
            events[str(entry["type"])] = entry
    return events


@dataclass
class EventTick:
    events: dict[str, dict]
    latency_ms: float
    timed_out: bool
    error_code: int | None = None

    @property
    def changed(self) -> bool:
        return bool(self.events)


class EventStream:
    """Sequential event consumer. One instance per camera session."""

    def __init__(
        self,
        camera: SonyCamera,
        *,
        recorder: Recorder | None = None,
        timeout: float = LONG_POLL_TIMEOUT,
    ) -> None:
        self.camera = camera
        self.recorder = recorder
        self.timeout = timeout

    def snapshot(self) -> EventTick:
        """Immediate, complete state read (``polling=false``)."""
        result = self.camera.get_event(polling=False)
        return EventTick(
            events=flatten(result.result),
            latency_ms=result.latency_ms,
            timed_out=result.timed_out,
            error_code=result.error_code,
        )

    def poll_once(self) -> EventTick:
        """One long-poll attempt, including 40402 recovery."""
        result = self.camera.get_event(polling=True, timeout=self.timeout)
        if result.error_code == ERR_ALREADY_POLLING:
            # A previous read timed out client-side but is still parked server-side.
            # Let it drain, then retry exactly once.
            time.sleep(1.0)
            result = self.camera.get_event(polling=True, timeout=self.timeout)
        return EventTick(
            events=flatten(result.result),
            latency_ms=result.latency_ms,
            timed_out=result.timed_out,
            error_code=result.error_code,
        )

    def camera_status(self) -> str | None:
        tick = self.snapshot()
        status = tick.events.get("cameraStatus", {})
        value = status.get("cameraStatus")
        return str(value) if value else None

    def stream(self, *, max_events: int = 10, seconds: float = 60.0) -> Iterator[EventTick]:
        """Yield ticks until either budget is exhausted."""
        deadline = time.monotonic() + seconds
        emitted = 0
        while emitted < max_events and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.timeout = min(self.timeout, max(5.0, remaining))
            tick = self.poll_once()
            if tick.changed:
                emitted += 1
                yield tick
            elif tick.error_code is not None and not tick.timed_out:
                if self.recorder:
                    self.recorder.event("note", message=f"getEvent error {tick.error_code}; backing off")
                time.sleep(1.0)
