"""The Sony protocol adapter.

This is the only module allowed to name a Sony JSON-RPC method. Everything above
it -- probe, events, capture, and eventually the Reticle capture engine -- speaks
in intents ("take a still", "what shutter speeds exist"), not Sony wire methods.

Method semantics are taken from the Camera Remote API API Reference v2.40.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .recorder import Recorder
from .rpc import LONG_POLL_TIMEOUT, RpcClient, RpcResult

# Error codes that are part of normal operation rather than failure.
ERR_STILL_CAPTURING = 40403   # "Still Capturing Not Finished" -- expected on long exposure
ERR_ALREADY_POLLING = 40402   # a long-poll is already parked on the server
ERR_CAMERA_NOT_READY = 40401


@dataclass
class CapabilitySets:
    """Supported vs. currently-available, kept strictly separate.

    ``supported`` comes from getMethodTypes and is a property of the firmware.
    ``available`` comes from getAvailableApiList / the event stream and is a
    property of the camera's current state. Conflating them is the classic bug.
    """

    supported: dict[str, list[str]] = field(default_factory=dict)
    available: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)

    def supports(self, method: str) -> bool:
        return method in self.supported

    def is_available(self, method: str) -> bool:
        return method in self.available

    def version_of(self, method: str, default: str = "1.0") -> str:
        versions = self.supported.get(method)
        if not versions:
            return default
        # Highest advertised version wins.
        return sorted(versions, key=_version_key)[-1]


def option_list(payload: Any) -> list[Any]:
    """Extract the selectable values from a ``getAvailable*`` response.

    Sony documents those responses as ``[current_value, [option, ...]]`` (for
    example ``["1/2", ["30\\"", "1\\"", "1/2"]]``). A couple of methods return
    the bare ``[[option, ...]]`` form instead, so both are accepted here. This
    is Sony response-shape knowledge and therefore belongs in this module.
    """
    if not isinstance(payload, list) or not payload:
        return []
    head = payload[0]
    if isinstance(head, list):
        return list(head)
    if len(payload) > 1 and isinstance(payload[1], list):
        return list(payload[1])
    return []


def shutter_seconds(value: object) -> float | None:
    """Convert a Sony shutter string to seconds.

    The documented wire form quotes whole seconds (``"30\\""`` means thirty
    seconds) and uses a slash for fractions (``"1/8000"``). Bulb and any
    non-numeric or non-positive value return ``None``. This is Sony value-encoding
    knowledge, so it lives in the adapter rather than in a caller.
    """
    if value is None:
        return None
    text = (
        str(value)
        .strip()
        .replace('"', "")
        .replace("\u201d", "")
        .replace("\u201c", "")
        .replace("\u2033", "")
    )
    if not text:
        return None
    if text.upper() in {"BULB", "B"}:
        return None
    if "/" in text:
        numerator, sep, denominator = text.partition("/")
        if not sep or not numerator or not denominator:
            return None
        try:
            num_f = float(numerator)
            den_f = float(denominator)
            if den_f <= 0 or num_f <= 0:
                return None
            return num_f / den_f
        except ValueError:
            return None
    try:
        val = float(text)
        return val if val > 0 else None
    except ValueError:
        return None


def match_option(options: list[Any], requested: object) -> str | None:
    """Pick the camera's exact advertised value for a user-supplied one.

    The operator shouldn't have to reproduce Sony's quoting (``20"``) on a
    command line, and the harness must never invent a wire value. So the
    requested value is matched against what the camera itself advertised:
    first by exact string, then by equal duration. Returns ``None`` when the
    value is not on offer.
    """
    if requested is None:
        return None
    wanted = str(requested).strip()
    advertised = [str(option) for option in options]
    if wanted in advertised:
        return wanted
    seconds = shutter_seconds(wanted)
    if seconds is None:
        return None
    for option in advertised:
        opt_sec = shutter_seconds(option)
        if opt_sec is not None and math.isclose(opt_sec, seconds, rel_tol=1e-5, abs_tol=1e-6):
            return option
    return None


def match_exact_option(options: list[Any], requested: object) -> str | None:
    """Resolve a requested value against exact camera-advertised options.

    Used for ISO/aperture where Sony wire values are opaque strings and should
    not be heuristically rewritten.
    """
    if requested is None:
        return None
    wanted = str(requested).strip()
    for option in options:
        if str(option) == wanted:
            return str(option)
    return None


def _version_key(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in value.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class SonyCamera:
    """Intent-level operations on the A7 II over the Camera Remote API."""

    def __init__(self, endpoint: str, *, recorder: Recorder | None = None, client: RpcClient | None = None) -> None:
        self.endpoint = endpoint
        self.rpc = client or RpcClient(endpoint, recorder=recorder)
        self.capabilities = CapabilitySets()

    @property
    def is_simulated(self) -> bool:
        """True if the camera endpoint points to a local simulated instance or test double."""
        ep = self.endpoint.lower()
        return (
            "127.0.0.1" in ep
            or "localhost" in ep
            or "::1" in ep
            or getattr(self.rpc, "is_simulated", False)
        )

    # ------------------------------------------------------------------ identity

    def get_application_info(self) -> RpcResult:
        return self.rpc.call("getApplicationInfo")

    def get_versions(self) -> RpcResult:
        return self.rpc.call("getVersions")

    def get_method_types(self, version: str = "") -> RpcResult:
        """Empty version string asks for every version the camera supports."""
        return self.rpc.call("getMethodTypes", [version])

    def get_available_api_list(self) -> RpcResult:
        return self.rpc.call("getAvailableApiList")

    # --------------------------------------------------------------------- state

    def get_event(self, *, polling: bool, timeout: float | None = None) -> RpcResult:
        """Park a long-poll (polling=True) or take an immediate snapshot (False)."""
        version = self.capabilities.version_of("getEvent", "1.0")
        effective = timeout if timeout is not None else (LONG_POLL_TIMEOUT if polling else 15.0)
        return self.rpc.call("getEvent", [polling], version=version, timeout=effective)

    def start_rec_mode(self) -> RpcResult:
        return self.rpc.call("startRecMode")

    def stop_rec_mode(self) -> RpcResult:
        return self.rpc.call("stopRecMode")

    def set_camera_function(self, function: str) -> RpcResult:
        return self.rpc.call("setCameraFunction", [function])

    def get_shoot_mode(self) -> RpcResult:
        return self.rpc.call("getShootMode")

    def set_shoot_mode(self, mode: str) -> RpcResult:
        return self.rpc.call("setShootMode", [mode])

    def get_storage_information(self) -> RpcResult:
        return self.rpc.call("getStorageInformation")

    # ------------------------------------------------------------------ exposure

    def _validation_error(self, method: str, message: str) -> RpcResult:
        return RpcResult(
            method=method,
            endpoint=self.endpoint,
            http_status=None,
            latency_ms=0.0,
            payload={"error": [3, message]},
            transport_error=None,
        )

    def resolve_shutter_speed(self, value: object) -> tuple[str | None, list[str], RpcResult | None]:
        available = self.get_available_shutter_speed()
        if not available.ok:
            return None, [], available
        options = [str(item) for item in self.available_options(available)]
        resolved = match_option(options, value)
        return resolved, options, None

    def resolve_iso_speed_rate(self, value: object) -> tuple[str | None, list[str], RpcResult | None]:
        available = self.get_available_iso_speed_rate()
        if not available.ok:
            return None, [], available
        options = [str(item) for item in self.available_options(available)]
        resolved = match_exact_option(options, value)
        return resolved, options, None

    def resolve_f_number(self, value: object) -> tuple[str | None, list[str], RpcResult | None]:
        available = self.get_available_f_number()
        if not available.ok:
            return None, [], available
        options = [str(item) for item in self.available_options(available)]
        resolved = match_exact_option(options, value)
        return resolved, options, None

    def get_shutter_speed(self) -> RpcResult:
        return self.rpc.call("getShutterSpeed")

    def get_available_shutter_speed(self) -> RpcResult:
        return self.rpc.call("getAvailableShutterSpeed")

    def set_shutter_speed(self, value: str, *, validate: bool = True) -> RpcResult:
        if validate:
            resolved, options, err = self.resolve_shutter_speed(value)
            if err is not None:
                return err
            if resolved is None:
                return self._validation_error(
                    "setShutterSpeed",
                    f"Requested shutter {value!r} is not in getAvailableShutterSpeed options: {options}",
                )
            value = resolved
        return self.rpc.call("setShutterSpeed", [value])

    def get_iso_speed_rate(self) -> RpcResult:
        return self.rpc.call("getIsoSpeedRate")

    def get_available_iso_speed_rate(self) -> RpcResult:
        return self.rpc.call("getAvailableIsoSpeedRate")

    def set_iso_speed_rate(self, value: str, *, validate: bool = True) -> RpcResult:
        if validate:
            resolved, options, err = self.resolve_iso_speed_rate(value)
            if err is not None:
                return err
            if resolved is None:
                return self._validation_error(
                    "setIsoSpeedRate",
                    f"Requested ISO {value!r} is not in getAvailableIsoSpeedRate options: {options}",
                )
            value = resolved
        return self.rpc.call("setIsoSpeedRate", [value])

    def get_f_number(self) -> RpcResult:
        return self.rpc.call("getFNumber")

    def get_available_f_number(self) -> RpcResult:
        return self.rpc.call("getAvailableFNumber")

    def set_f_number(self, value: str, *, validate: bool = True) -> RpcResult:
        if validate:
            resolved, options, err = self.resolve_f_number(value)
            if err is not None:
                return err
            if resolved is None:
                return self._validation_error(
                    "setFNumber",
                    f"Requested F number {value!r} is not in getAvailableFNumber options: {options}",
                )
            value = resolved
        return self.rpc.call("setFNumber", [value])

    def get_exposure_compensation(self) -> RpcResult:
        return self.rpc.call("getExposureCompensation")

    def get_available_exposure_compensation(self) -> RpcResult:
        return self.rpc.call("getAvailableExposureCompensation")

    def set_exposure_compensation(self, value: int) -> RpcResult:
        return self.rpc.call("setExposureCompensation", [value])

    def get_white_balance(self) -> RpcResult:
        return self.rpc.call("getWhiteBalance")

    def get_available_white_balance(self) -> RpcResult:
        return self.rpc.call("getAvailableWhiteBalance")

    def set_white_balance(self, value: str) -> RpcResult:
        return self.rpc.call("setWhiteBalance", [value])

    # ------------------------------------------------------------------- capture

    def take_picture(self, *, timeout: float = 40.0) -> RpcResult:
        return self.rpc.call("actTakePicture", [], timeout=timeout)

    def await_picture(self, *, timeout: float = 60.0) -> RpcResult:
        return self.rpc.call("awaitTakePicture", [], timeout=timeout)

    def start_interval_still(self) -> RpcResult:
        return self.rpc.call("startIntervalStillRec")

    def stop_interval_still(self) -> RpcResult:
        return self.rpc.call("stopIntervalStillRec")

    # -------------------------------------------------------------------- helpers

    @staticmethod
    def postview_urls(result: RpcResult) -> list[str]:
        """`actTakePicture`/`awaitTakePicture` return ``[[url, ...]]``."""
        payload = result.result
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            return [str(url) for url in payload[0]]
        return []

    @staticmethod
    def available_options(result: RpcResult) -> list[Any]:
        """Selectable values of a ``getAvailable*`` call (``[current, [options]]``)."""
        return option_list(result.result)

    @staticmethod
    def available_list(result: RpcResult) -> list[str]:
        """`getAvailableApiList` returns ``[[name, ...]]``."""
        payload = result.result
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            return [str(item) for item in payload[0]]
        return []

    @staticmethod
    def flat_list(result: RpcResult) -> list[Any]:
        """Return the list payload, unwrapping Sony's documented outer list.

        ``getVersions`` answers ``[["1.0", "1.1"]]`` while a plain getter such as
        ``getShutterSpeed`` answers ``["1/125"]``. Both should read as a list of
        values, so only a *nested* first element is unwrapped.
        """
        payload = result.result
        if not isinstance(payload, list):
            return []
        if payload and isinstance(payload[0], list):
            return list(payload[0])
        return list(payload)

    def load_capabilities(self) -> CapabilitySets:
        """Populate supported/available sets with the minimum number of calls."""
        versions = self.get_versions()
        if versions.ok:
            value = self.flat_list(versions)
            if isinstance(value, list):
                self.capabilities.versions = [str(item) for item in value]

        methods = self.get_method_types("")
        rows = methods.results
        if isinstance(rows, list):
            supported: dict[str, list[str]] = {}
            for row in rows:
                if not isinstance(row, list) or not row:
                    continue
                name = str(row[0])
                row_version = str(row[3]) if len(row) > 3 else "1.0"
                supported.setdefault(name, []).append(row_version)
            self.capabilities.supported = supported

        available = self.get_available_api_list()
        if available.ok:
            self.capabilities.available = self.available_list(available)

        return self.capabilities

    def describe_supported(self, names: Iterable[str]) -> dict[str, bool]:
        return {name: self.capabilities.supports(name) for name in names}
