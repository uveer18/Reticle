"""Shared test doubles. Not collected by unittest (the name is not ``test_*``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0.rpc import RpcResult  # noqa: E402

ENDPOINT = "http://127.0.0.1:10000/sony/camera"


def ok(payload: Any, *, method: str = "stub") -> RpcResult:
    return RpcResult(
        method=method,
        endpoint=ENDPOINT,
        http_status=200,
        latency_ms=1.0,
        payload={"result": payload},
    )


def error(code: int, message: str = "", *, method: str = "stub") -> RpcResult:
    return RpcResult(
        method=method,
        endpoint=ENDPOINT,
        http_status=200,
        latency_ms=1.0,
        payload={"error": [code, message]},
    )


def method_types(rows: list[list[str]]) -> RpcResult:
    """``getMethodTypes`` answers with ``results``, not ``result``."""
    return RpcResult(
        method="getMethodTypes",
        endpoint=ENDPOINT,
        http_status=200,
        latency_ms=1.0,
        payload={"results": rows},
    )


class ScriptedClient:
    """Hands back pre-built results in order; records every call."""

    def __init__(self, script: list[RpcResult]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def call(self, method: str, params: Any = None, *, version: str = "1.0", timeout: float = 10.0) -> RpcResult:
        self.calls.append({"method": method, "params": params, "version": version, "timeout": timeout})
        if not self.script:
            raise AssertionError(f"unexpected extra RPC call: {method}")
        return self.script.pop(0)


class CannedClient:
    """Dispatches by method name. Unknown methods answer Sony's 404/12 error."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def call(self, method: str, params: Any = None, *, version: str = "1.0", timeout: float = 10.0) -> RpcResult:
        self.calls.append(method)
        value = self.responses.get(method)
        if value is None:
            return error(12, "No Such Method", method=method)
        if callable(value):
            value = value(params)
        return value


class FakeResponse:
    def __init__(self, status_code: int = 200, body: str = "{}") -> None:
        self.status_code = status_code
        self.text = body

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class FakeSession:
    """Records request bodies and replays a prepared response or exception."""

    def __init__(self, response: FakeResponse | Exception | None = None) -> None:
        self.response = response or FakeResponse(body='{"result": [0], "id": 1}')
        self.bodies: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def post(self, url: str, json: Any = None, headers: Any = None, timeout: float = 10.0) -> FakeResponse:
        self.bodies.append(json)
        self.timeouts.append(timeout)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def always(value: Any) -> Callable[[Any], Any]:
    return lambda _params: value
