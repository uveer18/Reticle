"""Phase B: SSDP device discovery for the Sony Camera Remote API.

The camera answers an M-SEARCH for ``urn:schemas-sony-com:service:ScalarWebAPI:1``
with a unicast 200 OK whose ``LOCATION`` header points at a UPnP device
description. Per the Sony development guide, that descriptor is where the real
API endpoint lives -- never assume a fixed camera IP or port.

The wire format is handled here; the payload is parsed by ``reticle0.descriptor``.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

MCAST_GROUP = "239.255.255.250"
MCAST_PORT = 1900
SEARCH_TARGET = "urn:schemas-sony-com:service:ScalarWebAPI:1"
USER_AGENT = "Reticle-Phase0/0.1 UPnP/1.0"


def build_msearch(
    *,
    mx: int = 2,
    search_target: str = SEARCH_TARGET,
    user_agent: str = USER_AGENT,
) -> bytes:
    """Render the M-SEARCH datagram exactly as the Sony guide specifies."""
    lines = [
        "M-SEARCH * HTTP/1.1",
        f"HOST: {MCAST_GROUP}:{MCAST_PORT}",
        'MAN: "ssdp:discover"',
        f"MX: {mx}",
        f"ST: {search_target}",
        f"USER-AGENT: {user_agent}",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def parse_response(data: bytes, source: tuple[str, int] | None = None) -> dict[str, str]:
    """Parse a unicast SSDP response into a lower-cased header map."""
    text = data.decode("iso-8859-1", errors="replace")
    # Tolerate CRLF, LF, and bare CR: some stacks rewrite the datagram.
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    parsed: dict[str, str] = {"_status_line": lines[0] if lines else ""}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
    if source:
        parsed["_source_ip"] = source[0]
        parsed["_source_port"] = str(source[1])
    return parsed


@dataclass
class DiscoveryResult:
    """One responding device."""

    source_ip: str
    location: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def server(self) -> str:
        return self.headers.get("server", "")

    @property
    def search_target(self) -> str:
        return self.headers.get("st", "")


def _socket_for(interface_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((interface_ip, 0))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return sock


def discover(
    interface_ip: str,
    *,
    mx: int = 2,
    attempts: int = 3,
    search_target: str = SEARCH_TARGET,
    settle_seconds: float = 1.0,
) -> list[DiscoveryResult]:
    """Send M-SEARCH and collect unicast replies.

    ``interface_ip`` must be the local address on the camera network. Binding the
    socket to it (rather than to 0.0.0.0) is what forces the multicast out of the
    correct adapter when the laptop also has a normal internet connection.
    """
    payload = build_msearch(mx=mx, search_target=search_target)
    results: dict[tuple[str, str], DiscoveryResult] = {}

    sock = _socket_for(interface_ip)
    try:
        for _ in range(max(1, attempts)):
            sock.sendto(payload, (MCAST_GROUP, MCAST_PORT))
            time.sleep(0.15)

        deadline = time.monotonic() + mx + settle_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(remaining, 0.5))
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            headers = parse_response(data, address)
            location = headers.get("location", "")
            if not location:
                continue
            results.setdefault((address[0], location), DiscoveryResult(address[0], location, headers))
    finally:
        sock.close()

    return list(results.values())


def discover_all_interfaces(**kwargs: object) -> list[DiscoveryResult]:
    """Run discovery on every non-loopback interface and de-duplicate."""
    from . import net  # local import avoids a cycle in the package graph

    merged: dict[tuple[str, str], DiscoveryResult] = {}
    for interface in net.interfaces():
        try:
            for result in discover(interface.ipv4, **kwargs):  # type: ignore[arg-type]
                merged[(result.source_ip, result.location)] = result
        except OSError:
            continue  # interface refused multicast; try the next one
    return list(merged.values())

