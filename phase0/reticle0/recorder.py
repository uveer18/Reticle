"""Append-only evidence log.

Every byte exchanged with the camera is written as one JSON object per line, so a
claim in the findings document can be traced back to a specific request. This is
what makes the investigation falsifiable instead of anecdotal.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Recorder:
    """Writes JSONL evidence plus a human-readable console mirror."""

    def __init__(self, path: Path, *, echo: bool = True, simulated: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self.simulated = simulated
        self._started = time.monotonic()

    def _emit(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        record.setdefault("elapsed_ms", round((time.monotonic() - self._started) * 1000, 1))
        record.setdefault("provenance", "simulated" if self.simulated else "hardware")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.echo:
            print(_summary(record), flush=True)

    def event(self, kind: str, **fields: Any) -> None:
        self._emit({"kind": kind, **fields})

    def rpc(
        self,
        *,
        endpoint: str,
        method: str,
        params: Any,
        version: str,
        http_status: int | None,
        latency_ms: float,
        payload: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        self._emit(
            {
                "kind": "rpc",
                "endpoint": endpoint,
                "method": method,
                "params": params,
                "version": version,
                "http_status": http_status,
                "latency_ms": round(latency_ms, 1),
                "payload": payload,
                "transport_error": error,
            }
        )


def _summary(record: dict[str, Any]) -> str:
    prefix = "[SIM]" if record.get("provenance") == "simulated" else ""
    if record.get("kind") == "rpc":
        status = "ERR" if record.get("transport_error") else record.get("http_status")
        payload = record.get("payload") or {}
        rpc_error = payload.get("error")
        detail = f"error={rpc_error}" if rpc_error else "ok"
        return (
            f"  {prefix}[{status}] {record['method']:<26} {record['latency_ms']:>8.1f} ms  {detail}"
        )
    if record.get("kind") == "note":
        return f"  {prefix}.. {record.get('message', '')}"
    return f"  {prefix}.. {record.get('kind')}: {json.dumps({k: v for k, v in record.items() if k not in ('kind', 'ts', 'elapsed_ms', 'provenance')}, default=str)}"
