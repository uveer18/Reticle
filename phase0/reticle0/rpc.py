"""Minimal JSON-RPC client for Sony's ScalarWebAPI.

Deliberate properties, each traceable to a project constraint:

* No automatic retries. A retry is extra traffic and it corrupts timing evidence.
  Retry policy belongs to the future Recovery Manager, not to the transport.
* No keep-alive tuning beyond what ``requests`` does by default.
* Every call records its exchange through ``Recorder``.
* Long-poll timeouts are normal, not failures -- ``getEvent`` is expected to block.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .recorder import Recorder

DEFAULT_TIMEOUT = 10.0
LONG_POLL_TIMEOUT = 70.0


class RpcResult:
    """Outcome of one JSON-RPC call."""

    def __init__(
        self,
        *,
        method: str,
        endpoint: str,
        http_status: int | None,
        latency_ms: float,
        payload: dict[str, Any] | None,
        transport_error: str | None = None,
        timed_out: bool = False,
        raw_text: str = "",
    ) -> None:
        self.method = method
        self.endpoint = endpoint
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.payload = payload or {}
        self.transport_error = transport_error
        self.timed_out = timed_out
        self.raw_text = raw_text

    @property
    def ok(self) -> bool:
        return self.transport_error is None and "error" not in self.payload and self.http_status == 200

    @property
    def is_error(self) -> bool:
        return not self.ok

    @property
    def is_ambiguous(self) -> bool:
        """True when the client cannot know whether the camera executed the request.

        Transport timeouts, connection losses, and unparseable responses are
        treated as ambiguous because the request may have reached the camera.
        Callers must reconcile camera state before retrying any mutating action.
        """
        return self.transport_error is not None

    @property
    def error_message(self) -> str | None:
        err = self.error
        return err[1] if err else self.transport_error

    @property
    def result(self) -> Any:
        return self.payload.get("result")

    @property
    def results(self) -> Any:
        """``getMethodTypes`` answers with ``results`` rather than ``result``."""
        return self.payload.get("results")

    @property
    def error(self) -> tuple[int, str] | None:
        value = self.payload.get("error")
        if not value:
            return None
        try:
            return int(value[0]), str(value[1])
        except (IndexError, TypeError, ValueError):
            return None

    @property
    def error_code(self) -> int | None:
        error = self.error
        return error[0] if error else None

    def __repr__(self) -> str:
        if self.transport_error:
            return f"<RpcResult {self.method} transport_error={self.transport_error!r}>"
        return f"<RpcResult {self.method} ok={self.ok} error={self.error}>"


class RpcClient:
    """JSON-RPC over HTTP against a single ScalarWebAPI service endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        recorder: Recorder | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        self.endpoint = endpoint
        self.recorder = recorder
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._next_id = 1
        ep = endpoint.lower()
        self.is_simulated = "127.0.0.1" in ep or "localhost" in ep or "::1" in ep

    def _take_id(self) -> int:
        # Sony forbids id 0; the id space is 1..2_147_483_647.
        with self._lock:
            current = self._next_id
            self._next_id = 1 if current >= 2_147_483_647 else current + 1
            return current

    def call(
        self,
        method: str,
        params: Any | None = None,
        *,
        version: str = "1.0",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> RpcResult:
        request_id = self._take_id()
        body = {
            "method": method,
            "params": params if params is not None else [],
            "id": request_id,
            "version": version,
        }

        started = time.monotonic()
        http_status: int | None = None
        payload: dict[str, Any] | None = None
        transport_error: str | None = None
        timed_out = False
        raw_text = ""

        try:
            response = self.session.post(
                self.endpoint,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            http_status = response.status_code
            raw_text = response.text
            try:
                payload = response.json()
            except ValueError:
                payload = {"_unparsed": raw_text}
                transport_error = "response was not JSON"
        except requests.Timeout:
            timed_out = True
            transport_error = f"timeout after {timeout:.0f}s"
        except requests.RequestException as exc:
            transport_error = f"{type(exc).__name__}: {exc}"

        latency_ms = (time.monotonic() - started) * 1000

        if self.recorder:
            self.recorder.rpc(
                endpoint=self.endpoint,
                method=method,
                params=body["params"],
                version=version,
                http_status=http_status,
                latency_ms=latency_ms,
                payload=payload,
                error=transport_error,
            )

        return RpcResult(
            method=method,
            endpoint=self.endpoint,
            http_status=http_status,
            latency_ms=latency_ms,
            payload=payload,
            transport_error=transport_error,
            timed_out=timed_out,
            raw_text=raw_text,
        )
