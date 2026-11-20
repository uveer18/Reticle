"""Phase A helpers: host interface inventory, subnet arithmetic, camera-IP inference.

Nothing in this module talks to the camera. It describes the host side of the link
so the network baseline in the investigation record is reproducible rather than
remembered.
"""

from __future__ import annotations

import ipaddress
import locale
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_FIELD_DOTS = " . "

# ipconfig labels are localised. Match on a small keyword set per field rather
# than on an exact English string; the common Windows locales are covered.
_IPV4_WORDS = ("ipv4", "ip address", "ip-adresse", "dirección ip", "adresse ip", "indirizzo ip")
_NETMASK_WORDS = ("subnet", "mask", "maske", "mascara", "máscara", "subred", "subnetz", "sous-réseau")
_GATEWAY_WORDS = ("gateway", "puerta de enlace", "passerelle", "gateway predefinito", "standaardgateway")


def _is_adapter_header(lines: list[str], index: int) -> bool:
    """True when ``lines[index]`` opens a new adapter block.

    Windows prints one header per adapter, localised ("Ethernet adapter
    Ethernet:", "Adaptador de LAN inalámbrica Wi-Fi:"). Field lines also end in
    a colon when their value is empty, so a header is recognised by *not*
    looking like a field: field labels are padded with ``" . "`` runs.
    """
    stripped = lines[index].strip()
    if not stripped.endswith(":") or IPV4_RE.search(stripped) or _FIELD_DOTS in stripped:
        return False
    if "adapter" in stripped.lower() or "adaptador" in stripped.lower():
        return True
    for following in lines[index + 1 : index + 4]:
        if not following.strip():
            continue
        return following[:1].isspace() and _FIELD_DOTS in following
    return False


def _apply_field(block: dict[str, object], label: str, value: str) -> None:
    lowered = label.lower()
    found = IPV4_RE.search(value)
    if not found:
        return
    address = found.group(0)
    if any(word in lowered for word in _IPV4_WORDS) and not block["ipv4"]:
        block["ipv4"] = address
    elif any(word in lowered for word in _NETMASK_WORDS) and not block["netmask"]:
        block["netmask"] = address
    elif any(word in lowered for word in _GATEWAY_WORDS) and not block["gateway"]:
        block["gateway"] = address


def parse_ipconfig(text: str) -> list[Interface]:
    """Parse ``ipconfig`` text into usable IPv4 interfaces."""
    lines = text.splitlines()
    blocks: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    last_label = ""

    for index, line in enumerate(lines):
        if not line.strip():
            continue

        if _is_adapter_header(lines, index):
            current = {"name": line.strip().rstrip(":").strip(), "ipv4": None, "netmask": None, "gateway": None}
            blocks.append(current)
            last_label = ""
            continue

        if current is None:
            continue

        if ":" in line:
            label, _, value = line.partition(":")
            last_label = label.strip()
            _apply_field(current, last_label, value)
        elif last_label:
            # A multi-valued field (a dual-stack gateway) wraps onto its own
            # unlabelled continuation line on Windows.
            _apply_field(current, last_label, line)

    interfaces: list[Interface] = []
    for block in blocks:
        if not block["ipv4"] or not block["netmask"]:
            continue  # media disconnected / IPv6-only / unparsed locale
        interfaces.append(
            Interface(
                name=str(block["name"]),
                ipv4=str(block["ipv4"]),
                netmask=str(block["netmask"]),
                gateway=str(block["gateway"]) if block["gateway"] else None,
            )
        )
    return interfaces


def windows_interfaces() -> list[Interface]:
    """Run ``ipconfig`` and parse it.

    Windows is the only controller platform in Phase 0, and the stdlib has no
    portable way to enumerate addresses *with* a netmask. Parsing ``ipconfig``
    keeps the harness dependency-free; ``parse_ipconfig`` is unit-tested against
    captured output.
    """
    try:
        completed = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_ipconfig(_decode(completed.stdout))


@dataclass(frozen=True)
class Interface:
    """One IPv4-capable local interface."""

    name: str
    ipv4: str
    netmask: str
    gateway: str | None = None

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ipv4}/{self.netmask}", strict=False)

    @property
    def prefix_length(self) -> int:
        return self.network.prefixlen

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ipv4": self.ipv4,
            "netmask": self.netmask,
            "prefix_length": self.prefix_length,
            "network": str(self.network),
            "gateway": self.gateway,
        }


def _decode(raw: bytes) -> str:
    for encoding in (locale.getpreferredencoding(False), "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def primary_ipv4() -> str | None:
    """Return the address the OS would use for an outbound packet.

    No packet is sent: ``connect`` on a UDP socket only selects a route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def interfaces() -> list[Interface]:
    """All usable IPv4 interfaces, never loopback or link-local."""
    found: list[Interface] = []
    seen: set[str] = set()
    for interface in windows_interfaces():
        if interface.ipv4.startswith("127.") or interface.ipv4.startswith("169.254."):
            continue
        if interface.ipv4 in seen:
            continue
        seen.add(interface.ipv4)
        found.append(interface)
    if found:
        return found

    fallback: list[Interface] = []
    address = primary_ipv4()
    if address:
        fallback.append(Interface(name="<unknown>", ipv4=address, netmask="255.255.255.0"))
    return fallback


def primary_interface() -> Interface | None:
    """The interface carrying the default route, matched against the interface table."""
    address = primary_ipv4()
    if not address:
        return None
    available = interfaces()
    for interface in available:
        if interface.ipv4 == address:
            return interface
    for interface in available:
        if ipaddress.ip_address(address) in interface.network:
            return interface
    return None


def host_addresses(network: ipaddress.IPv4Network) -> list[str]:
    """Every assignable host address in *network*, network/broadcast excluded."""
    if network.num_addresses > 1024:
        raise ValueError(f"refusing to enumerate {network} ({network.num_addresses} addresses)")
    return [str(host) for host in network.hosts()]


def _tcp_probe(address: str, port: int, timeout: float) -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sock.connect_ex((address, port)) == 0:
            return address
        return None
    finally:
        sock.close()


def scan_http_hosts(
    network: ipaddress.IPv4Network,
    *,
    port: int = 80,
    timeout: float = 0.35,
    workers: int = 64,
    exclude: set[str] | None = None,
) -> list[str]:
    """TCP-connect sweep for a camera HTTP port.

    This is opt-in fallback only: it is a burst of connections across the subnet,
    which is exactly the traffic the project forbids during capture. It exists to
    rescue the session when SSDP is blocked by the host firewall.
    """
    skip = exclude or set()
    targets = [address for address in host_addresses(network) if address not in skip]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda item: _tcp_probe(item, port, timeout), targets)
        return [found for found in results if found]


def baseline() -> dict[str, object]:
    """Structured network baseline for the Phase A record."""
    interfaces_now = interfaces()
    primary = primary_interface()
    return {
        "hostname": socket.gethostname(),
        "primary_ipv4": primary_ipv4(),
        "primary_interface": primary.as_dict() if primary else None,
        "interfaces": [interface.as_dict() for interface in interfaces_now],
    }
